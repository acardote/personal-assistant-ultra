#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for #27 — harvest freshness check.

Tests:
  T1 — PASS: a recent ok=true run within the threshold returns state=PASS, exit 0.
  T2 — STALE: newest run is older than threshold (by started_at), returns STALE.
  T3 — FAILED: newest run within threshold but ok=false, returns FAILED.
  T4 — MISSING: runs/ directory absent, returns MISSING.
  T5 — MISSING: runs/ exists but has no .json files, returns MISSING.
  T6 — Scheduler-agnostic: works with both 'launchd' and 'routine' scheduler markers.
  T7 — Newest-by-mtime: when multiple files exist, picks the one with newest mtime.
  T8 — CORRUPT: malformed JSON in newest file → CORRUPT, not PASS (challenger fix).
  T9 — STUCK: 3 consecutive ok=false with same error → STUCK, not just FAILED.
  T10 — STUCK threshold: 2 consecutive failures stays as FAILED (below threshold).
  T11 — Age from started_at: payload-time wins over mtime (challenger Claim 2).
  T12 — Age fallback to mtime: when started_at missing/malformed, mtime is used.
  T13 — STALE escalation: STALE summary includes last error if payload_ok=false.
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


def write_run(runs_dir: Path, name: str, payload: dict | str, *, mtime: float | None = None) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / name
    if isinstance(payload, dict):
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path.write_text(payload, encoding="utf-8")  # raw string for malformed-JSON tests
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def iso_hours_ago(hours: float) -> str:
    """Return an ISO-8601 UTC timestamp for `hours` ago."""
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_pass_recent_ok():
    """T1: recent ok=true run → PASS."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        write_run(runs, "2026-05-05T060700Z.json", {
            "started_at": iso_hours_ago(1),
            "ok": True,
            "scheduler": "routine",
        })
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "PASS", f"expected PASS, got {result.state}: {result.summary}"
    assert result.payload_ok is True
    assert result.scheduler == "routine"
    assert result.age_source == "started_at"
    print("  T1 PASS — recent ok=true run treated as healthy.")


def test_stale_old_run():
    """T2: newest run older than threshold → STALE."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        write_run(runs, "2026-05-03T060700Z.json", {
            "started_at": iso_hours_ago(48),
            "ok": True,
            "scheduler": "routine",
        })
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "STALE", f"expected STALE, got {result.state}: {result.summary}"
    assert result.age_hours > 26
    assert result.age_source == "started_at"
    print("  T2 PASS — 48h-old run detected as stale (via started_at).")


def test_failed_recent_not_ok():
    """T3: recent ok=false → FAILED."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        write_run(runs, "2026-05-05T140000Z.json", {
            "started_at": iso_hours_ago(0.5),
            "ok": False,
            "scheduler": "routine",
            "phase": "preflight",
            "error": "critical connector missing: granola",
        })
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "FAILED", f"expected FAILED, got {result.state}: {result.summary}"
    assert result.payload_ok is False
    assert "granola" in (result.error or "")
    assert result.consecutive_failures == 1
    print("  T3 PASS — recent ok=false (single failure) detected as FAILED.")


def test_missing_no_runs_dir():
    """T4: runs/ directory does not exist → MISSING."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "MISSING"
    assert "first fire" in result.summary or "configured" in result.summary, (
        f"first-time-setup messaging should mention configuration: {result.summary}"
    )
    print("  T4 PASS — missing runs/ dir detected with friendly cold-start messaging.")


def test_missing_empty_runs_dir():
    """T5: runs/ exists but has no .json → MISSING."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        runs.mkdir()
        (runs / "irrelevant.txt").write_text("not a status file")
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "MISSING"
    print("  T5 PASS — empty runs/ dir detected as missing.")


def test_scheduler_agnostic():
    """T6: works for both launchd and routine markers."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        write_run(runs, "2026-05-05T060700Z.json", {
            "started_at": iso_hours_ago(1),
            "ok": True,
            "scheduler": "launchd",
        })
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "PASS"
    assert result.scheduler == "launchd"
    print("  T6 PASS — launchd scheduler marker recognized.")


def test_newest_by_mtime_not_lex():
    """T7: when multiple files exist, picks newest by mtime."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        # File with lex-LATER name but OLDER mtime
        write_run(runs, "2026-05-05T230000Z.json", {
            "started_at": iso_hours_ago(40),
            "ok": False, "scheduler": "routine", "error": "old failure",
        }, mtime=time.time() - (40 * 3600))
        # File with lex-EARLIER name but NEWER mtime
        write_run(runs, "2026-05-05T010000Z.json", {
            "started_at": iso_hours_ago(0.5),
            "ok": True, "scheduler": "routine",
        }, mtime=time.time() - 1800)
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "PASS", (
        f"expected PASS from the recent ok=true run; got {result.state}: {result.summary}"
    )
    assert "010000Z" in str(result.newest_path)
    print("  T7 PASS — selection by mtime, not lexicographic.")


def test_corrupt_json():
    """T8: malformed JSON in newest file → CORRUPT (challenger Claim T8 fix)."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        write_run(runs, "2026-05-05T120000Z.json", "{ this is not valid json")
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "CORRUPT", (
        f"malformed JSON should produce CORRUPT (not PASS); got {result.state}"
    )
    assert "unparseable" in result.summary or "manually edited" in result.summary
    print("  T8 PASS — malformed JSON now produces CORRUPT, not silent PASS.")


def test_stuck_three_same_error():
    """T9: 3 consecutive ok=false with same error → STUCK."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        # Three failures with same error, increasing mtime
        for i, hours_ago in enumerate([72, 48, 0.5]):
            write_run(runs, f"2026-05-0{2+i}T060700Z.json", {
                "started_at": iso_hours_ago(hours_ago),
                "ok": False,
                "scheduler": "routine",
                "phase": "preflight",
                "error": "critical connector missing: granola",
            }, mtime=time.time() - (hours_ago * 3600))
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "STUCK", f"expected STUCK, got {result.state}: {result.summary}"
    assert result.consecutive_failures == 3
    assert "chronic" in result.summary
    print("  T9 PASS — 3 consecutive same-error failures detected as STUCK.")


def test_failed_below_stuck_threshold():
    """T10: 2 consecutive failures stays FAILED (below STUCK threshold of 3)."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        for i, hours_ago in enumerate([24, 0.5]):
            write_run(runs, f"2026-05-0{4+i}T060700Z.json", {
                "started_at": iso_hours_ago(hours_ago),
                "ok": False,
                "scheduler": "routine",
                "error": "transient timeout",
            }, mtime=time.time() - (hours_ago * 3600))
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "FAILED", f"expected FAILED (below STUCK threshold), got {result.state}"
    assert result.consecutive_failures == 2
    print("  T10 PASS — 2 consecutive failures stays FAILED (below STUCK threshold).")


def test_age_from_started_at_not_mtime():
    """T11: started_at wins over mtime (challenger Claim 2)."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        # Mtime is FRESH (just-cloned simulation), but started_at says months ago
        write_run(runs, "2026-01-01T060700Z.json", {
            "started_at": iso_hours_ago(24 * 60),  # 60 days ago
            "ok": True,
            "scheduler": "routine",
        }, mtime=time.time() - 60)  # 60 seconds ago — like a fresh git clone
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "STALE", (
        f"60-day-old started_at should beat fresh mtime; got {result.state}: {result.summary}"
    )
    assert result.age_source == "started_at"
    assert result.age_hours > 24 * 30
    print("  T11 PASS — payload started_at wins over fresh mtime (post-clone scenario).")


def test_age_fallback_to_mtime():
    """T12: when started_at missing or malformed, fallback to mtime."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        # No started_at field
        write_run(runs, "2026-05-05T060700Z.json", {
            "ok": True, "scheduler": "routine",
        }, mtime=time.time() - 3600)
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "PASS", f"missing started_at should fall back to mtime, got {result.state}"
    assert result.age_source == "mtime"
    print("  T12 PASS — missing started_at falls back cleanly to mtime.")


def test_stale_includes_last_error():
    """T13: STALE summary surfaces last run's error if payload_ok=false (reviewer suggestion)."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        write_run(runs, "2026-05-03T060700Z.json", {
            "started_at": iso_hours_ago(48),
            "ok": False,
            "scheduler": "routine",
            "error": "git push failed: non-fast-forward",
        })
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "STALE"
    assert "non-fast-forward" in result.summary, (
        f"STALE banner should surface the last error: {result.summary}"
    )
    print("  T13 PASS — STALE banner surfaces last error when payload_ok=false.")


def test_malformed_started_at_falls_back_to_mtime():
    """T14: malformed-but-present started_at falls back to mtime (round-2 challenger gap)."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    for bad_value in ["yesterday", 1234567890, "", "2026-13-99T99:99:99Z"]:
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            write_run(runs, "2026-05-05T060700Z.json", {
                "started_at": bad_value,
                "ok": True,
                "scheduler": "routine",
            }, mtime=time.time() - 3600)
            result = cf.assess_freshness(runs, max_age_hours=26)
        assert result.state == "PASS", (
            f"malformed started_at={bad_value!r} should fall back to mtime, got {result.state}"
        )
        assert result.age_source == "mtime", (
            f"malformed started_at={bad_value!r} should use mtime, got age_source={result.age_source}"
        )
    print("  T14 PASS — malformed started_at values fall back to mtime cleanly.")


def test_stuck_and_stale_collision():
    """T15: STUCK + STALE both true → STUCK with stale-suffix in summary (round-2 challenger)."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        # 3 consecutive same-error failures, all old enough to also be STALE
        for i, hours_ago in enumerate([120, 96, 72]):
            write_run(runs, f"2026-04-3{i}T060700Z.json", {
                "started_at": iso_hours_ago(hours_ago),
                "ok": False,
                "scheduler": "routine",
                "error": "critical connector missing: granola",
            }, mtime=time.time() - (hours_ago * 3600))
        result = cf.assess_freshness(runs, max_age_hours=26)
    assert result.state == "STUCK", f"expected STUCK, got {result.state}"
    assert "Also STALE" in result.summary, (
        f"STUCK+STALE collision should surface both: {result.summary}"
    )
    print("  T15 PASS — STUCK+STALE collision surfaces both signals in summary.")


def test_stuck_threshold_configurable():
    """T16: stuck_threshold parameter changes when STUCK fires (round-2 challenger)."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        # Two consecutive same-error failures
        for i, hours_ago in enumerate([24, 0.5]):
            write_run(runs, f"2026-05-0{4+i}T060700Z.json", {
                "started_at": iso_hours_ago(hours_ago),
                "ok": False,
                "scheduler": "routine",
                "error": "transient timeout",
            }, mtime=time.time() - (hours_ago * 3600))
        # Default threshold (3): stays FAILED
        r3 = cf.assess_freshness(runs, max_age_hours=26, stuck_threshold=3)
        # Custom threshold of 2: now STUCK
        r2 = cf.assess_freshness(runs, max_age_hours=26, stuck_threshold=2)
    assert r3.state == "FAILED", f"threshold=3 should keep state=FAILED, got {r3.state}"
    assert r2.state == "STUCK", f"threshold=2 should produce STUCK, got {r2.state}"
    print("  T16 PASS — stuck_threshold parameter overrides default.")


def test_corrupt_in_middle_of_failures_does_not_break_count():
    """T17: corrupt file mid-failure-streak doesn't break the consecutive-failure count."""
    cf = load_module("check_freshness", PROJ / "tools" / "check-harvest-freshness.py")
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td) / "runs"
        # Newest: a real failure
        write_run(runs, "2026-05-05T060700Z.json", {
            "started_at": iso_hours_ago(0.5),
            "ok": False,
            "scheduler": "routine",
            "error": "critical connector missing: granola",
        }, mtime=time.time() - 1800)
        # Middle: corrupt file
        write_run(runs, "2026-05-04T060700Z.json", "{ truncated", mtime=time.time() - (24 * 3600))
        # Older: two more real failures with same error
        write_run(runs, "2026-05-03T060700Z.json", {
            "started_at": iso_hours_ago(48),
            "ok": False,
            "scheduler": "routine",
            "error": "critical connector missing: granola",
        }, mtime=time.time() - (48 * 3600))
        write_run(runs, "2026-05-02T060700Z.json", {
            "started_at": iso_hours_ago(72),
            "ok": False,
            "scheduler": "routine",
            "error": "critical connector missing: granola",
        }, mtime=time.time() - (72 * 3600))
        result = cf.assess_freshness(runs, max_age_hours=26, stuck_threshold=3)
    # Walk should skip the corrupt file in the middle and find 3 real failures.
    assert result.state == "STUCK", f"corrupt file in middle shouldn't break STUCK detection, got {result.state}"
    assert result.consecutive_failures == 3, (
        f"expected 3 consecutive failures (corrupt skipped), got {result.consecutive_failures}"
    )
    print("  T17 PASS — corrupt file mid-streak doesn't reset consecutive-failure count.")


if __name__ == "__main__":
    print("Running test_harvest_freshness_acceptance.py...")
    test_pass_recent_ok()
    test_stale_old_run()
    test_failed_recent_not_ok()
    test_missing_no_runs_dir()
    test_missing_empty_runs_dir()
    test_scheduler_agnostic()
    test_newest_by_mtime_not_lex()
    test_corrupt_json()
    test_stuck_three_same_error()
    test_failed_below_stuck_threshold()
    test_age_from_started_at_not_mtime()
    test_age_fallback_to_mtime()
    test_stale_includes_last_error()
    test_malformed_started_at_falls_back_to_mtime()
    test_stuck_and_stale_collision()
    test_stuck_threshold_configurable()
    test_corrupt_in_middle_of_failures_does_not_break_count()
    print("All harvest-freshness tests passed.")
