#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/metrics-self-review.py (#41 PR-E).

Tests:
  T1 — clean snapshot produces "no issues detected" review.
  T2 — high empty_handed_rate produces high-severity coverage finding.
  T3 — high gap_discovery_rate produces high-severity coverage finding.
  T4 — low memory_hit_rate produces medium-severity finding.
  T5 — high abandonment rate produces medium-severity finding.
  T6 — slow p95 latency produces low-severity finding.
  T7 — low harvest_success_rate produces high-severity system_health finding.
  T8 — high token_budget_violations produces medium finding.
  T9 — mtime > 2x created_at produces medium memory_quality finding.
  T10 — source with memory objects but no compress activity → low finding.
  T11 — latest_snapshot picks the newest by generated_at.
  T12 — render_review groups findings by severity correctly.
  T13 — multiple findings: severity counts in summary.
  T14 — CLI: writes review to default location with today's date.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def setup_review():
    sys.modules.pop("review_test", None)
    return load_module("review_test", PROJ / "tools" / "metrics-self-review.py")


def make_snapshot(**overrides) -> dict:
    """Build a minimal valid snapshot. Overrides merge onto defaults."""
    snap = {
        "schema_version": 1,
        "window_start": "2026-05-01",
        "window_end": "2026-05-07",
        "generated_at": "2026-05-07T12:00:00Z",
        "events_total": 100,
        "harvest_runs_total": 7,
        "user_experience": {
            "queries_total": 25, "sessions_total": 10,
            "time_to_response_ms_p50": 5000, "time_to_response_ms_p95": 15000,
            "queries_per_session_p50": 2, "queries_per_session_p95": 5,
            "query_abandonment_rate": 0.05,
        },
        "coverage": {
            "memory_hit_rate": 0.7, "empty_handed_rate": 0.05,
            "gap_discovery_rate": 0.10, "live_calls_per_query": 0.0,
            "total_queries": 25,
        },
        "memory_quality": {
            "memory_objects_total": 50, "memory_growth_count_in_window": 5,
            "topic_coverage_breadth": 30,
            "by_source_count": {"slack_thread": 20, "gmail_thread": 10, "granola_note": 20},
            "memory_age_days_p50": 3, "memory_age_days_p95": 25,
            "memory_age_source_distribution": {"created_at": 45, "mtime": 5},
        },
        "source_economy": {
            "by_source_kind": {
                "slack_thread": {"compress_result_count": 10, "over_budget_count": 1, "over_budget_rate": 0.1, "canonical_count": 9},
                "gmail_thread": {"compress_result_count": 5, "over_budget_count": 0, "over_budget_rate": 0.0, "canonical_count": 5},
                "granola_note": {"compress_result_count": 12, "over_budget_count": 0, "over_budget_rate": 0.0, "canonical_count": 12},
            },
            "by_kind": {},
        },
        "system_health": {
            "harvest_runs_total": 7, "harvest_success_count": 7, "harvest_failed_count": 0,
            "harvest_success_rate": 1.0,
            "freshness_check_states": {"PASS": 14},
            "mcp_errors_by_source": {},
            "token_budget_violations": 2,
        },
    }
    # Deep-merge overrides
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(snap.get(k), dict):
            snap[k] = {**snap[k], **v}
        else:
            snap[k] = v
    return snap


def test_clean_snapshot_no_issues():
    """T1: clean snapshot produces "no issues" review."""
    rev = setup_review()
    snap = make_snapshot()
    recs = rev.evaluate_rules(snap)
    assert recs == [], f"clean snapshot should produce no findings, got {len(recs)}"
    review = rev.render_review(snap, recs)
    assert "No issues detected" in review
    print("  T1 PASS — clean snapshot → 'no issues detected' review.")


def test_high_empty_handed():
    """T2: empty_handed_rate > 0.30 → high-severity coverage finding."""
    rev = setup_review()
    snap = make_snapshot(coverage={"empty_handed_rate": 0.45})
    recs = rev.evaluate_rules(snap)
    assert any(r["category"] == "coverage" and r["severity"] == "high" for r in recs)
    print("  T2 PASS — high empty_handed_rate → high-severity coverage finding.")


def test_high_gap_discovery():
    """T3: gap_discovery_rate > 0.40 → high-severity coverage finding."""
    rev = setup_review()
    snap = make_snapshot(coverage={"gap_discovery_rate": 0.55})
    recs = rev.evaluate_rules(snap)
    assert any(r["category"] == "coverage" and "gap_discovery" in r["finding"] for r in recs)
    print("  T3 PASS — high gap_discovery_rate → high-severity finding.")


def test_low_memory_hit_rate():
    """T4: memory_hit_rate < 0.50 → medium finding."""
    rev = setup_review()
    snap = make_snapshot(coverage={"memory_hit_rate": 0.4})
    recs = rev.evaluate_rules(snap)
    assert any(r["category"] == "coverage" and "memory_hit_rate" in r["finding"] for r in recs)
    print("  T4 PASS — low memory_hit_rate → medium finding.")


def test_high_abandonment():
    """T5: query_abandonment_rate > 0.20 → medium ux finding."""
    rev = setup_review()
    snap = make_snapshot(user_experience={"query_abandonment_rate": 0.25})
    recs = rev.evaluate_rules(snap)
    assert any(r["category"] == "user_experience" and "abandonment" in r["finding"] for r in recs)
    print("  T5 PASS — high abandonment → medium ux finding.")


def test_slow_p95():
    """T6: p95 latency > 60s → low ux finding."""
    rev = setup_review()
    snap = make_snapshot(user_experience={"time_to_response_ms_p95": 75_000})
    recs = rev.evaluate_rules(snap)
    assert any(r["category"] == "user_experience" and "latency p95" in r["finding"] for r in recs)
    print("  T6 PASS — slow p95 → low ux finding.")


def test_low_harvest_success():
    """T7: harvest_success_rate < 0.95 → high system_health finding."""
    rev = setup_review()
    snap = make_snapshot(system_health={"harvest_success_rate": 0.85})
    recs = rev.evaluate_rules(snap)
    assert any(r["category"] == "system_health" and r["severity"] == "high" for r in recs)
    print("  T7 PASS — low harvest_success_rate → high system_health finding.")


def test_high_token_violations():
    """T8: token_budget_violations > 10 → medium finding."""
    rev = setup_review()
    snap = make_snapshot(system_health={"token_budget_violations": 25})
    recs = rev.evaluate_rules(snap)
    assert any("token_budget_violations" in r["finding"] for r in recs)
    print("  T8 PASS — high token_budget_violations → medium finding.")


def test_mtime_dominates():
    """T9: mtime > 2x created_at → medium memory_quality finding."""
    rev = setup_review()
    snap = make_snapshot(memory_quality={"memory_age_source_distribution": {"created_at": 5, "mtime": 45}})
    recs = rev.evaluate_rules(snap)
    assert any(r["category"] == "memory_quality" and "mtime dominates" in r["finding"] for r in recs)
    print("  T9 PASS — mtime > 2x created_at → medium finding.")


def test_source_no_activity():
    """T10: source with memory but no compress activity → low finding."""
    rev = setup_review()
    snap = make_snapshot(
        memory_quality={
            "memory_objects_total": 50,
            "memory_growth_count_in_window": 5,
            "topic_coverage_breadth": 30,
            "by_source_count": {"slack_thread": 20, "orphan_source": 10},  # orphan_source has memory but...
            "memory_age_days_p50": 3, "memory_age_days_p95": 25,
            "memory_age_source_distribution": {"created_at": 45, "mtime": 5},
        },
        source_economy={
            "by_source_kind": {
                "slack_thread": {"compress_result_count": 10, "over_budget_count": 1, "over_budget_rate": 0.1, "canonical_count": 9},
                # orphan_source has zero compress_result entries
            },
            "by_kind": {},
        },
    )
    recs = rev.evaluate_rules(snap)
    assert any("orphan_source" in r["finding"] for r in recs), \
        f"expected orphan_source finding, got: {[r['finding'] for r in recs]}"
    print("  T10 PASS — source with memory but no compress → low finding.")


def test_latest_snapshot_picks_newest():
    """T11: latest_snapshot picks newest by generated_at."""
    rev = setup_review()
    with tempfile.TemporaryDirectory() as td:
        snap_dir = Path(td) / "snapshots"
        snap_dir.mkdir()
        # Out-of-order filenames; newest by generated_at should win
        for i, gen in enumerate(["2026-05-05T12:00:00Z", "2026-05-07T12:00:00Z", "2026-05-06T12:00:00Z"]):
            (snap_dir / f"snap{i}.json").write_text(json.dumps(make_snapshot(generated_at=gen)))
        latest = rev.latest_snapshot(snap_dir)
        assert latest is not None
        assert latest["generated_at"] == "2026-05-07T12:00:00Z"
    print("  T11 PASS — latest_snapshot picks newest by generated_at.")


def test_render_groups_by_severity():
    """T12: render_review groups by severity headers."""
    rev = setup_review()
    snap = make_snapshot(
        coverage={"empty_handed_rate": 0.5},  # high
        user_experience={"query_abandonment_rate": 0.3, "time_to_response_ms_p95": 75_000},  # medium + low
    )
    recs = rev.evaluate_rules(snap)
    review = rev.render_review(snap, recs)
    assert "🔴 High severity" in review or "High severity" in review
    assert "🟡 Medium severity" in review or "Medium severity" in review
    assert "🔵 Low severity" in review or "Low severity" in review
    print("  T12 PASS — render groups findings by severity.")


def test_summary_counts():
    """T13: summary section reports correct counts per severity."""
    rev = setup_review()
    snap = make_snapshot(
        coverage={"empty_handed_rate": 0.5, "gap_discovery_rate": 0.6},  # 2 high
    )
    recs = rev.evaluate_rules(snap)
    review = rev.render_review(snap, recs)
    # Should mention 2 high
    assert "2 finding" in review
    print("  T13 PASS — summary counts findings per severity correctly.")


def test_high_live_calls_per_query():
    """T15: live_calls_per_query > threshold → medium coverage finding (round-1 add)."""
    rev = setup_review()
    snap = make_snapshot(coverage={"live_calls_per_query": 0.7})
    recs = rev.evaluate_rules(snap)
    assert any(r["category"] == "coverage" and "live_calls_per_query" in r["finding"] for r in recs)
    print("  T15 PASS — high live_calls_per_query → medium finding.")


def test_live_call_error_rate_high():
    """T15a (#39-B follow-up): live_call error+timeout rate > threshold → high finding.
    Uses ≥5 calls to clear the min_live_calls_to_flag guard added per #58."""
    rev = setup_review()
    # 6 success + 2 error + 1 timeout = 9 total, error_rate = 3/9 = 33% > 10%
    snap = make_snapshot(coverage={
        "live_by_status": {"success": 6, "error": 2, "timeout": 1},
    })
    recs = rev.evaluate_rules(snap)
    found = [r for r in recs if r["category"] == "live_calls" and "error+timeout" in r["finding"]]
    assert found, f"expected live_call error finding, got: {[r['finding'] for r in recs]}"
    assert found[0]["severity"] == "high"
    print("  T15a PASS — high live_call error rate → high finding.")


def test_live_call_empty_rate_high():
    """T15b (#39-B follow-up): live_call empty rate > threshold → medium finding."""
    rev = setup_review()
    # 4 success + 6 empty = 10 total, empty_rate = 60% > 40%
    snap = make_snapshot(coverage={
        "live_by_status": {"success": 4, "empty": 6},
    })
    recs = rev.evaluate_rules(snap)
    found = [r for r in recs if r["category"] == "live_calls" and "empty rate" in r["finding"]]
    assert found, f"expected live_call empty finding, got: {[r['finding'] for r in recs]}"
    assert found[0]["severity"] == "medium"
    print("  T15b PASS — high live_call empty rate → medium finding.")


def test_live_call_no_data_no_finding():
    """T15c: when no live calls fired (live_by_status empty/missing), neither rule fires."""
    rev = setup_review()
    snap = make_snapshot(coverage={"live_by_status": {}})
    recs = rev.evaluate_rules(snap)
    live_findings = [r for r in recs if r["category"] == "live_calls"]
    assert live_findings == [], f"expected no live_calls findings, got: {live_findings}"
    print("  T15c PASS — no live calls → no live_call findings (avoids div-by-zero noise).")


def test_live_call_min_n_guard():
    """T15d (#58 challenger): small-N live calls don't trigger high-severity rules
    even when the rate exceeds the threshold. 1 error in 3 calls = 33% > 10%, but
    only 3 calls — wait for more signal before paging the operator."""
    rev = setup_review()
    # 2 success + 1 error = 3 total. err_rate = 33% > 10% threshold, BUT below min_n.
    snap = make_snapshot(coverage={
        "live_by_status": {"success": 2, "error": 1},
    })
    recs = rev.evaluate_rules(snap)
    live_findings = [r for r in recs if r["category"] == "live_calls"]
    assert live_findings == [], (
        f"min_live_calls_to_flag should suppress small-N findings; got: {live_findings}"
    )
    print("  T15d PASS — min_live_calls_to_flag guard prevents small-N noise.")


def test_mcp_errors_finding():
    """T16: mcp_errors_by_source has any errors → low system_health finding."""
    rev = setup_review()
    snap = make_snapshot(system_health={"mcp_errors_by_source": {"slack": 3, "granola": 1}})
    recs = rev.evaluate_rules(snap)
    found = [r for r in recs if "MCP errors" in r["finding"]]
    assert found, f"expected MCP errors finding, got: {[r['finding'] for r in recs]}"
    assert "slack=3" in found[0]["finding"] and "granola=1" in found[0]["finding"]
    print("  T16 PASS — MCP errors per source surfaced as low finding.")


def test_freshness_states_non_pass():
    """T17: non-PASS freshness_check_states surfaces medium finding."""
    rev = setup_review()
    snap = make_snapshot(system_health={"freshness_check_states": {"PASS": 5, "STALE": 2, "FAILED": 1}})
    recs = rev.evaluate_rules(snap)
    found = [r for r in recs if "non-PASS states" in r["finding"]]
    assert found, "non-PASS freshness states should produce a finding"
    assert "STALE=2" in found[0]["finding"] and "FAILED=1" in found[0]["finding"]
    print("  T17 PASS — non-PASS freshness_check_states surfaced as finding.")


def test_just_below_threshold_no_finding():
    """T18: values JUST BELOW threshold do NOT trigger the finding (boundary).

    The previous PR-D challenger raised the hair-trigger concern. Verify that
    values exactly at threshold or just below don't fire — only values that
    materially exceed do. This test pins the boundary semantics."""
    rev = setup_review()
    threshold = rev.THRESHOLDS["empty_handed_rate"]  # 0.30
    # At threshold: comparison is `> 0.30`, so 0.30 should NOT fire.
    snap = make_snapshot(coverage={"empty_handed_rate": threshold})
    recs = rev.evaluate_rules(snap)
    assert not any("empty_handed_rate" in r["finding"] for r in recs), (
        f"value at threshold should not fire, got recs: {[r['finding'] for r in recs]}"
    )
    # Just above: 0.31 should fire.
    snap_above = make_snapshot(coverage={"empty_handed_rate": threshold + 0.01})
    recs_above = rev.evaluate_rules(snap_above)
    assert any("empty_handed_rate" in r["finding"] for r in recs_above)
    print("  T18 PASS — threshold boundary is strict: at threshold = no fire, just above = fire.")


def test_mtime_rule_skipped_when_few_created_at():
    """T23: mtime rule does NOT fire when created_at count is below the floor.

    Fresh-clone vaults have ca=0 (or very low) but plenty of mtime-only
    objects; the old check `mt > 2.0 * max(ca, 1)` fired forever in that
    state, exactly the F4 staleness pattern the parent (#41) tries to avoid.
    The fix gates on `ca >= min_created_at_for_mtime_rule` so the rule
    silently abstains when it has no real signal."""
    rev = setup_review()
    floor = rev.THRESHOLDS["min_created_at_for_mtime_rule"]

    # ca == 0 (fresh-clone): rule must NOT fire even with lots of mtime data.
    snap_fresh = make_snapshot(memory_quality={"memory_age_source_distribution": {"created_at": 0, "mtime": 50}})
    recs_fresh = rev.evaluate_rules(snap_fresh)
    assert not any("mtime dominates" in r["finding"] for r in recs_fresh), (
        f"fresh-clone (ca=0) should not fire mtime rule, got: {[r['finding'] for r in recs_fresh]}"
    )

    # ca just below floor: still no fire.
    snap_below = make_snapshot(memory_quality={"memory_age_source_distribution": {"created_at": floor - 1, "mtime": 50}})
    recs_below = rev.evaluate_rules(snap_below)
    assert not any("mtime dominates" in r["finding"] for r in recs_below)

    # ca at the floor with mt > 2x: rule fires (real signal).
    snap_signal = make_snapshot(memory_quality={"memory_age_source_distribution": {"created_at": floor, "mtime": floor * 3}})
    recs_signal = rev.evaluate_rules(snap_signal)
    assert any("mtime dominates" in r["finding"] for r in recs_signal), (
        f"with ca=floor and mt=3*floor, rule should fire, got: {[r['finding'] for r in recs_signal]}"
    )

    print("  T23 PASS — mtime rule abstains when created_at count is below the floor (no fresh-clone false-positives).")


def test_schema_version_mismatch_warns(capsys):
    """T24: snapshot with unexpected schema_version produces a stderr warning.

    Aggregator bumps schema_version on RENAMES/REMOVALS. A mismatch means
    rules may silently degrade to 'key missing → no finding' — surface it
    loudly so the user updates the tool rather than acting on stale advice."""
    rev = setup_review()
    assert hasattr(rev, "EXPECTED_SCHEMA_VERSION")
    assert rev.EXPECTED_SCHEMA_VERSION == 1

    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        # Stage a content-root-shaped layout so load_config's metrics_dir resolves here.
        snap_dir = td_p / ".metrics" / "snapshots"
        snap_dir.mkdir(parents=True)
        snap_future = make_snapshot()
        snap_future["schema_version"] = 2
        (snap_dir / "snap-future.json").write_text(json.dumps(snap_future))

        # Patch the module's load_config to return a config pointing at td_p.
        # main() reads cfg.harvest_state_root.parent / ".metrics" — so harvest_state_root
        # must be td_p / "<anything>" so that .parent gives td_p.
        class FakeCfg:
            harvest_state_root = td_p / ".harvest"
        original_load_config = rev.load_config
        rev.load_config = lambda **kw: FakeCfg()
        try:
            out_path = td_p / "review.md"
            rc = rev.main(["metrics-self-review.py", "--out", str(out_path)])
        finally:
            rev.load_config = original_load_config

        assert rc == 0, f"unexpected exit code: {rc}"
        # capsys captures stderr from the in-process main() call.
        captured = capsys.readouterr()
        assert "schema_version=2" in captured.err, (
            f"expected schema_version warning in stderr, got: {captured.err!r}"
        )
        assert "WARNING" in captured.err

    print("  T24 PASS — snapshot with mismatched schema_version produces a stderr warning.")


def test_cli_writes_review():
    """T14: CLI writes review file."""
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        out_path = td_p / "review.md"
        env = {**os.environ}
        env.pop("PA_SESSION_ID", None)
        result = subprocess.run(
            [str(PROJ / "tools" / "metrics-self-review.py"), "--out", str(out_path)],
            env=env, capture_output=True, text=True, cwd=PROJ,
        )
        # Returns 0 if there's a snapshot, 1 if not. Either way, no crash.
        assert result.returncode in (0, 1), f"unexpected exit code: {result.returncode}, stderr: {result.stderr}"
        if result.returncode == 0:
            assert out_path.exists()
            md = out_path.read_text()
            assert md.startswith("# Self-review:")
    print("  T14 PASS — CLI runs end-to-end (writes review file when snapshot exists).")


if __name__ == "__main__":
    print("Running test_metrics_self_review_acceptance.py...")
    test_clean_snapshot_no_issues()
    test_high_empty_handed()
    test_high_gap_discovery()
    test_low_memory_hit_rate()
    test_high_abandonment()
    test_slow_p95()
    test_low_harvest_success()
    test_high_token_violations()
    test_mtime_dominates()
    test_source_no_activity()
    test_latest_snapshot_picks_newest()
    test_render_groups_by_severity()
    test_summary_counts()
    test_high_live_calls_per_query()
    test_live_call_error_rate_high()
    test_live_call_empty_rate_high()
    test_live_call_no_data_no_finding()
    test_live_call_min_n_guard()
    test_mcp_errors_finding()
    test_freshness_states_non_pass()
    test_just_below_threshold_no_finding()
    test_mtime_rule_skipped_when_few_created_at()
    test_schema_version_mismatch_warns()
    test_cli_writes_review()
    print("All metrics-self-review tests passed.")
