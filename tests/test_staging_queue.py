"""staging_queue.py -- the admin review worklist."""

import staging_queue as sq


def test_log_entry_returns_new_id():
    entry_id = sq.log_entry("q", "admin", None, "SELECT 1", "Approved", "ok")
    assert isinstance(entry_id, int)


def test_get_entry_round_trips():
    entry_id = sq.log_entry("show donors", "admin", None, "SELECT * FROM Donors", "Approved", "0 retries")
    entry = sq.get_entry(entry_id)
    assert entry["question"] == "show donors"
    assert entry["sql"] == "SELECT * FROM Donors"
    assert entry["status"] == "Approved"
    assert entry["role_name"] == "admin"


def test_get_entry_missing_returns_none():
    assert sq.get_entry(9999) is None


def test_get_queue_orders_newest_first():
    sq.log_entry("first", "admin", None, "SELECT 1", "Approved")
    sq.log_entry("second", "admin", None, "SELECT 2", "Approved")
    queue = sq.get_queue()
    assert queue[0]["question"] == "second"
    assert queue[1]["question"] == "first"


def test_mark_promoted_updates_status():
    entry_id = sq.log_entry("q", "admin", None, "SELECT 1", "Approved")
    sq.mark_promoted(entry_id)
    assert sq.get_entry(entry_id)["status"] == "Promoted"


def test_donor_scoped_entry_tracks_role_and_donor_id():
    entry_id = sq.log_entry("my donations", "donor", 7, "SELECT * FROM Donations WHERE DonorId=7", "Approved")
    entry = sq.get_entry(entry_id)
    assert entry["role_name"] == "donor"
    assert entry["donor_id"] == 7


def test_blocked_entry_has_no_sql():
    entry_id = sq.log_entry("bad input", "admin", None, None, "Blocked", "Blocked by input security fence")
    entry = sq.get_entry(entry_id)
    assert entry["sql"] is None
    assert entry["status"] == "Blocked"
