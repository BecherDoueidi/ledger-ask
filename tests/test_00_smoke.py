"""Sanity checks for the fixture infrastructure itself -- if these fail,
every other test's failure is probably noise from this, not a real bug."""


def test_client_can_login_as_seeded_admin(client):
    response = login_helper(client)
    assert response.status_code == 302


def login_helper(client):
    return client.post("/login", data={"username": "admin", "password": "admin123"})


def test_business_engine_has_seed_data(business_engine):
    from sqlalchemy import text
    with business_engine.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*) FROM Donors")).fetchone()
        assert rows[0] == 2


def test_fake_llm_intercepts_call(app_module, fake_llm):
    fake_llm.returns("SELECT 1")
    result = app_module.call_llm_api("sys", "user query")
    assert result == "SELECT 1"
    assert fake_llm.calls == [("sys", "user query", 0.0)]
