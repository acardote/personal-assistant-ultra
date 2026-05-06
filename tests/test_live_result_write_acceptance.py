#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/live-result-write.py (#39-B.1).

Verifies the helper writes live-fetched artifacts to the right location
with the right provenance comment, emits the live_call_end metric event,
and refuses bad input.

Tests:
  T1  — write_live_artifact creates the expected path under raw/<source>/.
  T2  — File contents start with the provenance HTML comment.
  T3  — Filename uses live-<ts>-<hash>.md format with a hex query-hash.
  T4  — Same query produces the same hash (deterministic).
  T5  — Different queries produce different hashes.
  T6  — Refuses unknown --source.
  T7  — Refuses empty query.
  T8  — Refuses empty body (zero-byte artifact).
  T9  — Uses content_root from config (writes under <content_root>/raw/).
  T10 — CLI emits live_call_end event with source, query_hash, bytes_written.
  T11 — CLI exits non-zero on missing --source.
  T12 — CLI exits 3 on empty stdin.
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


def test_write_creates_path_under_source_dir():
    """T1: target path is <content_root>/raw/<source>/live-<ts>-<hash>.md."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = h.write_live_artifact(
            source="granola_note",
            query="what's up with Acko?",
            body="Some meeting notes.",
            content_root=root,
        )
        expected_dir = root / "raw" / "granola_note"
        assert target.parent == expected_dir, f"expected dir {expected_dir}, got {target.parent}"
        assert target.exists()
        assert target.name.startswith("live-")
        assert target.suffix == ".md"
    print("  T1 PASS — write creates path under raw/<source>/.")


def test_provenance_comment_first():
    """T2: first line of file is the HTML provenance comment."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        target = h.write_live_artifact(
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
    """T3: filename is live-<ts>-<8hex>.md."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        target = h.write_live_artifact(
            source="gmail_thread",
            query="renewal status",
            body="email body",
            content_root=Path(td),
        )
        # filename = live-2026-05-06T12-30-00Z-abcdef12.md
        parts = target.stem.split("-")
        # parts: ["live", "2026", "05", "06T12", "30", "00Z", <hash>]
        # The hash is the LAST part regardless of how the ts splits.
        assert parts[0] == "live"
        last = parts[-1]
        assert len(last) == h.QUERY_HASH_LEN, f"hash length {len(last)} != {h.QUERY_HASH_LEN}"
        # All hex chars
        int(last, 16)
    print(f"  T3 PASS — filename is live-<ts>-<{h.QUERY_HASH_LEN}hex>.md.")


def test_query_hash_deterministic():
    """T4: same query → same hash."""
    h = load_helper()
    a = h.query_hash("what's up with Acko?")
    b = h.query_hash("what's up with Acko?")
    assert a == b
    print("  T4 PASS — same query → same hash.")


def test_query_hash_distinct():
    """T5: different queries → different hashes (collisions are vanishingly rare in 8hex)."""
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
    """T8: empty/whitespace-only body raises (zero-byte artifacts pollute the corpus)."""
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
    """T9: target is under content_root/raw/, not method root."""
    h = load_helper()
    with tempfile.TemporaryDirectory() as td:
        custom_root = Path(td) / "custom_vault"
        custom_root.mkdir()
        target = h.write_live_artifact(
            source="granola_note",
            query="q",
            body="b",
            content_root=custom_root,
        )
    # target must start with custom_root, not the method root.
    assert str(target).startswith(str(custom_root)), (
        f"target {target} not under {custom_root}"
    )
    assert "raw/granola_note" in str(target)
    print("  T9 PASS — write respects passed content_root.")


def test_cli_emits_live_call_end():
    """T10: CLI emits a live_call_end event with the expected payload."""
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        # Set up a content_root via .assistant.local.json and an isolated metrics dir.
        content_root = td_p / "vault"
        content_root.mkdir()
        metrics_dir = td_p / "metrics"
        metrics_dir.mkdir()

        # Create .assistant.local.json in a custom method root via env override.
        # _metrics honors PA_METRICS_DIR; _config reads from the method-repo root.
        # The simplest path is to use the real method root and let the CLI write
        # to <real_content_root>/raw/, then check the metrics dir directly.
        env = dict(os.environ)
        env["PA_METRICS_DIR"] = str(metrics_dir)
        env["PA_CONTENT_ROOT"] = str(content_root)  # _metrics fallback path uses this

        # We need _config to also see content_root. _config reads .assistant.local.json
        # from METHOD_ROOT (which is the project root). We can't easily override that
        # without writing in-place. Workaround: use a temporary .assistant.local.json
        # if absent, or test against the real one. The cleanest path is to write to
        # the real config target and assert the path. But that pollutes the user's
        # real vault. Use a different approach: assert on the _emit_ side only —
        # write to the real content_root (already configured) and check the event.
        # Actually we already have a real .assistant.local.json. So just run the CLI
        # against it and check the metrics event.

        # Skip the content_root override; rely on the real config and just isolate metrics.
        env.pop("PA_CONTENT_ROOT", None)

        result = subprocess.run(
            [str(TOOL), "--source", "granola_note", "--query", "test integration query"],
            input="Integration test body for #39-B.1.\n",
            env=env, text=True, capture_output=True, check=True,
        )
        target_str = result.stdout.strip()
        target = Path(target_str)
        assert target.exists(), f"target {target} not written"
        assert target.name.startswith("live-")

        # Find the events file and look for live_call_end.
        event_files = list(metrics_dir.glob("events-*.jsonl"))
        assert event_files, "no events file written"
        events = [json.loads(line) for line in event_files[0].read_text().splitlines() if line.strip()]
        live_ends = [e for e in events if e["event"] == "live_call_end"]
        assert len(live_ends) == 1, f"expected 1 live_call_end, got {len(live_ends)}"
        payload = live_ends[0]["data"]
        assert payload["source"] == "granola_note"
        assert "query_hash" in payload and len(payload["query_hash"]) == 8
        assert payload["bytes_written"] > 0

        # Cleanup the file we wrote so we don't pollute the real vault.
        target.unlink()
    print("  T10 PASS — CLI emits live_call_end event with expected payload.")


def test_cli_rejects_missing_source():
    """T11: CLI exits non-zero when --source is missing."""
    result = subprocess.run(
        [str(TOOL), "--query", "q"],
        input="body",
        text=True, capture_output=True,
    )
    assert result.returncode != 0, "CLI should fail without --source"
    print("  T11 PASS — CLI rejects missing --source.")


def test_cli_rejects_empty_stdin():
    """T12: CLI exits 3 when stdin is empty."""
    result = subprocess.run(
        [str(TOOL), "--source", "granola_note", "--query", "q"],
        input="",
        text=True, capture_output=True,
    )
    assert result.returncode == 3, f"expected exit 3 on empty stdin, got {result.returncode}"
    print("  T12 PASS — CLI exits 3 on empty stdin.")


if __name__ == "__main__":
    print("Running test_live_result_write_acceptance.py...")
    test_write_creates_path_under_source_dir()
    test_provenance_comment_first()
    test_filename_format()
    test_query_hash_deterministic()
    test_query_hash_distinct()
    test_refuses_unknown_source()
    test_refuses_empty_query()
    test_refuses_empty_body()
    test_writes_under_content_root()
    test_cli_emits_live_call_end()
    test_cli_rejects_missing_source()
    test_cli_rejects_empty_stdin()
    print("All live-result-write tests passed.")
