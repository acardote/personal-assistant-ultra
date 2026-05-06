#!/usr/bin/env -S uv run --quiet --with jsonschema --with pyyaml --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4", "pyyaml>=6"]
# ///
"""Acceptance tests for tools/live-writeback.py + compress.py --provenance (#39-D).

Tests:
  T1  — compress.derive_memory_path strips `live/` segment when provenance=live.
  T2  — compress.derive_memory_path leaves path unchanged when provenance is None or "harvest".
  T3  — find_unprocessed walks raw/live/<source>/, skips .processed/ and non-md.
  T4  — find_unprocessed returns empty when raw/live/ doesn't exist.
  T5  — find_unprocessed honors --source filtering.
  T6  — mark_processed moves the file into .processed/, preserves filename.
  T7  — CLI --dry-run lists targets and exits 0 without moving anything.
  T8  — CLI --source <invalid> rejected by argparse.
  T9  — compress.py --provenance live refuses harvest-shape raw path (per #64 C1).
  T10 — compress.py --provenance harvest refuses raw/live/ path (symmetric).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_writeback():
    sys.modules.pop("lwb_test", None)
    spec = importlib.util.spec_from_file_location("lwb_test", str(PROJ / "tools" / "live-writeback.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["lwb_test"] = m
    spec.loader.exec_module(m)
    return m


def load_compress():
    sys.modules.pop("compress_test", None)
    spec = importlib.util.spec_from_file_location("compress_test", str(PROJ / "tools" / "compress.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["compress_test"] = m
    spec.loader.exec_module(m)
    return m


def make_live_artifact(content_root: Path, source: str, name: str = "2026-05-06T10-00-00-000msZ-abcdef12.md") -> Path:
    d = content_root / "raw" / "live" / source
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("<!-- live-fetched -->\nbody content\n", encoding="utf-8")
    return p


def test_derive_memory_path_strips_live_segment():
    """T1: provenance=live → memory path drops `live/` so it lands alongside harvest memory."""
    cm = load_compress()
    # Build a synthetic raw_path under cm.RAW_ROOT. cm.RAW_ROOT is hardcoded to
    # the configured content_root's raw/. We can't easily fake that without
    # remounting; use Path arithmetic and assert on the relative shape.
    raw_root = cm.RAW_ROOT
    # Simulate: raw/live/granola_note/<file>.md under raw_root.
    fake_raw = raw_root / "live" / "granola_note" / "test-fixture.md"
    out = cm.derive_memory_path(fake_raw, "granola_note", provenance="live")
    rel = out.relative_to(cm.MEMORY_ROOT)
    parts = rel.parts
    assert parts[0] == "granola_note", f"expected leading 'granola_note', got {parts}"
    assert "live" not in parts, f"'live' should be stripped from memory path; got {parts}"
    assert parts[-1] == "test-fixture.md"
    print("  T1 PASS — provenance=live strips `live/` from memory path.")


def test_derive_memory_path_unchanged_without_live_provenance():
    """T2: harvest path or no provenance leaves the memory path unchanged."""
    cm = load_compress()
    raw_root = cm.RAW_ROOT
    # Harvest-shape: raw/granola_note/<file>.md
    fake_raw = raw_root / "granola_note" / "harvest-fixture.md"
    out_no = cm.derive_memory_path(fake_raw, "granola_note", provenance=None)
    out_harvest = cm.derive_memory_path(fake_raw, "granola_note", provenance="harvest")
    expected = cm.MEMORY_ROOT / "granola_note" / "harvest-fixture.md"
    assert out_no == expected
    assert out_harvest == expected
    print("  T2 PASS — provenance None/harvest leaves memory path unchanged.")


def test_find_unprocessed_walks_live_subtree():
    """T3: walker enumerates raw/live/<source>/*.md, skips .processed/."""
    lwb = load_writeback()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        a = make_live_artifact(root, "granola_note", "a.md")
        b = make_live_artifact(root, "slack_thread", "b.md")
        # already-processed file under .processed/
        proc_dir = root / "raw" / "live" / "granola_note" / ".processed"
        proc_dir.mkdir()
        (proc_dir / "old.md").write_text("processed", encoding="utf-8")
        # non-md file should be skipped
        (root / "raw" / "live" / "granola_note" / "skip.txt").write_text("nope", encoding="utf-8")

        result = lwb.find_unprocessed(root, ["granola_note", "slack_thread"])
        names = sorted([(s, p.name) for s, p in result])
    assert names == [("granola_note", "a.md"), ("slack_thread", "b.md")], (
        f"unexpected: {names}"
    )
    print("  T3 PASS — find_unprocessed enumerates raw/live/<src>/*.md correctly.")


def test_find_unprocessed_missing_dirs():
    """T4: missing raw/live/<source>/ subtree → empty list."""
    lwb = load_writeback()
    with tempfile.TemporaryDirectory() as td:
        result = lwb.find_unprocessed(Path(td), ["granola_note", "slack_thread", "gmail_thread"])
    assert result == []
    print("  T4 PASS — missing dirs → empty target list.")


def test_find_unprocessed_source_filter():
    """T5: --source filter walks only the named source."""
    lwb = load_writeback()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_live_artifact(root, "granola_note")
        make_live_artifact(root, "slack_thread")
        result = lwb.find_unprocessed(root, ["granola_note"])
        sources_seen = {s for s, _ in result}
    assert sources_seen == {"granola_note"}
    print("  T5 PASS — source filter respected.")


def test_mark_processed_moves_file():
    """T6: mark_processed moves file into per-source .processed/."""
    lwb = load_writeback()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = make_live_artifact(root, "granola_note", "moveme.md")
        lwb.mark_processed(p)
        assert not p.exists()
        moved = p.parent / ".processed" / "moveme.md"
        assert moved.exists()
        assert moved.read_text().startswith("<!--")
    print("  T6 PASS — mark_processed moves file under .processed/.")


def test_cli_dry_run():
    """T7: --dry-run lists targets, exits 0, doesn't move anything."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Set up a fake content_root via temporary .assistant.local.json.
        # Easier: just test the helper functions directly. The CLI exercises
        # the real config path; in CI we'd want a richer fixture. Smoke
        # test here: invoke CLI on the real config (no harm — dry-run only).
        result = subprocess.run(
            [str(PROJ / "tools" / "live-writeback.py"), "--dry-run"],
            check=False, capture_output=True, text=True,
        )
    # Exit 0 regardless of how many real artifacts exist.
    assert result.returncode == 0, f"dry-run exited {result.returncode}: {result.stderr}"
    # Output starts with the dry-run prefix
    assert "dry-run:" in result.stdout
    print("  T7 PASS — --dry-run exits 0 with dry-run preface.")


def test_cli_invalid_source():
    """T8: --source with an unknown value rejected by argparse."""
    result = subprocess.run(
        [str(PROJ / "tools" / "live-writeback.py"), "--source", "invented_source"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "invalid choice" in result.stderr or "invented_source" in result.stderr
    print("  T8 PASS — invalid --source rejected.")


def test_provenance_live_refuses_harvest_path():
    """T9 (#64 challenger C1): --provenance live on a raw path NOT under
    raw/live/ exits 2. Prevents silent mislabeling of harvest-shape memory
    objects as live-fetched."""
    # Run compress.py against an invalid path/flag combination. The check
    # happens before any LLM call, so we don't need to set up a model.
    # Use a fake raw path that exists but isn't under raw/live/.
    with tempfile.TemporaryDirectory() as td:
        # Build a path under what would be RAW_ROOT (we can't easily mock
        # RAW_ROOT, so use the real one — write a fixture file there).
        # Compress reads RAW_ROOT from _config; resolving requires the file
        # to exist UNDER RAW_ROOT. The check at line ~187 fails first if
        # not under RAW_ROOT, so we need a real RAW_ROOT-relative path.
        cm = load_compress()
        fixture = cm.RAW_ROOT / "granola_note" / "_test-provenance-fixture.md"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text("test\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [str(PROJ / "tools" / "compress.py"), str(fixture),
                 "--source-kind", "granola_note", "--provenance", "live"],
                check=False, capture_output=True, text=True,
            )
            assert result.returncode == 2, (
                f"expected exit 2, got {result.returncode}: {result.stderr[:300]}"
            )
            assert "--provenance live requires" in result.stderr
        finally:
            fixture.unlink(missing_ok=True)
    print("  T9 PASS — --provenance live refuses harvest-shape raw path.")


def test_provenance_harvest_refuses_live_path():
    """T10 (#64 challenger C1, symmetric): --provenance harvest on a path
    UNDER raw/live/ exits 2. Symmetric guard against the inverse mislabel."""
    with tempfile.TemporaryDirectory() as td:
        cm = load_compress()
        fixture = cm.RAW_ROOT / "live" / "granola_note" / "_test-provenance-fixture.md"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text("test\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [str(PROJ / "tools" / "compress.py"), str(fixture),
                 "--source-kind", "granola_note", "--provenance", "harvest"],
                check=False, capture_output=True, text=True,
            )
            assert result.returncode == 2, (
                f"expected exit 2, got {result.returncode}: {result.stderr[:300]}"
            )
            assert "--provenance harvest is incompatible" in result.stderr
        finally:
            fixture.unlink(missing_ok=True)
    print("  T10 PASS — --provenance harvest refuses raw/live/ path.")


if __name__ == "__main__":
    print("Running test_live_writeback_acceptance.py...")
    test_derive_memory_path_strips_live_segment()
    test_derive_memory_path_unchanged_without_live_provenance()
    test_find_unprocessed_walks_live_subtree()
    test_find_unprocessed_missing_dirs()
    test_find_unprocessed_source_filter()
    test_mark_processed_moves_file()
    test_cli_dry_run()
    test_cli_invalid_source()
    test_provenance_live_refuses_harvest_path()
    test_provenance_harvest_refuses_live_path()
    print("All live-writeback tests passed.")
