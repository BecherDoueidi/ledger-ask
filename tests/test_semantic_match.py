"""
semantic_match.py -- the entity guard, content-word guard, and cosine
threshold that decide whether a paraphrased question hits the same
cache entry. The thresholds (SIMILARITY_THRESHOLD=0.80,
CONTENT_OVERLAP_THRESHOLD=0.5) were calibrated against a real embedding
model, not guessed -- these tests use synthetic vectors (no live Ollama
dependency) but assert the same accept/reject decisions that
calibration run produced.
"""

import semantic_match as sm


class TestEntitySignature:
    def test_extracts_bare_numbers(self):
        assert sm.extract_entity_signature("top 5 donors") == frozenset({"num:5"})

    def test_extracts_quarter(self):
        assert "quarter:q1" in sm.extract_entity_signature("donations in Q1")

    def test_extracts_relative_time(self):
        assert "time:this year" in sm.extract_entity_signature("total donations this year")
        assert "time:last year" in sm.extract_entity_signature("total donations last year")

    def test_no_entities_is_empty_set(self):
        assert sm.extract_entity_signature("show all employees") == frozenset()

    def test_this_year_and_last_year_are_incompatible(self):
        sig_a = sm.extract_entity_signature("total donations this year")
        sig_b = sm.extract_entity_signature("total donations last year")
        assert not sm.entity_signatures_compatible(sig_a, sig_b)

    def test_top_5_and_top_10_are_incompatible(self):
        sig_a = sm.extract_entity_signature("top 5 donors")
        sig_b = sm.extract_entity_signature("top 10 donors")
        assert not sm.entity_signatures_compatible(sig_a, sig_b)

    def test_identical_entity_free_questions_are_compatible(self):
        sig_a = sm.extract_entity_signature("show all employees")
        sig_b = sm.extract_entity_signature("can you list every employee")
        assert sm.entity_signatures_compatible(sig_a, sig_b)


class TestContentWords:
    def test_strips_generic_scaffolding(self):
        assert sm.extract_content_words("Show all employees") == frozenset({"employee"})

    def test_plural_stripping_lets_paraphrases_match(self):
        a = sm.extract_content_words("Show all employees")
        b = sm.extract_content_words("Can you list every employee?")
        assert a == b == frozenset({"employee"})

    def test_multi_word_topic_overlap(self):
        a = sm.extract_content_words("Can you list donor 1's donations?")
        b = sm.extract_content_words("Show me donations from donor 1")
        assert a == b == frozenset({"donor", "donation"})


class TestContentOverlapScore:
    def test_identical_sets_score_one(self):
        assert sm.content_overlap_score(frozenset({"donor"}), frozenset({"donor"})) == 1.0

    def test_both_empty_is_compatible(self):
        assert sm.content_overlap_score(frozenset(), frozenset()) == 1.0

    def test_topic_drift_scores_low(self):
        # "donations by campaign" vs "donations by city" -- real
        # topic-drift pair from the calibration run (cosine 0.81, would
        # have been accepted on cosine alone; this guard is what rejects it)
        a = frozenset({"donation", "campaign"})
        b = frozenset({"donation", "city"})
        score = sm.content_overlap_score(a, b)
        assert score < sm.CONTENT_OVERLAP_THRESHOLD

    def test_genuine_paraphrase_clears_threshold(self):
        a = frozenset({"donation", "campaign"})
        b = frozenset({"donation", "campaign", "grouped"})
        score = sm.content_overlap_score(a, b)
        assert score >= sm.CONTENT_OVERLAP_THRESHOLD


class TestCosineSimilarity:
    def test_identical_vectors(self):
        assert sm.cosine_similarity([1, 2, 3], [1, 2, 3]) == 1.0

    def test_orthogonal_vectors(self):
        assert sm.cosine_similarity([1, 0], [0, 1]) == 0.0

    def test_mismatched_lengths_returns_zero_not_an_error(self):
        assert sm.cosine_similarity([1, 2], [1, 2, 3]) == 0.0

    def test_zero_vector_returns_zero_not_a_divide_by_zero_error(self):
        assert sm.cosine_similarity([0, 0], [1, 1]) == 0.0


class TestFindBestMatch:
    def _candidate(self, embedding, text, label):
        return {
            "embedding": embedding,
            "entity_signature": sm.extract_entity_signature(text),
            "content_words": sm.extract_content_words(text),
            "label": label,
        }

    def test_accepts_close_match_with_compatible_entities_and_topic(self):
        candidates = [self._candidate([1, 0, 0], "show all employees", "A")]
        match, score = sm.find_best_match("can you list every employee", [0.99, 0.05, 0], candidates)
        assert match["label"] == "A"
        assert score >= sm.SIMILARITY_THRESHOLD

    def test_rejects_high_cosine_when_entities_differ(self):
        # Simulates the "this year" vs "last year" danger case: even a
        # near-perfect cosine score must not override the entity guard.
        candidates = [self._candidate([1, 0, 0], "total donations last year", "A")]
        match, score = sm.find_best_match("total donations this year", [1, 0, 0], candidates)
        assert match is None
        assert score == 0.0

    def test_rejects_high_cosine_when_topic_differs(self):
        candidates = [self._candidate([1, 0, 0], "donations by city", "A")]
        match, score = sm.find_best_match("donations by campaign", [1, 0, 0], candidates)
        assert match is None

    def test_rejects_below_similarity_threshold(self):
        candidates = [self._candidate([1, 0], "show all employees", "A")]
        match, score = sm.find_best_match("show all employees", [0, 1], candidates)
        assert match is None

    def test_no_candidates_returns_none(self):
        match, score = sm.find_best_match("anything", [1, 0], [])
        assert match is None and score == 0.0

    def test_picks_highest_scoring_of_multiple_qualifying_candidates(self):
        candidates = [
            self._candidate([0.85, 0.1, 0], "show all employees", "lower"),
            self._candidate([0.99, 0.02, 0], "show all employees", "higher"),
        ]
        match, score = sm.find_best_match("can you list every employee", [1, 0, 0], candidates)
        assert match["label"] == "higher"
