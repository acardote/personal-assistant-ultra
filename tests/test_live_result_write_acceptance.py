#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/live-result-write.py (#39-B.1).

Verifies the helper writes live-fetched artifacts to the right location
with the right provenance comment, emits live_call_end with the right
status, and refuses bad input.

Tests (revised after PR #53 adversarial review):
  T1  — write_live_artifact creates path under raw/live/<source>/ (NEW dir scheme).
  T2  — File contents start with the provenance HTML comment.
  T3  — Filename uses <ts>-<hash>.md (no longer "live-" prefix; that's the parent dir).
  T4  — Same query → same hash (deterministic).
  T5  — Different queries → different hashes.
  T6  — Refuses unknown --source.
  T7  — Refuses empty query.
  T8  — Refuses empty body (zero-byte artifact).
  T9  — write_live_artifact respects passed content_root.
  T10 — CLI emits live_call_end with status=success.
  T11 — CLI rejects missing --source.
  T12 — CLI exits 3 + emits live_call_end status=empty on empty stdin.
  T13 — --start-iso causes helper to compute duration_ms on the event.
  T14 — utc_ts has millisecond precision (collision avoidance for same-second retries).
  T15 — raw/live/<source>/ is distinct from raw/<source>/ (separation from harvest).
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
TOOL = PROJ / "tools" / "live-result-write.py"


def load_helper():
    sys.modules.pop("live_result_write_test", None)
    spec = importlib.util.spec_from_file_location(
        "live_result_write_test", str(TOOL),
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules["live_result_write_test"] = m
    spec.loader.exec_module(m)
    return m


def test_write_creates_path_under_live_dir():
    """T1: target path is <content_root>/raw/live/<source>/<ts>-<hash>.md."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target, truncated = h.write_live_artifact(
            source="granola_note",
            query="what's up with Acko?",
            body="Some meeting notes.",
            content_root=root,
        )
        expected_dir = root / "raw" / "live" / "granola_note"
        assert target.parent == expected_dir, f"expected dir {expected_dir}, got {target.parent}"
        assert target.exists()
        assert target.suffix == ".md"
        assert truncated is False
    print("  T1 PASS — write creates path under raw/live/<source>/.")


def test_provenance_comment_first():
    """T2: first line of file is the HTML provenance comment."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        target, _ = h.write_live_artifact(
            source="slack_thread",
            query="status of BADAS",
            body="@user: progressing well",
            content_root=Path(td),
        )
        first_line = target.read_text().splitlines()[0]
        assert first_line.startswith("<!--"), f"first line not HTML comment: {first_line!r}"
        assert "live-fetched on" in first_line
        assert "status of BADAS" in first_line
        assert "#39-B" in first_line
    print("  T2 PASS — provenance comment carries query + timestamp + ref.")


def test_filename_format():
    """T3: filename is <ts>-<8hex>.md (no 'live-' prefix; the dir conveys that)."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        target, _ = h.write_live_artifact(
            source="gmail_thread",
            query="renewal status",
            body="email body",
            content_root=Path(td),
        )
        # Filename ends with -<8hex>.md
        m = re.match(r"^.*-([0-9a-f]{8})$", target.stem)
        assert m, f"filename {target.name!r} does not end with -<8hex>"
        assert "live" not in target.name, "live/ is now the parent dir, not a filename prefix"
    print(f"  T3 PASS — filename is <ts>-<8hex>.md, dir conveys 'live'.")


def test_query_hash_deterministic():
    """T4: same query → same hash."""
    h = load_helper()
    a = h.query_hash("what's up with Acko?")
    b = h.query_hash("what's up with Acko?")
    assert a == b
    print("  T4 PASS — same query → same hash.")


def test_query_hash_distinct():
    """T5: different queries → different hashes."""
    h = load_helper()
    a = h.query_hash("query one")
    b = h.query_hash("query two")
    assert a != b
    print("  T5 PASS — different queries → different hashes.")


def test_refuses_unknown_source():
    """T6: write_live_artifact raises on unknown source."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        raised = False
        try:
            h.write_live_artifact(
                source="invented_source",
                query="q",
                body="b",
                content_root=Path(td),
            )
        except ValueError:
            raised = True
        assert raised
    print("  T6 PASS — unknown source raises ValueError.")


def test_refuses_empty_query():
    """T7: empty/whitespace-only query raises."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        raised = False
        try:
            h.write_live_artifact(
                source="granola_note",
                query="   ",
                body="b",
                content_root=Path(td),
            )
        except ValueError:
            raised = True
        assert raised
    print("  T7 PASS — empty query raises.")


def test_refuses_empty_body():
    """T8: empty/whitespace-only body raises."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        raised = False
        try:
            h.write_live_artifact(
                source="granola_note",
                query="q",
                body="\n  \n",
                content_root=Path(td),
            )
        except ValueError:
            raised = True
        assert raised
    print("  T8 PASS — empty body raises.")


def test_writes_under_content_root():
    """T9: target is under the passed content_root."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        custom_root = Path(td) / "custom_vault"
        target, _ = h.write_live_artifact(
            source="granola_note",
            query="q",
            body="b",
            content_root=custom_root,
        )
        assert str(target).startswith(str(custom_root))
        assert "raw/live/granola_note" in str(target)
    print("  T9 PASS — write respects passed content_root.")


def test_cli_emits_live_call_end_success():
    """T10: CLI emits live_call_end with status=success on a successful write.
    Uses try/finally to clean up the real-vault file even if assertions fail
    (per pr-reviewer R3 on PR #53)."""
    target_path: Path | None = None
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        metrics_dir = td_p / "metrics"
        metrics_dir.mkdir()
        env = dict(os.environ)
        env["PA_METRICS_DIR"] = str(metrics_dir)
        env.pop("PA_CONTENT_ROOT", None)

        result = subprocess.run(
            [str(TOOL), "--source", "granola_note", "--query", "test integration query"],
            input="Integration test body for #39-B.1.\n",
            env=env, text=True, capture_output=True, check=True,
        )
        target_path = Path(result.stdout.strip())
        try:
            assert target_path.exists(), f"target {target_path} not written"
            assert "raw/live/granola_note" in str(target_path)

            event_files = list(metrics_dir.glob("events-*.jsonl"))
            assert event_files, "no events file written"
            events = [json.loads(line) for line in event_files[0].read_text().splitlines() if line.strip()]
            ends = [e for e in events if e["event"] == "live_call_end"]
            assert len(ends) == 1
            payload = ends[0]["data"]
            assert payload["source"] == "granola_note"
            assert payload["status"] == "success"
            assert payload["bytes_written"] > 0
            assert "query_hash" in payload and len(payload["query_hash"]) == 8
        finally:
            if target_path is not None and target_path.exists():
                target_path.unlink()
    print("  T10 PASS — CLI emits live_call_end status=success.")


def test_cli_rejects_missing_source():
    """T11: CLI exits non-zero when --source is missing."""
    result = subprocess.run(
        [str(TOOL), "--query", "q"],
        input="body",
        text=True, capture_output=True,
    )
    assert result.returncode != 0
    print("  T11 PASS — CLI rejects missing --source.")


def test_cli_empty_stdin_emits_status_empty():
    """T12: empty stdin → exit 3, AND emits live_call_end with status=empty
    (per pr-challenger C3 on PR #53 — was a silent skip before, biasing
    dashboards by hiding empty Granola responses)."""
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        metrics_dir = td_p / "metrics"
        metrics_dir.mkdir()
        env = dict(os.environ)
        env["PA_METRICS_DIR"] = str(metrics_dir)

        result = subprocess.run(
            [str(TOOL), "--source", "granola_note", "--query", "empty result query"],
            input="",
            env=env, text=True, capture_output=True,
        )
        assert result.returncode == 3, f"expected exit 3, got {result.returncode}"

        event_files = list(metrics_dir.glob("events-*.jsonl"))
        assert event_files
        events = [json.loads(line) for line in event_files[0].read_text().splitlines() if line.strip()]
        ends = [e for e in events if e["event"] == "live_call_end"]
        assert len(ends) == 1
        assert ends[0]["data"]["status"] == "empty"
    print("  T12 PASS — empty stdin emits live_call_end status=empty + exits 3.")


def test_cli_start_iso_computes_duration_ms():
    """T13: --start-iso causes helper to set duration_ms on live_call_end
    (per pr-reviewer R1 on PR #53 — was relying on skill discipline alone)."""
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        metrics_dir = td_p / "metrics"
        metrics_dir.mkdir()
        env = dict(os.environ)
        env["PA_METRICS_DIR"] = str(metrics_dir)

        # Synthetic start ts 5 seconds before now.
        start = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)).isoformat(timespec="milliseconds")
        # ISO format may carry +00:00; helper handles both.
        target_path: Path | None = None
        try:
            result = subprocess.run(
                [str(TOOL), "--source", "granola_note", "--query", "duration check",
                 "--start-iso", start],
                input="body content for duration test\n",
                env=env, text=True, capture_output=True, check=True,
            )
            target_path = Path(result.stdout.strip())
            event_files = list(metrics_dir.glob("events-*.jsonl"))
            events = [json.loads(line) for line in event_files[0].read_text().splitlines() if line.strip()]
            end = [e for e in events if e["event"] == "live_call_end"][0]
            assert end.get("duration_ms") is not None, "duration_ms missing"
            # Should be between 4500 and 7000 ms (5s synth + headroom)
            assert 4500 <= end["duration_ms"] <= 7000, f"duration_ms = {end['duration_ms']}, expected ~5000"
        finally:
            if target_path is not None and target_path.exists():
                target_path.unlink()
    print("  T13 PASS — --start-iso → live_call_end carries duration_ms.")


def test_utc_ts_has_millisecond_precision():
    """T14: utc_ts() includes millisecond component so same-second calls
    don't collide on filename (per pr-reviewer R2 + pr-challenger S2)."""
    h = load_helper()
    ts = h.utc_ts()
    # Format: YYYY-MM-DDTHH-MM-SS-NNNmsZ
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}msZ$", ts), (
        f"utc_ts {ts!r} does not match millisecond-precision pattern"
    )
    # Generate two ts in rapid succession; if same wall second, they should
    # still differ on millisecond (at least most of the time — accept a flake
    # by re-trying once).
    a = h.utc_ts()
    b = h.utc_ts()
    if a == b:
        # Force a brief delta and try once more.
        import time as _time
        _time.sleep(0.002)
        b = h.utc_ts()
    assert a != b, "utc_ts collided on rapid successive calls"
    print(f"  T14 PASS — utc_ts has ms precision (sample: {ts}).")


def test_live_dir_separate_from_harvest_dir():
    """T15: raw/live/<source>/ is distinct from raw/<source>/ (per pr-challenger
    C1/C2 on PR #53 — keeps live artifacts away from harvest's compress + dedup
    paths until #39-D explicitly walks raw/live/)."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Simulate harvest's per-source dir
        (root / "raw" / "granola_note").mkdir(parents=True)
        target, _ = h.write_live_artifact(
            source="granola_note",
            query="separation test",
            body="meeting notes",
            content_root=root,
        )
        # Live target is NOT in raw/granola_note/
        harvest_dir = root / "raw" / "granola_note"
        live_dir = root / "raw" / "live" / "granola_note"
        assert target.parent == live_dir
        assert target.parent != harvest_dir
        # Harvest dir is empty (live didn't pollute it)
        assert list(harvest_dir.iterdir()) == []
    print("  T15 PASS — raw/live/<source>/ is distinct from raw/<source>/.")


def test_oversized_body_truncated():
    """T16 (#39-B.2 F4): bodies above MAX_BODY_CHARS are truncated with a marker
    and the function returns body_truncated=True so callers can flag it on the
    metric event. Slack threads with 50+ messages were the motivating case."""
    h = load_helper()
    cap = h.MAX_BODY_CHARS
    # Use a sentinel char that doesn't appear in the marker or comment.
    # 'Q' avoids 'X' (in MAX_BODY_CHARS marker) and any iso-ts characters.
    huge = "Q" * (cap + 1000)
    with tempfile.TemporaryDirectory() as td:
        target, truncated = h.write_live_artifact(
            source="slack_thread",
            query="big thread",
            body=huge,
            content_root=Path(td),
        )
        content = target.read_text()
        assert truncated is True
        assert "[...truncated" in content
        # The body region was truncated to exactly MAX_BODY_CHARS sentinel chars.
        # Past-cap content must NOT be present.
        assert content.count("Q") == cap, (
            f"expected exactly {cap} sentinel chars (truncated); got {content.count('Q')}"
        )
    print(f"  T16 PASS — bodies > MAX_BODY_CHARS={cap} are truncated with marker.")


def test_truncation_marker_built_from_constant():
    """T17a (#55 B1): marker must be derived from MAX_BODY_CHARS so a future
    cap change can't desync the documented number from the actual cap."""
    h = load_helper()
    assert str(h.MAX_BODY_CHARS) in h.TRUNCATION_MARKER, (
        "TRUNCATION_MARKER must reference MAX_BODY_CHARS — was the marker hardcoded?"
    )
    print(f"  T17a PASS — TRUNCATION_MARKER references MAX_BODY_CHARS={h.MAX_BODY_CHARS}.")


def test_cli_emits_body_truncated_flag():
    """T17: when body exceeds the cap, the CLI emits live_call_end with
    body_truncated=True so the dashboard can chart context-overflow risk."""
    target_path: Path | None = None
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        metrics_dir = td_p / "metrics"
        metrics_dir.mkdir()
        env = dict(os.environ)
        env["PA_METRICS_DIR"] = str(metrics_dir)

        h = load_helper()
        # Sentinel 'Q' chosen to avoid collision with iso-ts and marker chars.
        big_body = "Q" * (h.MAX_BODY_CHARS + 500)
        result = subprocess.run(
            [str(TOOL), "--source", "slack_thread", "--query", "oversized thread test"],
            input=big_body,
            env=env, text=True, capture_output=True, check=True,
        )
        target_path = Path(result.stdout.strip())
        try:
            event_files = list(metrics_dir.glob("events-*.jsonl"))
            events = [json.loads(line) for line in event_files[0].read_text().splitlines() if line.strip()]
            end = [e for e in events if e["event"] == "live_call_end"][0]
            assert end["data"]["body_truncated"] is True
            assert end["data"]["status"] == "success"
        finally:
            if target_path is not None and target_path.exists():
                target_path.unlink()
    print("  T17 PASS — CLI propagates body_truncated to live_call_end.")


if __name__ == "__main__":
    print("Running test_live_result_write_acceptance.py...")
    test_write_creates_path_under_live_dir()
    test_provenance_comment_first()
    test_filename_format()
    test_query_hash_deterministic()
    test_query_hash_distinct()
    test_refuses_unknown_source()
    test_refuses_empty_query()
    test_refuses_empty_body()
    test_writes_under_content_root()
    test_cli_emits_live_call_end_success()
    test_cli_rejects_missing_source()
    test_cli_empty_stdin_emits_status_empty()
    test_cli_start_iso_computes_duration_ms()
    test_utc_ts_has_millisecond_precision()
    test_live_dir_separate_from_harvest_dir()
    test_oversized_body_truncated()
    test_truncation_marker_built_from_constant()
    test_cli_emits_body_truncated_flag()
    print("All live-result-write tests passed.")
