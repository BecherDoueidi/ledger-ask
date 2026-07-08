"""
roles_config.py -- the single source of truth for per-role data/app
access. resolve_allowed_tables is the key piece of this session's
"adapt to a different database" work: for a row-filtered role it must
NOT read allowed_tables from static config, it must ask
schema_harvester what's live right now (see that function's docstring).
"""

import roles_config
import schema_harvester


class TestGetRole:
    def test_known_role_returns_config(self):
        assert roles_config.get_role("admin")["label"].startswith("Admin")

    def test_unknown_role_returns_none(self):
        assert roles_config.get_role("superuser") is None


class TestHasCapability:
    def test_true_when_role_has_flag_set(self):
        assert roles_config.has_capability("admin", "can_manage_users") is True

    def test_false_when_role_lacks_flag(self):
        assert roles_config.has_capability("viewer", "can_manage_users") is False

    def test_false_for_unknown_role(self):
        assert roles_config.has_capability("superuser", "can_manage_users") is False


class TestIsRowRestricted:
    def test_true_for_donor(self):
        assert roles_config.is_row_restricted("donor") is True

    def test_false_for_unrestricted_roles(self):
        assert roles_config.is_row_restricted("viewer") is False
        assert roles_config.is_row_restricted("analyst") is False
        assert roles_config.is_row_restricted("admin") is False

    def test_false_for_unknown_role(self):
        assert roles_config.is_row_restricted("superuser") is False


class TestResolveAllowedTables:
    def test_unrestricted_role_returns_static_none_without_touching_schema(self, monkeypatch):
        # Must NOT call schema_harvester at all for an unrestricted role
        # -- touching the DB here would be pure waste on every request.
        def _boom(column_name):
            raise AssertionError("should not be called for an unrestricted role")
        monkeypatch.setattr(schema_harvester, "discover_row_scoped_tables", _boom)
        assert roles_config.resolve_allowed_tables("admin") is None
        assert roles_config.resolve_allowed_tables("viewer") is None
        assert roles_config.resolve_allowed_tables("analyst") is None

    def test_row_filtered_role_delegates_to_live_discovery(self, monkeypatch):
        calls = []
        def _fake_discover(column_name):
            calls.append(column_name)
            return ["Contributions", "Sponsorships"]
        monkeypatch.setattr(schema_harvester, "discover_row_scoped_tables", _fake_discover)
        assert roles_config.resolve_allowed_tables("donor") == ["Contributions", "Sponsorships"]
        assert calls == ["DonorId"]

    def test_unknown_role_returns_none(self):
        assert roles_config.resolve_allowed_tables("superuser") is None
