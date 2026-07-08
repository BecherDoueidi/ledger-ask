"""
conversation_state.py -- per-session follow-up context: save/get,
role+donor scoping (a stale conversation must never be read back under
a DIFFERENT identity), and TTL expiry.
"""

from datetime import datetime, timezone, timedelta

import conversation_state as cs


def test_save_then_get_state_round_trips():
    cs.save_state("conv-1", "admin", None, "show donors", "SELECT * FROM Donors", [{"n": 1}], None)
    state = cs.get_state("conv-1", "admin", None)
    assert state["last_question"] == "show donors"
    assert state["last_sql"] == "SELECT * FROM Donors"
    assert state["rows"] == [{"n": 1}]
    assert state["transform_log"] == []


def test_get_state_missing_conversation_returns_none():
    assert cs.get_state("nonexistent", "admin", None) is None


def test_get_state_with_no_conversation_id_returns_none():
    assert cs.get_state(None, "admin", None) is None
    assert cs.get_state("", "admin", None) is None


def test_get_state_rejects_mismatched_role():
    cs.save_state("conv-1", "admin", None, "q", "SELECT 1", [], None)
    assert cs.get_state("conv-1", "donor", 1) is None


def test_get_state_rejects_mismatched_donor_id():
    # Same role_name, different donor -- e.g. two donor accounts somehow
    # sharing a conversation_id would still not see each other's state.
    cs.save_state("conv-1", "donor", 1, "q", "SELECT 1", [], None)
    assert cs.get_state("conv-1", "donor", 2) is None


def test_save_state_overwrites_previous_turn():
    cs.save_state("conv-1", "admin", None, "first question", "SELECT 1", [{"a": 1}], None)
    cs.save_state("conv-1", "admin", None, "second question", "SELECT 2", [{"b": 2}], None)
    state = cs.get_state("conv-1", "admin", None)
    assert state["last_question"] == "second question"
    assert state["last_sql"] == "SELECT 2"


def test_transform_log_persists_and_defaults_to_empty():
    cs.save_state("conv-1", "admin", None, "q", "SELECT 1", [], None, transform_log=["sorted by X"])
    assert cs.get_state("conv-1", "admin", None)["transform_log"] == ["sorted by X"]

    cs.save_state("conv-1", "admin", None, "q2", "SELECT 2", [], None)
    assert cs.get_state("conv-1", "admin", None)["transform_log"] == []


def test_clear_state_removes_conversation():
    cs.save_state("conv-1", "admin", None, "q", "SELECT 1", [], None)
    cs.clear_state("conv-1")
    assert cs.get_state("conv-1", "admin", None) is None


def test_clear_state_on_missing_conversation_does_not_raise():
    cs.clear_state("nonexistent")
    cs.clear_state(None)


def test_visualization_round_trips():
    viz = {"chart_type": "bar", "labels": ["A"], "datasets": []}
    cs.save_state("conv-1", "admin", None, "q", "SELECT 1", [], viz)
    assert cs.get_state("conv-1", "admin", None)["visualization"] == viz


def test_row_cap_limits_stored_rows(monkeypatch):
    monkeypatch.setattr(cs, "MAX_STORED_ROWS", 3)
    rows = [{"n": i} for i in range(10)]
    cs.save_state("conv-1", "admin", None, "q", "SELECT 1", rows, None)
    assert len(cs.get_state("conv-1", "admin", None)["rows"]) == 3


def test_expired_conversation_is_treated_as_missing(monkeypatch):
    cs.save_state("conv-1", "admin", None, "q", "SELECT 1", [], None)

    # Simulate 31 minutes having passed (TTL is 30) by monkeypatching
    # datetime.now() as seen from inside conversation_state's own module
    # namespace, rather than reaching into the stored row.
    real_datetime = cs.datetime

    class FrozenFuture(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(minutes=31)

    monkeypatch.setattr(cs, "datetime", FrozenFuture)
    assert cs.get_state("conv-1", "admin", None) is None


def test_conversation_within_ttl_is_still_valid(monkeypatch):
    cs.save_state("conv-1", "admin", None, "q", "SELECT 1", [], None)
    real_datetime = cs.datetime

    class FrozenSoon(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(minutes=5)

    monkeypatch.setattr(cs, "datetime", FrozenSoon)
    assert cs.get_state("conv-1", "admin", None) is not None
