"""
chart_advisor.recommend() -- the core "should we chart this, and as
what" decision engine. Cases here are drawn directly from real bugs
found during manual testing this session (Decimal-as-string after a
cache round-trip, phone numbers misclassified as numeric, CampaignId
treated as a measurement, the multi-dimension "auto" chart fallback),
not just the happy path, so a regression here would have re-broken
something a real user already hit.
"""

import chart_advisor as ca


def test_scalar_result_has_no_chart():
    assert ca.recommend([{"COUNT(*)": 9}]) is None


def test_empty_result_has_no_chart():
    assert ca.recommend([]) is None
    assert ca.recommend(None) is None


def test_single_column_multi_row_has_no_chart():
    assert ca.recommend([{"FullName": "A"}, {"FullName": "B"}]) is None


def test_category_and_amount_produces_bar_chart():
    rows = [
        {"CampaignName": "A", "Total": 100},
        {"CampaignName": "B", "Total": 200},
        {"CampaignName": "C", "Total": 150},
    ]
    viz = ca.recommend(rows)
    assert viz["chart_type"] == "bar"
    assert viz["labels"] == ["A", "B", "C"]
    assert viz["datasets"][0]["data"] == [100.0, 200.0, 150.0]


def test_percentage_like_values_produce_pie_chart():
    rows = [
        {"Category": "A", "PctShare": 40},
        {"Category": "B", "PctShare": 35},
        {"Category": "C", "PctShare": 25},
    ]
    viz = ca.recommend(rows)
    assert viz["chart_type"] == "pie"


def test_percent_in_column_name_is_detected_even_without_summing_to_100():
    # "Percentage" doesn't contain the whole word "percent" with a
    # trailing boundary -- regression test for the regex fix.
    rows = [{"Region": "A", "PercentageContribution": 6.17}, {"Region": "B", "PercentageContribution": 93.83}]
    viz = ca.recommend(rows)
    assert viz["chart_type"] == "pie"


def test_two_numeric_columns_produce_scatter():
    rows = [{"FamilyMembers": f, "MonthlyIncome": m} for f, m in [(2, 3000), (4, 1500), (1, 6000)]]
    viz = ca.recommend(rows)
    assert viz["chart_type"] == "scatter"
    assert viz["datasets"][0]["data"][0] == {"x": 2.0, "y": 3000.0}


def test_temporal_name_and_numeric_produces_line_chart():
    rows = [{"DonationQuarter": q, "TotalDonation": t} for q, t in [(1, 107000), (2, 148000), (3, 93000), (4, 57000)]]
    viz = ca.recommend(rows)
    assert viz["chart_type"] == "line"


def test_decomposed_year_and_month_columns_combine_into_one_time_axis():
    # Regression: GROUP BY YEAR(x), MONTH(x) returns two separate integer
    # columns, not one date string -- both must be recognized as temporal
    # by NAME (DonationYear/DonationMonth), not by looking like a date
    # value, and joined into one composite x-axis label.
    rows = [
        {"DonationYear": 2025, "DonationMonth": 3, "TotalDonationAmount": "37000.00"},
        {"DonationYear": 2025, "DonationMonth": 6, "TotalDonationAmount": "18000.00"},
        {"DonationYear": 2025, "DonationMonth": 9, "TotalDonationAmount": "85000.00"},
    ]
    viz = ca.recommend(rows)
    assert viz["chart_type"] == "line"
    # Label order follows column order in the result (DonationYear then
    # DonationMonth here); it's a composite of whichever temporal columns
    # are present, not a fixed year-then-month or month-then-year rule.
    assert viz["labels"] == ["2025-3", "2025-6", "2025-9"]


def test_cached_decimal_as_string_is_still_treated_as_numeric():
    # Regression: query_cache.py stores Decimal as str(...) via
    # json.dumps(default=str); after a cache hit, "100000.00" is a
    # plain string, not a Decimal. Must still be chartable.
    rows = [{"CampaignName": "A", "TotalDonation": "100000.00"}, {"CampaignName": "B", "TotalDonation": "85000.00"}]
    viz = ca.recommend(rows)
    assert viz is not None
    assert viz["datasets"][0]["data"] == [100000.0, 85000.0]


def test_phone_number_string_is_not_treated_as_numeric():
    # Regression: an all-digit string like a phone number must NOT be
    # classified numeric just because it round-tripped through a cache
    # as a string -- only a string WITH a decimal point (matching how
    # DECIMAL columns actually serialize) counts.
    rows = [{"FullName": "Ahmed", "Mobile": "0501112223"}, {"FullName": "Sara", "Mobile": "0502223334"}]
    assert ca.recommend(rows) is None


def test_id_like_integer_column_is_still_numeric_known_limitation():
    # Documents a known, accepted limitation (see code review): an int
    # column can't be distinguished from a real measurement by type
    # alone, so two numeric-looking columns produce a scatter plot even
    # when one of them is really an identifier.
    rows = [{"CampaignId": i, "TotalDonations": i * 1000} for i in range(1, 5)]
    viz = ca.recommend(rows)
    assert viz["chart_type"] == "scatter"


def test_too_many_categories_declines_to_chart():
    rows = [{"City": f"City{i}", "Total": i * 10} for i in range(30)]
    assert ca.recommend(rows) is None


def test_forced_bar_type_overrides_the_normal_rule():
    rows = [{"CampaignName": "A", "Total": 100}, {"CampaignName": "B", "Total": 200}]
    assert ca.recommend(rows)["chart_type"] == "bar"
    assert ca.recommend(rows, forced_type="pie")["chart_type"] == "pie"
    assert ca.recommend(rows, forced_type="line")["chart_type"] == "line"


def test_forced_doughnut_aliases_to_pie():
    rows = [{"CampaignName": "A", "Total": 100}, {"CampaignName": "B", "Total": 200}]
    assert ca.recommend(rows, forced_type="doughnut")["chart_type"] == "pie"


def test_forced_type_on_single_numeric_column_falls_back_to_row_position_labels():
    rows = [{"Amount": 10}, {"Amount": 20}, {"Amount": 15}]
    assert ca.recommend(rows) is None  # normal auto-suggestion declines
    forced = ca.recommend(rows, forced_type="bar")
    assert forced["chart_type"] == "bar"
    assert forced["labels"] == ["Row 1", "Row 2", "Row 3"]


def test_forced_scatter_declines_gracefully_when_data_does_not_support_it():
    # Only one numeric column -- forced scatter can't work, so it should
    # fall through to whatever the normal advisor picks rather than
    # returning nothing outright.
    rows = [{"City": "Dubai", "Count": 5}, {"City": "Sharjah", "Count": 2}]
    viz = ca.recommend(rows, forced_type="scatter")
    assert viz["chart_type"] == "bar"


def test_auto_mode_falls_back_to_composite_axis_for_multi_dimension_data():
    # Regression: an EXPLICIT "chart it"/"show me a graph" request
    # (forced_type="auto") on a result with more than one grouping
    # dimension (donor x year x quarter) must still produce a chart --
    # the normal automatic-suggestion rules correctly decline here
    # (multiple grouping columns), but an explicit ask should not.
    rows = [
        {"DonorName": "Ahmed Ali", "DonationYear": 2024, "DonationQuarter": 1, "TotalDonation": "10000.00"},
        {"DonorName": "Sara Khalid", "DonationYear": 2024, "DonationQuarter": 3, "TotalDonation": "8000.00"},
    ]
    assert ca.recommend(rows, forced_type=None) is None
    viz = ca.recommend(rows, forced_type="auto")
    assert viz["chart_type"] == "bar"
    assert viz["labels"] == ["Ahmed Ali · 2024 · 1", "Sara Khalid · 2024 · 3"]


def test_auto_mode_prefers_normal_rules_when_they_apply():
    rows = [{"CampaignName": "A", "Total": 100}, {"CampaignName": "B", "Total": 200}]
    viz = ca.recommend(rows, forced_type="auto")
    assert viz["chart_type"] == "bar"
    assert viz["labels"] == ["A", "B"]


def test_auto_mode_declines_only_when_there_is_no_numeric_measure_at_all():
    rows = [{"City": "Dubai", "Country": "UAE"}, {"City": "Sharjah", "Country": "UAE"}]
    assert ca.recommend(rows, forced_type="auto") is None
