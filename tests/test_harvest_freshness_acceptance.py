#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for #27 — harvest freshness check.

Tests:
  T1 — PASS: a recent ok=true run within the threshold returns state=PASS, exit 0.
  T2 — STALE: newest run is older than threshold, returns state=STALE, exit 1.
  T3 — FAILED: newest run within threshold but ok=false, returns state=FAILED, exit 1.
  T4 — MISSING: runs/ directory absent, returns state=MISSING, exit 1.
  T5 — MISSING: runs/ exists but has no .json files, returns state=MISSING, exit 1.
  T6 — Scheduler-agnostic: works with both 'launchd' and 'routine' scheduler markers.
  T7 — Newest-by-mtime: when multiple files exist, picks the one with newest mtime
        (not lexicographic order), validating that a freshly-written file with an
        older filename still wins.
  T8 — Malformed JSON in newest file: still surfaces age-based assessment cleanly,
        does not crash, treats payload as unavailable.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_run(runs_dir: Path, ts_compact: str, payload: dict, *, mtime: float | None = None) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{ts_compact}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_pass_recent_ok():
    """T1: recent ok=true run → PASS."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        recent_mtime = time.time() - 3600  # 1h ago
        write_run(runs, "2026-05-05T060700Z", {
            "started_at": "2026-05-05T06:07:00Z",
            "ok": True,
            "scheduler": "routine",
        }, mtime=recent_mtime)
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "PASS", f"expected PASS, got {result.state}: {result.summary}"
    assert result.payload_ok is True
    assert result.scheduler == "routine"
    print("  T1 PASS — recent ok=true run treated as healthy.")


def test_stale_old_run():
    """T2: newest run older than threshold → STALE."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        old_mtime = time.time() - (48 * 3600)  # 48h ago
        write_run(runs, "2026-05-03T060700Z", {
            "started_at": "2026-05-03T06:07:00Z",
            "ok": True,
            "scheduler": "routine",
        }, mtime=old_mtime)
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "STALE", f"expected STALE, got {result.state}: {result.summary}"
    assert result.age_hours > 26
    print("  T2 PASS — 48h-old run detected as stale.")


def test_failed_recent_not_ok():
    """T3: recent run with ok=false → FAILED."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        recent_mtime = time.time() - 1800  # 30min ago
        write_run(runs, "2026-05-05T140000Z", {
            "started_at": "2026-05-05T14:00:00Z",
            "ok": False,
            "scheduler": "routine",
            "phase": "preflight",
            "error": "critical connector missing: granola",
        }, mtime=recent_mtime)
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "FAILED", f"expected FAILED, got {result.state}: {result.summary}"
    assert result.payload_ok is False
    assert "granola" in (result.error or "")
    print("  T3 PASS — recent ok=false run detected as failed.")


def test_missing_no_runs_dir():
    """T4: runs/ directory does not exist → MISSING."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"  # never created
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "MISSING", f"expected MISSING, got {result.state}: {result.summary}"
    assert result.newest_path is None
    print("  T4 PASS — missing runs/ dir detected.")


def test_missing_empty_runs_dir():
    """T5: runs/ exists but has no .json → MISSING."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        runs.mkdir()
        (runs / "irrelevant.txt").write_text("not a status file")
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "MISSING", f"expected MISSING, got {result.state}: {result.summary}"
    print("  T5 PASS — empty runs/ dir detected as missing.")


def test_scheduler_agnostic():
    """T6: works for both launchd and routine markers."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        recent_mtime = time.time() - 3600
        write_run(runs, "2026-05-05T060700Z", {
            "started_at": "2026-05-05T06:07:00Z",
            "ok": True,
            "scheduler": "launchd",
        }, mtime=recent_mtime)
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "PASS"
    assert result.scheduler == "launchd"
    print("  T6 PASS — launchd scheduler marker recognized; check is scheduler-agnostic.")


def test_newest_by_mtime_not_lex():
    """T7: when multiple files exist, picks newest by mtime."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        # File with lex-LATER name but OLDER mtime
        write_run(runs, "2026-05-05T230000Z", {
            "ok": False, "scheduler": "routine", "error": "old failure",
        }, mtime=time.time() - (40 * 3600))
        # File with lex-EARLIER name but NEWER mtime
        write_run(runs, "2026-05-05T010000Z", {
            "ok": True, "scheduler": "routine",
        }, mtime=time.time() - 1800)
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "PASS", (
        f"newest-by-mtime should be the recent ok=true run; got {result.state}: {result.summary}"
    )
    assert "010000Z" in str(result.newest_path)
    print("  T7 PASS — selection by mtime, not lexicographic.")


def test_malformed_json_does_not_crash():
    """T8: malformed JSON in newest file → still surfaces age-based assessment."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        runs.mkdir()
        # Write a malformed JSON file with a recent mtime
        bad = runs / "2026-05-05T120000Z.json"
        bad.write_text("{ this is not valid json")
        recent_mtime = time.time() - 3600
        os.utime(bad, (recent_mtime, recent_mtime))
        result = cf.assess_freshness(runs, max_age_hours=26)
    # Malformed payload means we can't read ok/scheduler, but age is still computable.
    # By the design here, malformed counts as "payload unavailable" → falls through
    # to the not-stale-and-not-explicitly-failed branch → PASS.
    # That's acceptable: corrupted status files shouldn't cascade alerts; a stale
    # check is the right alert when the user genuinely needs to investigate.
    assert result.state == "PASS", f"malformed JSON should not crash; got {result.state}"
    assert result.payload_ok is None
    assert result.scheduler is None
    print("  T8 PASS — malformed JSON handled gracefully.")


if __name__ == "__main__":
    print("Running test_harvest_freshness_acceptance.py...")
    test_pass_recent_ok()
    test_stale_old_run()
    test_failed_recent_not_ok()
    test_missing_no_runs_dir()
    test_missing_empty_runs_dir()
    test_scheduler_agnostic()
    test_newest_by_mtime_not_lex()
    test_malformed_json_does_not_crash()
    print("All harvest-freshness tests passed.")
