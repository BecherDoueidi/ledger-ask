"""
query_cache.py -- exact-match + semantic-fallback lookup, scoping by
(role_name, donor_id), and the security-critical invariant that a cache
hit is never a shortcut around access control (that re-check lives in
app.py, but the cache_key plumbing this depends on is tested here).
"""

import query_cache as qc
import embeddings


def test_set_then_get_cached_exact_match():
    qc.set_cached("How many donors?", "admin", None, "SELECT COUNT(*) FROM Donors", [{"n": 5}])
    result = qc.get_cached("How many donors?", "admin", None)
    assert result is not None
    assert result["generated_sql"] == "SELECT COUNT(*) FROM Donors"
    assert result["data"] == [{"n": 5}]
    assert result["match_type"] == "exact"


def test_get_cached_is_normalized_case_and_whitespace_insensitive():
    qc.set_cached("How many donors?", "admin", None, "SELECT COUNT(*) FROM Donors", [{"n": 5}])
    result = qc.get_cached("  how   MANY donors?  ", "admin", None)
    assert result is not None


def test_get_cached_miss_returns_none():
    assert qc.get_cached("never asked before", "admin", None) is None


def test_hit_count_increments_on_each_get():
    qc.set_cached("q", "admin", None, "SELECT 1", [])
    assert qc.get_cached("q", "admin", None)["hit_count"] == 1
    assert qc.get_cached("q", "admin", None)["hit_count"] == 2


def test_cache_is_scoped_per_role():
    qc.set_cached("show my data", "admin", None, "SELECT * FROM AdminView", [{"x": 1}])
    # A donor asking the identical text must NOT see the admin's cached
    # answer -- this is the core security invariant of the whole module.
    assert qc.get_cached("show my data", "donor", 1) is None


def test_cache_is_scoped_per_donor():
    qc.set_cached("show my donations", "donor", 1, "SELECT * FROM Donations WHERE DonorId=1", [{"amt": 100}])
    assert qc.get_cached("show my donations", "donor", 2) is None
    assert qc.get_cached("show my donations", "donor", 1) is not None


def test_invalidate_by_key_removes_entry():
    qc.set_cached("q", "admin", None, "SELECT 1", [])
    cached = qc.get_cached("q", "admin", None)
    qc.invalidate_by_key(cached["cache_key"])
    assert qc.get_cached("q", "admin", None) is None


def test_invalidate_by_text_removes_entry():
    qc.set_cached("q", "admin", None, "SELECT 1", [])
    qc.invalidate("q", "admin", None)
    assert qc.get_cached("q", "admin", None) is None


def test_invalidate_nonexistent_key_does_not_raise():
    qc.invalidate_by_key("not-a-real-key")


def test_clear_cache_wipes_everything():
    qc.set_cached("q1", "admin", None, "SELECT 1", [])
    qc.set_cached("q2", "donor", 1, "SELECT 2", [])
    qc.clear_cache()
    assert qc.get_cached("q1", "admin", None) is None
    assert qc.get_cached("q2", "donor", 1) is None


def test_clear_cache_by_role_only_clears_that_role():
    qc.set_cached("q1", "admin", None, "SELECT 1", [])
    qc.set_cached("q2", "donor", 1, "SELECT 2", [])
    qc.clear_cache(role_name="donor")
    assert qc.get_cached("q1", "admin", None) is not None
    assert qc.get_cached("q2", "donor", 1) is None


def test_decimal_values_survive_the_json_round_trip_as_strings():
    from decimal import Decimal
    qc.set_cached("total", "admin", None, "SELECT SUM(x)", [{"total": Decimal("100.00")}])
    result = qc.get_cached("total", "admin", None)
    # json.dumps(default=str) is what makes Decimal serializable at all;
    # the tradeoff (money values come back as strings) is exactly what
    # chart_advisor.py's numeric-string handling exists to work around.
    assert result["data"][0]["total"] == "100.00"


class TestSemanticFallback:
    def test_skips_semantic_lookup_when_embeddings_unavailable(self, monkeypatch):
        # conftest's no_real_embeddings fixture already does this, but
        # asserting it explicitly documents the graceful-degradation
        # contract: get_cached_or_similar must not raise or hang, just
        # miss, when the embedding model isn't available.
        monkeypatch.setattr(embeddings, "get_embedding", lambda text: None)
        qc.set_cached("Show all employees", "admin", None, "SELECT * FROM Employees", [])
        assert qc.get_cached_or_similar("Can you list every employee?", "admin", None) is None

    def test_semantic_match_hits_on_paraphrase_with_fake_embeddings(self, monkeypatch):
        # Deterministic fake: same text -> same vector, so a genuine
        # paraphrase (different text) needs an explicit vector map.
        vectors = {
            "Show all employees": [1.0, 0.0, 0.0],
            "Can you list every employee?": [0.95, 0.05, 0.0],
        }
        monkeypatch.setattr(embeddings, "get_embedding", lambda text: vectors.get(text, [0, 0, 1]))
        qc.set_cached("Show all employees", "admin", None, "SELECT * FROM Employees", [{"n": 1}])
        result = qc.get_cached_or_similar("Can you list every employee?", "admin", None)
        assert result is not None
        assert result["match_type"] == "semantic"
        assert result["generated_sql"] == "SELECT * FROM Employees"

    def test_semantic_match_respects_entity_guard_even_with_high_cosine(self, monkeypatch):
        # "top 5" and "top 10" would embed almost identically in a real
        # model -- the entity guard must reject this regardless of the
        # (here, deliberately identical) embedding vectors.
        monkeypatch.setattr(embeddings, "get_embedding", lambda text: [1.0, 0.0])
        qc.set_cached("top 5 donors", "admin", None, "SELECT * FROM Donors LIMIT 5", [])
        assert qc.get_cached_or_similar("top 10 donors", "admin", None) is None

    def test_semantic_match_never_crosses_role_or_donor_scope(self, monkeypatch):
        monkeypatch.setattr(embeddings, "get_embedding", lambda text: [1.0, 0.0, 0.0])
        qc.set_cached("show my donations", "donor", 1, "SELECT * FROM Donations WHERE DonorId=1", [])
        assert qc.get_cached_or_similar("show my donations please", "donor", 2) is None

    def test_exact_match_is_tried_before_semantic_and_short_circuits(self, monkeypatch):
        calls = []

        def tracking_embed(text):
            calls.append(text)
            return [1.0, 0.0]

        monkeypatch.setattr(embeddings, "get_embedding", tracking_embed)
        qc.set_cached("q", "admin", None, "SELECT 1", [])
        calls.clear()  # ignore the embedding call made by set_cached itself
        qc.get_cached_or_similar("q", "admin", None)
        # An exact hit must never even call the embedding model.
        assert calls == []
