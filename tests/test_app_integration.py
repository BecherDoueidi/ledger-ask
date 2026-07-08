"""
End-to-end tests against the real /api/generate-sql pipeline via
Flask's test client, with only the LLM call and the business database
swapped for controllable fakes (see conftest.py). Everything else --
session auth, the security matrix, the catalog/cache/LLM routing, the
self-healing retry loop, and CRITICALLY the row-level filter -- runs
for real.

The row-level-filter tests are the most important ones in this file:
they're the only tests in the whole suite that prove a donor's SQL
actually gets rewritten and executed with their own row restriction
applied, end to end, rather than just asserting access_control.py's
string-rewriting logic in isolation (see test_access_control.py).
"""

import json

from sqlalchemy import text as sql_text

from conftest import login


def test_unauthenticated_request_is_rejected(client):
    response = client.post("/api/generate-sql", json={"query": "how many donors"})
    assert response.status_code == 401


def test_missing_query_field_is_a_400(client):
    login(client, "admin", "admin123")
    response = client.post("/api/generate-sql", json={})
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "INVALID_REQUEST"


def test_security_matrix_blocks_malicious_input(client, fake_llm):
    login(client, "admin", "admin123")
    response = client.post("/api/generate-sql", json={"query": "how many donors; DROP TABLE Donors"})
    assert response.status_code == 403
    assert response.get_json()["error_code"] == "SECURITY_VIOLATION"
    assert fake_llm.calls == []  # never even reached the LLM


def test_client_cannot_forge_admin_role_via_request_body(client, fake_llm, business_engine):
    # Regression guard for the exact vulnerability auth.py's docstring
    # describes: role/donor_id must come from the server-side session,
    # never from the client's JSON payload.
    login(client, "donor1", "donor123")
    fake_llm.returns("SELECT * FROM Beneficiaries")
    response = client.post(
        "/api/generate-sql",
        json={"query": "show beneficiaries", "role": "admin", "donor_id": None},
    )
    assert response.status_code == 403
    assert response.get_json()["error_code"] == "ACCESS_DENIED"


def test_catalog_hit_never_calls_the_llm(client, fake_llm):
    import catalog_manager
    catalog_manager.promote("how many donors", "SELECT COUNT(*) FROM Donors")
    login(client, "admin", "admin123")

    response = client.post("/api/generate-sql", json={"query": "how many donors"})
    body = response.get_json()
    assert response.status_code == 200
    assert body["source"] == "catalog"
    assert body["data"] == [{"COUNT(*)": 2}]
    assert fake_llm.calls == []


def test_fresh_llm_success_is_then_served_from_cache(client, fake_llm):
    login(client, "admin", "admin123")
    fake_llm.returns("SELECT COUNT(*) FROM Donors")

    first = client.post("/api/generate-sql", json={"query": "how many donors are there"})
    assert first.status_code == 200
    assert first.get_json()["cached"] is False
    assert len(fake_llm.calls) == 1

    second = client.post("/api/generate-sql", json={"query": "how many donors are there"})
    assert second.status_code == 200
    assert second.get_json()["cached"] is True
    assert second.get_json()["match_type"] == "exact"
    assert len(fake_llm.calls) == 1  # cache hit must not call the LLM again


def test_non_sql_llm_output_is_rejected_without_entering_retry_loop(client, fake_llm):
    login(client, "admin", "admin123")
    fake_llm.returns("I'm not sure what you're asking.")
    response = client.post("/api/generate-sql", json={"query": "hi"})
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "NOT_A_QUERY"
    assert len(fake_llm.calls) == 1  # short-circuited, no retries burned on non-SQL


def test_markdown_fenced_sql_is_sanitized_and_succeeds(client, fake_llm):
    login(client, "admin", "admin123")
    fake_llm.returns("```sql\nSELECT COUNT(*) FROM Donors\n```")
    response = client.post("/api/generate-sql", json={"query": "how many donors"})
    assert response.status_code == 200
    assert response.get_json()["generated_sql"] == "SELECT COUNT(*) FROM Donors"


def test_self_healing_retry_succeeds_on_second_attempt(client, fake_llm):
    login(client, "admin", "admin123")
    fake_llm.returns_sequence([
        "SELECT NonExistentColumn FROM Donors",
        "SELECT FullName FROM Donors",
    ])
    response = client.post("/api/generate-sql", json={"query": "show donor names"})
    body = response.get_json()
    assert response.status_code == 200
    assert body["retries_used"] == 1
    assert body["generated_sql"] == "SELECT FullName FROM Donors"
    assert len(fake_llm.calls) == 2
    # Second attempt must use nonzero temperature (see call_llm_api's
    # docstring on why temp=0 would just regenerate the same wrong SQL).
    assert fake_llm.calls[1][2] > 0


def test_retries_exhausted_returns_500(client, fake_llm):
    login(client, "admin", "admin123")
    fake_llm.returns("SELECT NonExistentColumn FROM Donors")
    response = client.post("/api/generate-sql", json={"query": "show donor names"})
    assert response.status_code == 500
    assert len(fake_llm.calls) == 3  # attempt 0 + 2 retries = max_retries=2


class TestDonorRoleSecurity:
    def test_donor_cannot_reach_a_table_outside_allowed_tables(self, client, fake_llm):
        login(client, "donor1", "donor123")
        fake_llm.returns("SELECT * FROM Beneficiaries")
        response = client.post("/api/generate-sql", json={"query": "show beneficiaries"})
        assert response.status_code == 403
        assert response.get_json()["error_code"] == "ACCESS_DENIED"

    def test_donor_write_operations_are_blocked(self, client, fake_llm):
        login(client, "donor1", "donor123")
        fake_llm.returns("INSERT INTO Donations (DonorId, DonationAmount) VALUES (1, 500)")
        response = client.post("/api/generate-sql", json={"query": "add a donation"})
        assert response.status_code == 403
        assert response.get_json()["error_code"] == "WRITE_NOT_PERMITTED"

    def test_row_level_filter_is_actually_enforced_end_to_end(self, client, fake_llm):
        # THE critical security test in this file: the LLM "forgets" to
        # scope the query to the logged-in donor at all -- if the
        # row-level filter didn't really run, this would return the SUM
        # across ALL donors (100+200+50=350) instead of just donor 1's
        # own rows (100+200=300).
        login(client, "donor1", "donor123")
        fake_llm.returns("SELECT SUM(DonationAmount) FROM Donations")
        response = client.post("/api/generate-sql", json={"query": "how much have I donated"})
        body = response.get_json()
        assert response.status_code == 200
        assert body["data"] == [{"SUM(DonationAmount)": 300.0}]

    def test_different_donors_are_isolated_from_each_other(self, client, fake_llm):
        import auth
        auth.create_user("donor2", "donor123", "donor", 2)

        login(client, "donor1", "donor123")
        fake_llm.returns("SELECT SUM(DonationAmount) FROM Donations")
        donor1_response = client.post("/api/generate-sql", json={"query": "how much have I donated"})
        assert donor1_response.get_json()["data"] == [{"SUM(DonationAmount)": 300.0}]

        client.post("/logout")
        login(client, "donor2", "donor123")
        donor2_response = client.post("/api/generate-sql", json={"query": "how much have I donated"})
        assert donor2_response.get_json()["data"] == [{"SUM(DonationAmount)": 50.0}]

    def test_donor_cache_entry_is_never_visible_to_a_different_donor(self, client, fake_llm):
        import auth
        auth.create_user("donor2", "donor123", "donor", 2)

        login(client, "donor1", "donor123")
        fake_llm.returns("SELECT SUM(DonationAmount) FROM Donations")
        client.post("/api/generate-sql", json={"query": "how much have I donated"})
        client.post("/logout")

        login(client, "donor2", "donor123")
        response = client.post("/api/generate-sql", json={"query": "how much have I donated"})
        # Must be a fresh LLM call (own cache entry), not donor1's cached
        # answer, and must reflect donor2's own (different) total.
        assert response.get_json()["cached"] is False
        assert response.get_json()["data"] == [{"SUM(DonationAmount)": 50.0}]


class TestFollowUpConversation:
    def test_deterministic_sort_never_calls_the_llm(self, client, fake_llm):
        login(client, "admin", "admin123")
        fake_llm.returns("SELECT FullName, Email FROM Donors")
        client.post("/api/generate-sql", json={"query": "show me all donors"})
        assert len(fake_llm.calls) == 1

        response = client.post("/api/generate-sql", json={"query": "sort them by name"})
        body = response.get_json()
        assert response.status_code == 200
        assert body["source"] == "transform"
        assert len(fake_llm.calls) == 1  # still just the original call
        names = [r["FullName"] for r in body["data"]]
        assert names == sorted(names)

    def test_aggregate_follow_up_escalates_to_llm_with_context(self, client, fake_llm):
        login(client, "admin", "admin123")
        fake_llm.returns("SELECT FullName FROM Donors")
        client.post("/api/generate-sql", json={"query": "show me all donors"})

        fake_llm.returns("SELECT FullName, SUM(DonationAmount) AS Total FROM Donors d "
                          "JOIN Donations o ON d.DonorId = o.DonorId GROUP BY FullName")
        response = client.post("/api/generate-sql", json={"query": "now show their total donations"})
        assert response.status_code == 200
        assert len(fake_llm.calls) == 2
        # The follow-up's system prompt must carry the previous
        # question/SQL as context -- this is what build_system_prompt's
        # conversation_context section adds.
        second_system_prompt = fake_llm.calls[1][0]
        assert "show me all donors" in second_system_prompt
        assert "FOLLOW-UP" in second_system_prompt

    def test_new_unrelated_question_does_not_trigger_a_transform(self, client, fake_llm):
        login(client, "admin", "admin123")
        fake_llm.returns("SELECT FullName FROM Donors")
        client.post("/api/generate-sql", json={"query": "show me all donors"})

        fake_llm.returns("SELECT COUNT(*) FROM Donors")
        response = client.post("/api/generate-sql", json={"query": "how many volunteers are there"})
        assert response.get_json().get("source") != "transform"

    def test_clear_conversation_resets_follow_up_context(self, client, fake_llm):
        login(client, "admin", "admin123")
        fake_llm.returns("SELECT FullName FROM Donors")
        client.post("/api/generate-sql", json={"query": "show me all donors"})

        client.post("/api/conversation/clear")

        fake_llm.returns("SELECT FullName FROM Donors ORDER BY FullName")
        response = client.post("/api/generate-sql", json={"query": "sort them by name"})
        # With no active conversation, "sort them by name" is just a
        # fresh (if odd) question -- it must go through the LLM, not
        # silently no-op as a transform with nothing to transform.
        assert response.get_json().get("source") != "transform"


class TestAdminRoutes:
    def test_non_admin_cannot_reach_admin_page(self, client):
        login(client, "donor1", "donor123")
        response = client.get("/admin")
        assert response.status_code in (302, 403)

    def test_admin_can_list_and_create_users(self, client):
        login(client, "admin", "admin123")
        response = client.post(
            "/api/users",
            json={"username": "newadmin", "password": "pw123456", "role": "admin"},
        )
        assert response.status_code == 201
        users = client.get("/api/users").get_json()
        assert any(u["username"] == "newadmin" for u in users)

    def test_promote_rejects_non_admin_originated_entry(self, client, fake_llm):
        login(client, "donor1", "donor123")
        fake_llm.returns("SELECT SUM(DonationAmount) FROM Donations")
        client.post("/api/generate-sql", json={"query": "how much have I donated"})
        client.post("/logout")

        login(client, "admin", "admin123")
        queue = client.get("/api/queue").get_json()
        donor_entry = next(e for e in queue if e["role_name"] == "donor")
        response = client.post(f"/api/promote/{donor_entry['id']}")
        assert response.status_code == 400

    def test_analytics_summary_reflects_requests_made(self, client, fake_llm):
        login(client, "admin", "admin123")
        fake_llm.returns("SELECT COUNT(*) FROM Donors")
        client.post("/api/generate-sql", json={"query": "how many donors"})
        summary = client.get("/api/analytics/summary").get_json()
        assert summary["total_requests"] >= 1


class TestRoleTiers:
    """
    admin used to be one monolithic role; it's now split into
    viewer < analyst < admin (see roles_config.py), each granting a
    different subset of admin-side capabilities while all three still
    have full, unrestricted query access (unlike donor, which is
    row-filtered). These tests pin down the boundary between the tiers.
    """

    def test_viewer_can_query_but_not_reach_admin_panel(self, client, fake_llm):
        login(client, "viewer1", "viewer123")
        fake_llm.returns("SELECT COUNT(*) FROM Donors")
        response = client.post("/api/generate-sql", json={"query": "how many donors"})
        assert response.status_code == 200

        assert client.get("/admin").status_code in (302, 403)
        assert client.get("/api/queue").status_code == 403
        assert client.get("/api/analytics/summary").status_code == 403
        assert client.post("/api/clear-cache").status_code == 403
        assert client.get("/api/users").status_code == 403

    def test_analyst_can_view_admin_panel_and_analytics_but_not_manage_or_mutate(self, client, fake_llm):
        login(client, "analyst1", "analyst123")

        assert client.get("/admin").status_code == 200
        assert client.get("/api/queue").status_code == 200
        assert client.get("/api/analytics/summary").status_code == 200
        assert client.get("/api/analytics/recent").status_code == 200

        # Read-only within the admin panel: cannot manage users, clear
        # the shared cache, or promote entries to the shared catalog.
        assert client.get("/api/users").status_code == 403
        assert client.post("/api/users", json={"username": "x", "password": "pw123456", "role": "viewer"}).status_code == 403
        assert client.post("/api/clear-cache").status_code == 403
        assert client.post("/api/promote/1").status_code == 403

    def test_admin_still_has_every_capability(self, client, fake_llm):
        login(client, "admin", "admin123")
        assert client.get("/admin").status_code == 200
        assert client.get("/api/queue").status_code == 200
        assert client.get("/api/analytics/summary").status_code == 200
        assert client.get("/api/users").status_code == 200
        assert client.post("/api/clear-cache").status_code == 200

    def test_viewer_and_analyst_originated_questions_are_still_catalog_eligible(self, client, fake_llm):
        # Promotion eligibility is judged on whether the ORIGINATING role
        # is row-restricted, not on whether it's literally named "admin"
        # -- viewer/analyst are unrestricted-table roles too, so their
        # questions are just as safe to promote as an admin's.
        login(client, "viewer1", "viewer123")
        fake_llm.returns("SELECT FullName FROM Donors")
        client.post("/api/generate-sql", json={"query": "show me all donors"})
        client.post("/logout")

        login(client, "admin", "admin123")
        queue = client.get("/api/queue").get_json()
        viewer_entry = next(e for e in queue if e["role_name"] == "viewer")
        response = client.post(f"/api/promote/{viewer_entry['id']}")
        assert response.status_code == 200


class TestDatabaseSchemaChange:
    """
    The point of the schema-fingerprint check: if the underlying
    database changes shape after a question was cached (a table gets a
    new column, gets renamed, the whole database gets swapped for a
    different one, ...), a repeat of that same question must NOT
    silently replay the old cached answer -- it should look like a
    fresh cache miss and go through the LLM again.
    """

    def test_cache_hit_before_schema_change(self, client, fake_llm):
        login(client, "admin", "admin123")
        fake_llm.returns("SELECT COUNT(*) FROM Donors")
        client.post("/api/generate-sql", json={"query": "how many donors"})
        assert len(fake_llm.calls) == 1

        response = client.post("/api/generate-sql", json={"query": "how many donors"})
        assert response.get_json()["cached"] is True
        assert len(fake_llm.calls) == 1  # still just the original call

    def test_schema_change_invalidates_the_cache_instead_of_replaying_stale_data(
        self, client, fake_llm, app_module
    ):
        login(client, "admin", "admin123")
        fake_llm.returns("SELECT COUNT(*) FROM Donors")
        client.post("/api/generate-sql", json={"query": "how many donors"})
        client.post("/api/generate-sql", json={"query": "how many donors"})  # cache hit
        assert len(fake_llm.calls) == 1

        # Simulate the database changing shape underneath the app --
        # e.g. a migration ran, or the whole database was swapped.
        with app_module.engine.begin() as connection:
            connection.execute(sql_text("ALTER TABLE Donors ADD COLUMN LoyaltyTier TEXT"))

        fake_llm.returns("SELECT COUNT(*) FROM Donors")
        response = client.post("/api/generate-sql", json={"query": "how many donors"})
        body = response.get_json()
        # Must NOT silently replay the pre-change cached answer: this
        # has to look like a fresh generation, not a cache hit.
        assert body.get("cached") is not True
        assert len(fake_llm.calls) == 2

    def test_donor_role_table_list_adapts_to_a_renamed_table(self, client, fake_llm, app_module):
        # Rename Donations -> Contributions but keep its DonorId column.
        # A donor asking about their contributions must still be able to
        # reach it -- roles_config.py was never told this table's new
        # name, it has to be discovered live.
        with app_module.engine.begin() as connection:
            connection.execute(sql_text("ALTER TABLE Donations RENAME TO Contributions"))

        login(client, "donor1", "donor123")
        fake_llm.returns("SELECT SUM(DonationAmount) FROM Contributions")
        response = client.post("/api/generate-sql", json={"query": "how much have I donated"})
        assert response.status_code == 200
        assert response.get_json()["status"] == "success"
