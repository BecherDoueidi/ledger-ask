"""
catalog_manager.py -- Path 3's exact-match lookup and the append-only
promote() that preserves the file's existing comments/formatting.
"""

import catalog_manager as cm


def test_find_match_returns_none_when_catalog_file_does_not_exist():
    assert cm.find_match("anything") is None


def test_promote_then_find_match_exact():
    cm.promote("how many donors", "SELECT COUNT(*) FROM donors")
    assert cm.find_match("how many donors") == "SELECT COUNT(*) FROM donors"


def test_find_match_is_case_and_whitespace_insensitive():
    cm.promote("How many donors", "SELECT COUNT(*) FROM donors")
    assert cm.find_match("  HOW    many DONORS  ") == "SELECT COUNT(*) FROM donors"


def test_find_match_does_not_match_a_different_question():
    cm.promote("how many donors", "SELECT COUNT(*) FROM donors")
    assert cm.find_match("how many volunteers") is None


def test_promote_appends_without_clobbering_existing_entries():
    cm.promote("first question", "SELECT 1")
    cm.promote("second question", "SELECT 2")
    assert cm.find_match("first question") == "SELECT 1"
    assert cm.find_match("second question") == "SELECT 2"


def test_promoting_into_a_nonexistent_file_does_not_corrupt_future_lookups():
    # Regression: promote() used to append bare "  - intent: ..." list
    # items with no "promoted_queries:" parent key when catalog.yaml
    # didn't exist yet, which yaml.safe_load() parses as a raw list --
    # _load()'s data.get(...) would then crash on every subsequent
    # find_match() call, not just fail to find the entry.
    import os
    assert not os.path.exists(cm.CATALOG_PATH)
    cm.promote("how many donors", "SELECT COUNT(*) FROM donors")
    assert cm.find_match("how many donors") == "SELECT COUNT(*) FROM donors"
    # A second promote (file now exists with a header already) must also
    # keep working -- guards against only fixing the "file missing" case.
    cm.promote("how many volunteers", "SELECT COUNT(*) FROM volunteers")
    assert cm.find_match("how many volunteers") == "SELECT COUNT(*) FROM volunteers"
    assert cm.find_match("how many donors") == "SELECT COUNT(*) FROM donors"


def test_promoting_into_an_empty_existing_file_also_gets_a_header():
    with open(cm.CATALOG_PATH, "w") as f:
        f.write("")
    cm.promote("how many donors", "SELECT COUNT(*) FROM donors")
    assert cm.find_match("how many donors") == "SELECT COUNT(*) FROM donors"
