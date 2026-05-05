#!/usr/bin/env -S uv run --quiet --with pyyaml --with tiktoken --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6", "tiktoken>=0.7"]
# ///
"""Acceptance tests for #8 — expiry_locked behavior + retrieval recency decay.

Run: tests/test_expiry_acceptance.py
Exits 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # required for @dataclass under py3.14
    spec.loader.exec_module(mod)
    return mod


def test_locked_expiry():
    prune_mod = load_module("prune", PROJ / "tools" / "prune.py")
    tmp = Path(tempfile.mkdtemp(prefix="expiry-test-"))
    try:
        prune_mod.MEMORY_ROOT = tmp
        prune_mod.ARCHIVE_ROOT = tmp / ".archive"

        (tmp / "unlocked-expired.md").write_text(
            "---\nid: a\nsource_uri: file:./tests/x\nsource_kind: doc\n"
            "created_at: '2024-01-01T00:00:00Z'\nexpires_at: '2024-06-01T00:00:00Z'\n"
            "kind: weekly\ntags: []\n---\n\nbody unlocked\n"
        )
        (tmp / "locked-expired.md").write_text(
            "---\nid: b\nsource_uri: file:./tests/y\nsource_kind: doc\n"
            "created_at: '2024-01-01T00:00:00Z'\nexpires_at: '2024-06-01T00:00:00Z'\n"
            "kind: weekly\ntags: []\nexpiry_locked: true\n---\n\nbody locked\n"
        )
        (tmp / "fresh.md").write_text(
            "---\nid: c\nsource_uri: file:./tests/z\nsource_kind: doc\n"
            "created_at: '2026-04-01T00:00:00Z'\nexpires_at: '2027-01-01T00:00:00Z'\n"
            "kind: weekly\ntags: []\n---\n\nbody fresh\n"
        )

        s = prune_mod.prune(now=datetime(2026, 5, 5, tzinfo=timezone.utc), dry_run=False)
        moved_set = set(s.moved)
        remaining = sorted(p.name for p in tmp.iterdir() if p.is_file())
        archived = sorted(p.name for p in (tmp / ".archive").iterdir() if p.is_file()) if (tmp / ".archive").exists() else []

        print("Test 1 — locked-vs-unlocked expiry:")
        print(f"  moved: {s.moved}")
        print(f"  remaining in memory/: {remaining}")
        print(f"  archived: {archived}")
        assert "unlocked-expired.md" in moved_set, "unlocked-expired should have been pruned"
        assert "locked-expired.md" not in moved_set, "locked-expired should NOT have been pruned"
        assert "locked-expired.md" in remaining
        assert "fresh.md" in remaining
        assert s.skipped_locked == 1
        assert "unlocked-expired.md" in archived, "unlocked-expired should be in archive/"
        print("  PASS — F3 mitigation: locked items survive prune past expires_at\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recency_decay():
    route_mod = load_module("route", PROJ / "tools" / "route.py")
    test_mem = Path(tempfile.mkdtemp(prefix="route-test-"))
    try:
        route_mod.MEMORY_ROOT = test_mem
        (test_mem / "older.md").write_text(
            "---\nid: old\nsource_uri: file:./old\nsource_kind: doc\n"
            "created_at: '2025-08-01T00:00:00Z'\nexpires_at: '2027-01-01T00:00:00Z'\n"
            "kind: weekly\ntags: []\n---\n\nThe integration spine pilot conversion baseline is 4 of 7.\n"
        )
        (test_mem / "newer.md").write_text(
            "---\nid: new\nsource_uri: file:./new\nsource_kind: doc\n"
            "created_at: '2026-04-01T00:00:00Z'\nexpires_at: '2027-01-01T00:00:00Z'\n"
            "kind: weekly\ntags: []\n---\n\nThe integration spine pilot conversion baseline is 4 of 7.\n"
        )

        rendered, tokens, files = route_mod.load_memory_objects(
            "integration spine pilot conversion baseline", max_items=2
        )
        basenames = [Path(f).name for f in files]
        print(f"Test 2 — retrieval recency decay (equal-relevance, ~9 month age gap):")
        print(f"  retrieved order: {basenames}")
        older_idx = basenames.index("older.md")
        newer_idx = basenames.index("newer.md")
        print(f"  older.md index: {older_idx}, newer.md index: {newer_idx}")
        assert newer_idx < older_idx, "Newer should rank ahead of older at equal relevance"
        print("  PASS — recency decay penalizes older items at equal relevance (AC3)\n")
    finally:
        shutil.rmtree(test_mem, ignore_errors=True)


if __name__ == "__main__":
    test_locked_expiry()
    test_recency_decay()
    print("=== All #8 acceptance tests passed ===")
