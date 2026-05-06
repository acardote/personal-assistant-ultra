#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/metrics-dashboard.py (#41 PR-D).

Tests:
  T1 — empty snapshots dir produces well-formed "no data" HTML.
  T2 — single snapshot produces an HTML page with current state but no time-series data points.
  T3 — multiple snapshots produce time-series charts ordered by generated_at.
  T4 — HTML output contains Plotly script tag and chart divs.
  T5 — Latest-snapshot tables render expected content.
  T6 — Malformed JSON in snapshots dir is skipped, not crashed on.
  T7 — load_snapshots handles missing snapshots dir gracefully.
  T8 — by_source_kind table includes the source kind names.
  T9 — mtime-dominates warning surfaces when memory_age_source_distribution shows mtime > created_at.
  T10 — CLI --out flag writes to specified path.
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


def setup_dashboard():
    sys.modules.pop("dash_test", None)
    return load_module("dash_test", PROJ / "tools" / "metrics-dashboard.py")


def make_snapshot(generated_at: str, **overrides) -> dict:
    """Build a minimal valid snapshot."""
    snap = {
        "schema_version": 1,
        "window_start": "2026-05-01",
        "window_end": "2026-05-07",
        "generated_at": generated_at,
        "events_total": 100,
        "harvest_runs_total": 7,
        "user_experience": {
            "queries_total": 25, "sessions_total": 10,
            "time_to_response_ms_p50": 5000, "time_to_response_ms_p95": 30000,
            "queries_per_session_p50": 2, "queries_per_session_p95": 5,
            "query_abandonment_rate": 0.05,
        },
        "coverage": {
            "memory_hit_rate": 0.6, "empty_handed_rate": 0.1,
            "gap_discovery_rate": 0.2, "live_calls_per_query": 0.0,
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
            },
            "by_kind": {},
        },
        "system_health": {
            "harvest_runs_total": 7, "harvest_success_count": 6, "harvest_failed_count": 1,
            "harvest_success_rate": 0.857,
            "freshness_check_states": {"PASS": 14, "STALE": 1},
            "mcp_errors_by_source": {},
            "token_budget_violations": 3,
        },
    }
    snap.update(overrides)
    return snap


def test_empty_snapshots_dir():
    """T1: empty snapshots dir produces well-formed 'no data' HTML."""
    dash = setup_dashboard()
    with tempfile.TemporaryDirectory() as td:
        snapshots = dash.load_snapshots(Path(td) / "snapshots")
        html = dash.render_html(snapshots)
        assert "<html" in html
        assert "Personal-assistant metrics" in html
        assert "No snapshot data yet" in html
        # Plotly script tag still present (even with no data)
        assert "plot.ly" in html or "plotly" in html.lower()
    print("  T1 PASS — empty snapshots dir produces well-formed 'no data' HTML.")


def test_single_snapshot():
    """T2: single snapshot produces page with current-state tables."""
    dash = setup_dashboard()
    with tempfile.TemporaryDirectory() as td:
        snap_dir = Path(td) / "snapshots"
        snap_dir.mkdir()
        snap = make_snapshot("2026-05-07T12:00:00Z")
        (snap_dir / "snap.json").write_text(json.dumps(snap))

        snapshots = dash.load_snapshots(snap_dir)
        html = dash.render_html(snapshots)

        assert "2026-05-07T12:00:00Z" in html
        assert "100" in html  # events_total
        assert "User experience" in html
        assert "Coverage" in html
    print("  T2 PASS — single snapshot renders current-state tables.")


def test_multiple_snapshots_ordered():
    """T3: multiple snapshots produce time-series ordered by generated_at."""
    dash = setup_dashboard()
    with tempfile.TemporaryDirectory() as td:
        snap_dir = Path(td) / "snapshots"
        snap_dir.mkdir()
        # Out-of-order filenames; correct order should be by generated_at.
        for i, gen in enumerate(["2026-05-07T12:00:00Z", "2026-05-05T12:00:00Z", "2026-05-06T12:00:00Z"]):
            (snap_dir / f"snap{i}.json").write_text(json.dumps(make_snapshot(gen)))

        snapshots = dash.load_snapshots(snap_dir)
        gens = [s["generated_at"] for s in snapshots]
        assert gens == sorted(gens), f"snapshots not sorted: {gens}"
        # 3 snapshots → time-series charts have 3 x-axis points
        charts = dash.build_charts(snapshots)
        assert len(charts) > 0
        for c in charts:
            for series in c["data"]:
                assert len(series["x"]) == 3
    print("  T3 PASS — multiple snapshots sorted chronologically; charts have all data points.")


def test_html_has_plotly_and_charts():
    """T4: HTML output contains Plotly script and chart divs."""
    dash = setup_dashboard()
    with tempfile.TemporaryDirectory() as td:
        snap_dir = Path(td) / "snapshots"
        snap_dir.mkdir()
        (snap_dir / "snap.json").write_text(json.dumps(make_snapshot("2026-05-07T12:00:00Z")))
        snapshots = dash.load_snapshots(snap_dir)
        html = dash.render_html(snapshots)
        # Plotly CDN + at least one chart div
        assert "plot.ly" in html
        assert "Plotly.newPlot" in html
        assert "chart-0" in html
    print("  T4 PASS — HTML includes Plotly script tag and chart divs.")


def test_current_state_tables():
    """T5: current-state tables include expected numeric content."""
    dash = setup_dashboard()
    snap = make_snapshot("2026-05-07T12:00:00Z")
    html = dash.render_current_state(snap)
    # Table values from snap
    assert "25" in html  # queries_total
    assert "0.6" in html  # memory_hit_rate
    assert "slack_thread" in html  # by_source_count entry
    assert "9" in html  # canonical_count
    print("  T5 PASS — current-state tables render expected values.")


def test_malformed_snapshot_skipped():
    """T6: malformed JSON in snapshots dir is skipped, not crashed on."""
    dash = setup_dashboard()
    with tempfile.TemporaryDirectory() as td:
        snap_dir = Path(td) / "snapshots"
        snap_dir.mkdir()
        # One valid, one malformed
        (snap_dir / "good.json").write_text(json.dumps(make_snapshot("2026-05-07T12:00:00Z")))
        (snap_dir / "bad.json").write_text("{ this is NOT valid json")
        snapshots = dash.load_snapshots(snap_dir)
        # Only the good one loaded
        assert len(snapshots) == 1
        assert snapshots[0]["generated_at"] == "2026-05-07T12:00:00Z"
    print("  T6 PASS — malformed snapshots skipped, valid loaded.")


def test_missing_snapshots_dir():
    """T7: missing snapshots dir returns empty list."""
    dash = setup_dashboard()
    with tempfile.TemporaryDirectory() as td:
        snapshots = dash.load_snapshots(Path(td) / "nonexistent")
        assert snapshots == []
    print("  T7 PASS — missing snapshots dir returns empty list cleanly.")


def test_by_source_kind_table():
    """T8: by_source_kind table renders source kind names."""
    dash = setup_dashboard()
    snap = make_snapshot(
        "2026-05-07T12:00:00Z",
        source_economy={
            "by_source_kind": {
                "slack_thread": {"compress_result_count": 16, "over_budget_count": 6, "over_budget_rate": 0.375, "canonical_count": 14},
                "granola_note": {"compress_result_count": 12, "over_budget_count": 0, "over_budget_rate": 0.0, "canonical_count": 12},
                "gmail_thread": {"compress_result_count": 5, "over_budget_count": 0, "over_budget_rate": 0.0, "canonical_count": 5},
            },
            "by_kind": {},
        },
    )
    html = dash.render_current_state(snap)
    assert "slack_thread" in html
    assert "granola_note" in html
    assert "gmail_thread" in html
    assert "16" in html and "12" in html
    print("  T8 PASS — by_source_kind table includes all source names + counts.")


def test_mtime_dominates_warning():
    """T9: mtime-dominates warning when mtime > created_at in age distribution."""
    dash = setup_dashboard()
    snap = make_snapshot(
        "2026-05-07T12:00:00Z",
        memory_quality={
            "memory_objects_total": 50, "memory_growth_count_in_window": 0,
            "topic_coverage_breadth": 0,
            "by_source_count": {"slack_thread": 50},
            "memory_age_days_p50": 0, "memory_age_days_p95": 0,
            "memory_age_source_distribution": {"created_at": 5, "mtime": 45},  # mtime dominates
        },
    )
    html = dash.render_current_state(snap)
    assert "mtime dominates" in html, "warning should appear when mtime > created_at"
    print("  T9 PASS — mtime-dominates warning appears in HTML.")


def test_cli_writes_html():
    """T10: CLI runs end-to-end and writes a valid HTML page.

    Note: the CLI's snapshot discovery uses load_config (which reads the real
    `.assistant.local.json`, not the temp dir we set up here). So the dashboard
    will see whatever snapshots actually exist in the user's vault, not the
    test fixture. We only assert that the CLI writes a well-formed HTML page —
    the per-snapshot rendering is covered by T2/T3/T5/T8/T9 calling the
    library functions directly.
    """
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        env = {**os.environ}
        env.pop("PA_SESSION_ID", None)
        out_path = td_p / "dashboard.html"
        result = subprocess.run(
            [str(PROJ / "tools" / "metrics-dashboard.py"), "--out", str(out_path)],
            env=env, capture_output=True, text=True, cwd=PROJ,
        )
        assert result.returncode == 0, f"dashboard CLI failed: {result.stderr}"
        assert out_path.exists()
        html = out_path.read_text()
        assert "<html" in html
        assert "Personal-assistant metrics" in html
        # Plotly script either via CDN or JS code present
        assert "plot.ly" in html or "Plotly" in html
    print("  T10 PASS — CLI runs end-to-end, writes valid HTML.")


if __name__ == "__main__":
    print("Running test_metrics_dashboard_acceptance.py...")
    test_empty_snapshots_dir()
    test_single_snapshot()
    test_multiple_snapshots_ordered()
    test_html_has_plotly_and_charts()
    test_current_state_tables()
    test_malformed_snapshot_skipped()
    test_missing_snapshots_dir()
    test_by_source_kind_table()
    test_mtime_dominates_warning()
    test_cli_writes_html()
    print("All metrics-dashboard tests passed.")
