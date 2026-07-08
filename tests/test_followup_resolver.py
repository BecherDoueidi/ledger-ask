"""
followup_resolver.py -- classification (is this a follow-up? which
operation?) and the deterministic transforms themselves. The full
example chain from the conversational-follow-ups feature request is
covered explicitly (test_full_conversation_chain_matches_spec_example),
since that's the concrete spec this module was built against.
"""

import followup_resolver as fr


COLUMNS = ["DonorId", "FullName", "Email", "Status"]
ROWS = [
    {"DonorId": 3, "FullName": "Zara", "Email": "z@x.com", "Status": "Active"},
    {"DonorId": 1, "FullName": "Amir", "Email": "a@x.com", "Status": "Inactive"},
    {"DonorId": 1, "FullName": "Amir", "Email": "a@x.com", "Status": "Inactive"},
]


class TestIsFollowup:
    def test_false_when_no_active_conversation(self):
        assert fr.is_followup("sort them by name", has_active_conversation=False) is False

    def test_true_for_pronoun_reference(self):
        assert fr.is_followup("Sort them by name.", has_active_conversation=True) is True

    def test_true_for_leading_transform_verb(self):
        assert fr.is_followup("Only show active donors.", has_active_conversation=True) is True

    def test_true_for_now_and_their(self):
        assert fr.is_followup("Now show their total donations.", has_active_conversation=True) is True

    def test_false_for_unrelated_new_topic(self):
        assert fr.is_followup("How many volunteers are there?", has_active_conversation=True) is False


class TestResolveColumn:
    def test_exact_match(self):
        assert fr.resolve_column("Email", COLUMNS) == "Email"

    def test_case_insensitive_substring(self):
        assert fr.resolve_column("name", COLUMNS) == "FullName"

    def test_plural_stripping(self):
        assert fr.resolve_column("emails", COLUMNS) == "Email"

    def test_multi_word_field_ignores_spaces(self):
        # Regression: "total donations" must match "TotalDonations" --
        # without space-normalization this silently escalated every
        # multi-word field reference to the expensive LLM tier.
        assert fr.resolve_column("total donations", ["CampaignId", "TotalDonations"]) == "TotalDonations"

    def test_no_match_returns_none_never_guesses(self):
        assert fr.resolve_column("nonexistent field", COLUMNS) is None


class TestClassifyOperation:
    def test_sort_with_direction(self):
        cls = fr.classify_operation("Sort them by name.", COLUMNS)
        assert cls.operation == "sort"
        assert cls.params == {"field": "FullName", "descending": False}

    def test_sort_descending(self):
        cls = fr.classify_operation("Sort by total donations descending", ["TotalDonations"])
        assert cls.operation == "sort"
        assert cls.params["descending"] is True
        assert cls.params["field"] == "TotalDonations"

    def test_sort_ascending_direction_word_is_stripped_before_resolving_field(self):
        # Regression: "contribution ascending" must resolve to
        # "AnnualContribution" -- without stripping "ascending" first,
        # the concatenated "contributionascending" doesn't substring-match
        # "annualcontribution" (unlike the descending case, which only
        # ever worked by the coincidence of being a prefix match).
        cls = fr.classify_operation("Sort them by contribution ascending", ["AnnualContribution", "CompanyName"])
        assert cls.operation == "sort"
        assert cls.params["field"] == "AnnualContribution"
        assert cls.params["descending"] is False

    def test_limit_first_n(self):
        cls = fr.classify_operation("Show only the first 10.", COLUMNS)
        assert cls.operation == "limit"
        assert cls.params == {"n": 10}

    def test_limit_top_n(self):
        cls = fr.classify_operation("top 5", COLUMNS)
        assert cls.operation == "limit"
        assert cls.params == {"n": 5}

    def test_dedupe_by_named_field(self):
        cls = fr.classify_operation("Remove duplicate emails.", COLUMNS)
        assert cls.operation == "dedupe"
        assert cls.params == {"field": "Email"}

    def test_dedupe_whole_row_when_no_field_named(self):
        cls = fr.classify_operation("just remove duplicates", COLUMNS)
        assert cls.operation == "dedupe"
        assert cls.params == {"field": None}

    def test_filter_known_status_adjective(self):
        cls = fr.classify_operation("Only show active donors.", COLUMNS)
        assert cls.operation == "filter"
        assert cls.params == {"column": "Status", "value": "Active"}

    def test_filter_falls_through_to_other_without_status_column(self):
        cls = fr.classify_operation("Only show active donors.", ["FullName", "Email"])
        assert cls.operation == "other"

    def test_column_select(self):
        cls = fr.classify_operation("just show name and email", COLUMNS)
        assert cls.operation == "column_select"
        assert cls.params == {"fields": ["FullName", "Email"]}

    def test_chart_with_explicit_type(self):
        cls = fr.classify_operation("Create a bar chart.", COLUMNS)
        assert cls.operation == "chart"
        assert cls.params == {"forced_type": "bar"}

    def test_chart_without_explicit_type_defaults_to_auto(self):
        cls = fr.classify_operation("show me a graph of the result", COLUMNS)
        assert cls.operation == "chart"
        assert cls.params == {"forced_type": "auto"}

    def test_requires_new_join_falls_to_other(self):
        cls = fr.classify_operation("Now show their total donations.", COLUMNS)
        assert cls.operation == "other"


class TestApplyTransform:
    def test_sort_ascending(self):
        cls = fr.Classification("sort", field="FullName", descending=False)
        result = fr.apply_transform(ROWS, cls, COLUMNS)
        assert [r["FullName"] for r in result] == ["Amir", "Amir", "Zara"]

    def test_sort_without_resolved_field_returns_none(self):
        cls = fr.Classification("sort", field=None, descending=False)
        assert fr.apply_transform(ROWS, cls, COLUMNS) is None

    def test_sort_numeric_strings_from_a_cache_hit_sort_numerically_not_lexicographically(self):
        # Regression: found via a real screenshot -- a cache-hit's DECIMAL
        # values arrive as strings ("85000.00"). Sorting those as plain
        # strings put "85000.00" ahead of "100000.00" (lexicographic '8'
        # > '1'), scrambling anything but single-leading-digit values.
        rows = [
            {"CampaignId": 7, "TotalDonations": "85000.00"},
            {"CampaignId": 3, "TotalDonations": "8000.00"},
            {"CampaignId": 4, "TotalDonations": "50000.00"},
            {"CampaignId": 9, "TotalDonations": "100000.00"},
            {"CampaignId": 10, "TotalDonations": "10000.00"},
        ]
        cls = fr.Classification("sort", field="TotalDonations", descending=True)
        result = fr.apply_transform(rows, cls, ["CampaignId", "TotalDonations"])
        assert [r["TotalDonations"] for r in result] == ["100000.00", "85000.00", "50000.00", "10000.00", "8000.00"]

    def test_sort_non_numeric_strings_still_sort_lexicographically(self):
        rows = [{"City": "Sharjah"}, {"City": "Ajman"}, {"City": "Dubai"}]
        cls = fr.Classification("sort", field="City", descending=False)
        result = fr.apply_transform(rows, cls, ["City"])
        assert [r["City"] for r in result] == ["Ajman", "Dubai", "Sharjah"]

    def test_limit(self):
        cls = fr.Classification("limit", n=2)
        assert len(fr.apply_transform(ROWS, cls, COLUMNS)) == 2

    def test_dedupe_by_field(self):
        cls = fr.Classification("dedupe", field="Email")
        result = fr.apply_transform(ROWS, cls, COLUMNS)
        assert len(result) == 2  # z@x.com once, a@x.com once (dropping the second Amir row)

    def test_dedupe_whole_row(self):
        cls = fr.Classification("dedupe", field=None)
        result = fr.apply_transform(ROWS, cls, COLUMNS)
        assert len(result) == 2  # the two identical Amir rows collapse to one

    def test_filter_by_status(self):
        cls = fr.Classification("filter", column="Status", value="Active")
        result = fr.apply_transform(ROWS, cls, COLUMNS)
        assert len(result) == 1
        assert result[0]["FullName"] == "Zara"

    def test_filter_on_missing_column_returns_none(self):
        cls = fr.Classification("filter", column="NotAColumn", value="Active")
        assert fr.apply_transform(ROWS, cls, COLUMNS) is None

    def test_column_select_projects_only_requested_fields(self):
        cls = fr.Classification("column_select", fields=["FullName"])
        result = fr.apply_transform(ROWS, cls, COLUMNS)
        assert result[0] == {"FullName": "Zara"}

    def test_chart_and_other_are_not_row_transforms(self):
        assert fr.apply_transform(ROWS, fr.Classification("chart", forced_type="bar"), COLUMNS) is None
        assert fr.apply_transform(ROWS, fr.Classification("other"), COLUMNS) is None


def test_full_conversation_chain_matches_spec_example():
    """
    The exact chain from the feature request: show donors -> sort ->
    filter -> dedupe -> limit -> aggregate (escalates) -> chart.
    Verifies classification end-to-end without needing a live server.
    """
    columns = ["FullName", "Mobile", "Email", "Nationality", "RegistrationDate", "Status"]

    assert fr.classify_operation("Sort them by name.", columns).operation == "sort"
    assert fr.classify_operation("Only show active donors.", columns).operation == "filter"
    assert fr.classify_operation("Remove duplicate emails.", columns).operation == "dedupe"
    assert fr.classify_operation("Show only the first 10.", columns).operation == "limit"
    # Requires a JOIN to Donations -- no deterministic transform can
    # produce this from the currently-fetched columns, so it must
    # escalate rather than silently doing nothing.
    assert fr.classify_operation("Now show their total donations.", columns).operation == "other"
    assert fr.classify_operation("Create a bar chart.", columns).operation == "chart"
