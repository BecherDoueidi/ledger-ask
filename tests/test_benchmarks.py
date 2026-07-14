"""
Smoke test for benchmarks/run_benchmarks.py -- proves the benchmark
script's setup and all four scenarios actually execute end-to-end
(hermetic: temp SQLite engine, faked LLM, isolated stores, same as the
rest of the suite) without needing to run the full default iteration
count, which is a separate concern from this repo's normal fast test
run. Not a performance assertion -- CI hardware varies too much for a
duration threshold here to mean anything; this only proves the script
still runs and returns well-shaped results after a refactor.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmarks"))

import run_benchmarks


def test_all_four_paths_run_and_return_well_shaped_results():
    results = run_benchmarks.run(iterations=2, verbose=False)
    paths = {r["path"] for r in results}
    assert paths == {
        "Fresh LLM generation (pipeline only)",
        "Exact cache hit",
        "Catalog hit",
        "In-memory follow-up transform",
    }
    for row in results:
        assert row["n"] == 2
        assert row["min_ms"] <= row["p50_ms"] <= row["p95_ms"] <= row["max_ms"]
        assert row["mean_ms"] >= 0
