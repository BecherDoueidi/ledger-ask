"""
query_analytics.py -- the complete observability log (every request,
every path, success or failure) and its aggregate summary endpoint.
"""

import query_analytics as qa


def _log(path, success, **kwargs):
    timer = qa.QueryTimer()
    qa.log_query("question", "admin", None, path=path, success=success, timer=timer, **kwargs)


class TestQueryTimer:
    def test_phase_accumulates_across_multiple_entries(self):
        timer = qa.QueryTimer()
        with timer.phase("llm"):
            pass
        with timer.phase("llm"):
            pass
        assert timer.ms("llm") is not None
        assert timer.ms("llm") >= 0

    def test_unmeasured_phase_is_none(self):
        timer = qa.QueryTimer()
        assert timer.ms("never_entered") is None

    def test_total_ms_is_nonnegative(self):
        timer = qa.QueryTimer()
        assert timer.total_ms() >= 0


class TestLogAndSummary:
    def test_summary_on_empty_log(self):
        summary = qa.get_summary()
        assert summary["total_requests"] == 0
        assert summary["cache_hit_rate"] == 0
        assert summary["success_rate"] == 0

    def test_summary_counts_totals_and_rates(self):
        _log("llm", True, generated_sql="SELECT 1", rows_returned=1)
        _log("cache", True, generated_sql="SELECT 1", rows_returned=1)
        _log("blocked", False, failure_reason="Blocked by input security fence")

        summary = qa.get_summary()
        assert summary["total_requests"] == 3
        assert summary["cache_hits"] == 1
        assert summary["llm_calls"] == 1
        assert summary["successes"] == 2
        assert summary["failures"] == 1
        assert summary["cache_hit_rate"] == round(1 / 3, 3)
        assert summary["success_rate"] == round(2 / 3, 3)

    def test_summary_breaks_down_by_path(self):
        _log("llm", True)
        _log("llm", True)
        _log("cache", True)
        by_path = {row["path"]: row["count"] for row in qa.get_summary()["by_path"]}
        assert by_path == {"llm": 2, "cache": 1}

    def test_summary_top_failure_reasons(self):
        _log("blocked", False, failure_reason="bad input")
        _log("blocked", False, failure_reason="bad input")
        _log("error", False, failure_reason="db down")
        top = qa.get_summary()["top_failure_reasons"]
        assert top[0] == {"reason": "bad input", "count": 2}

    def test_get_recent_orders_newest_first(self):
        _log("llm", True, generated_sql="first")
        _log("llm", True, generated_sql="second")
        recent = qa.get_recent(limit=10)
        assert recent[0]["generated_sql"] == "second"
        assert recent[1]["generated_sql"] == "first"

    def test_get_recent_filters_by_path(self):
        _log("llm", True)
        _log("cache", True)
        recent = qa.get_recent(path="cache")
        assert len(recent) == 1
        assert recent[0]["path"] == "cache"

    def test_get_recent_filters_by_success(self):
        _log("llm", True)
        _log("llm", False, failure_reason="x")
        assert len(qa.get_recent(success=False)) == 1
        assert len(qa.get_recent(success=True)) == 1

    def test_get_recent_respects_limit(self):
        for _ in range(5):
            _log("llm", True)
        assert len(qa.get_recent(limit=2)) == 2
