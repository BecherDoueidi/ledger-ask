"""
catalog_manager.py -- the SQLite-backed, versioned catalog registry.
Promoting stages an entry as 'pending'; it has no effect on find_match()
until a separate approve_entry() call activates it. See the module's
own docstring for why that two-step gate exists.
"""

import catalog_manager as cm


def test_find_match_returns_none_when_nothing_promoted():
    assert cm.find_match("anything") is None


def test_pending_entry_does_not_match_until_approved():
    entry_id = cm.promote("how many donors", "SELECT COUNT(*) FROM donors")
    assert cm.find_match("how many donors") is None
    assert cm.approve_entry(entry_id) is True
    assert cm.find_match("how many donors") == "SELECT COUNT(*) FROM donors"


def test_find_match_is_case_and_whitespace_insensitive():
    entry_id = cm.promote("How many donors", "SELECT COUNT(*) FROM donors")
    cm.approve_entry(entry_id)
    assert cm.find_match("  HOW    many DONORS  ") == "SELECT COUNT(*) FROM donors"


def test_find_match_does_not_match_a_different_question():
    entry_id = cm.promote("how many donors", "SELECT COUNT(*) FROM donors")
    cm.approve_entry(entry_id)
    assert cm.find_match("how many volunteers") is None


def test_multiple_active_entries_coexist():
    id1 = cm.promote("first question", "SELECT 1")
    id2 = cm.promote("second question", "SELECT 2")
    cm.approve_entry(id1)
    cm.approve_entry(id2)
    assert cm.find_match("first question") == "SELECT 1"
    assert cm.find_match("second question") == "SELECT 2"


class TestVersioning:
    def test_repromoting_the_same_intent_increments_version(self):
        id1 = cm.promote("how many donors", "SELECT COUNT(*) FROM donors")
        id2 = cm.promote("how many donors", "SELECT COUNT(DonorId) FROM Donors")
        assert cm.get_entry(id1)["version"] == 1
        assert cm.get_entry(id2)["version"] == 2

    def test_approving_a_new_version_supersedes_the_previous_active_one(self):
        id1 = cm.promote("how many donors", "SELECT COUNT(*) FROM donors")
        cm.approve_entry(id1)
        assert cm.find_match("how many donors") == "SELECT COUNT(*) FROM donors"

        id2 = cm.promote("how many donors", "SELECT COUNT(DonorId) FROM Donors")
        # Not yet approved -- the old version is still what's live.
        assert cm.find_match("how many donors") == "SELECT COUNT(*) FROM donors"

        cm.approve_entry(id2)
        assert cm.find_match("how many donors") == "SELECT COUNT(DonorId) FROM Donors"
        assert cm.get_entry(id1)["status"] == "superseded"
        assert cm.get_entry(id2)["status"] == "active"


class TestApproveAndReject:
    def test_approve_records_who_and_when(self):
        entry_id = cm.promote("q", "SELECT 1")
        cm.approve_entry(entry_id, approved_by="admin")
        entry = cm.get_entry(entry_id)
        assert entry["approved_by"] == "admin"
        assert entry["approved_at"] is not None

    def test_approving_a_nonexistent_entry_returns_false(self):
        assert cm.approve_entry(9999) is False

    def test_approving_an_already_approved_entry_returns_false(self):
        entry_id = cm.promote("q", "SELECT 1")
        cm.approve_entry(entry_id)
        assert cm.approve_entry(entry_id) is False

    def test_reject_marks_status_and_stores_reason(self):
        entry_id = cm.promote("q", "SELECT 1")
        assert cm.reject_entry(entry_id, reason="bad SQL") is True
        entry = cm.get_entry(entry_id)
        assert entry["status"] == "rejected"
        assert entry["notes"] == "bad SQL"

    def test_rejected_entry_never_matches(self):
        entry_id = cm.promote("how many donors", "SELECT COUNT(*) FROM donors")
        cm.reject_entry(entry_id)
        assert cm.find_match("how many donors") is None

    def test_rejecting_an_already_rejected_entry_returns_false(self):
        entry_id = cm.promote("q", "SELECT 1")
        cm.reject_entry(entry_id)
        assert cm.reject_entry(entry_id) is False


class TestListEntries:
    def test_list_entries_newest_first(self):
        cm.promote("first", "SELECT 1")
        cm.promote("second", "SELECT 2")
        entries = cm.list_entries()
        assert entries[0]["intent"] == "second"
        assert entries[1]["intent"] == "first"

    def test_list_entries_filters_by_status(self):
        id1 = cm.promote("first", "SELECT 1")
        cm.promote("second", "SELECT 2")
        cm.approve_entry(id1)
        pending = cm.list_entries(status="pending")
        active = cm.list_entries(status="active")
        assert [e["intent"] for e in pending] == ["second"]
        assert [e["intent"] for e in active] == ["first"]


class TestLegacyYamlMigration:
    def test_migrates_existing_yaml_entries_as_active_on_first_use(self, tmp_path, monkeypatch):
        yaml_path = tmp_path / "catalog.yaml"
        yaml_path.write_text(
            "promoted_queries:\n"
            "  - intent: \"how many donors\"\n"
            "    sql: SELECT COUNT(*) FROM donors\n"
        )
        monkeypatch.setattr(cm, "_LEGACY_YAML_PATH", str(yaml_path))

        # No promote() call at all -- the migration must happen purely
        # from the legacy file existing, on first real use of the store.
        assert cm.find_match("how many donors") == "SELECT COUNT(*) FROM donors"

    def test_migration_is_a_noop_once_the_store_already_has_entries(self, tmp_path, monkeypatch):
        yaml_path = tmp_path / "catalog.yaml"
        monkeypatch.setattr(cm, "_LEGACY_YAML_PATH", str(yaml_path))

        cm.promote("real entry", "SELECT 1")
        # Legacy file appears only AFTER the store already has a row --
        # must never overwrite/duplicate on top of real usage.
        yaml_path.write_text(
            "promoted_queries:\n"
            "  - intent: \"should never appear\"\n"
            "    sql: SELECT 999\n"
        )
        assert cm.find_match("should never appear") is None
