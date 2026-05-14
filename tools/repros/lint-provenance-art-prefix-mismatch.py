#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Minimal repro for the lint-provenance art:// prefix-mismatch bug (#193).

Constructs a tempdir vault with one `artefacts/memo/art-<u>.md` whose
`sources_cited` is `art://art-<u>` (a self-reference to the file's own
id), runs `tools/lint-provenance.py --require-vault` against it, and
reports whether the bug is reproducible.

The bug: the index keyed by bare UUIDs at `tools/lint-provenance.py:839`
doesn't match the URI body shape used at `:706`, which includes the
`art-` prefix. A self-referencing `art://art-<u>` ref is reported as
dangling even though the file is right there.

Exit codes:
- 0: bug REPRODUCED (today's expected behavior — falsifier F1 NOT fired)
- 1: bug GONE (post-C3 expected behavior; or falsifier F1 fired)
- 2: repro mechanism itself broke (lint-provenance missing, fixture
     failed to construct, etc.) — needs operator attention.

Per C1 of #193 (closer #194). Same script becomes the post-fix smoke
test in C3 — when C3 lands, this exits 1 and the operator can verify
the fix end-to-end with one command.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LINT = REPO_ROOT / "tools" / "lint-provenance.py"
TARGET_VIOLATION = "artefact-dangling-art-ref"


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _build_fixture(vault: Path) -> str:
    """Drop one project + one memo artefact whose sources_cited self-refers.
    Returns the art-id used for the fixture."""
    art_uuid = str(uuid.uuid4())
    art_id = f"art-{art_uuid}"

    # Minimal vault skeleton: .assistant.local.json + kb/ + projects/
    (vault / "kb").mkdir(parents=True, exist_ok=True)
    _write(vault / "kb" / "people.md", "# People\n")
    _write(vault / "kb" / "org.md", "# Org\n")
    _write(vault / "kb" / "decisions.md", "# Decisions\n")

    # Project slug per ADR-0003 Amendment 1: <YYYYMMDD>-<name>-<4hex>
    proj_slug = f"20260514-repro-{art_uuid[:4]}"
    proj = vault / "projects" / proj_slug
    proj.mkdir(parents=True, exist_ok=True)
    _write(
        proj / "project.md",
        "---\n"
        f"project_id: {proj_slug}\n"
        "status: active\n"
        "created_at: 2026-05-14\n"
        "last_active: 2026-05-14\n"
        "---\n\n"
        "# Repro project\n",
    )

    # The memo whose sources_cited self-refers via art://art-<u>
    memo_path = proj / "artefacts" / "memo" / f"{art_id}.md"
    _write(
        memo_path,
        "---\n"
        f"id: {art_id}\n"
        "kind: memo\n"
        f"project_id: {proj_slug}\n"
        "created_at: 2026-05-14\n"
        "produced_by:\n"
        "  session_id: repro-session\n"
        "  model: claude-opus-4-7\n"
        "  query: c1-repro-fixture\n"
        "  sources_cited:\n"
        f"    - art://{art_id}\n"  # the offending self-reference
        "title: \"Repro memo\"\n"
        "audience: [\"#193 reviewer\"]\n"
        "---\n\n"
        "# Repro memo\n\n"
        "This memo's sources_cited contains a self-reference to its own art-id.\n"
        "Today's lint should report this as `artefact-dangling-art-ref` despite\n"
        "the file existing at the expected location — that's the bug.\n",
    )
    return art_id


def _run_lint(vault: Path) -> tuple[int, str, str]:
    """Run lint-provenance.py --require-vault against the fixture vault.
    Returns (returncode, stdout, stderr)."""
    # Point lint at the fixture via .assistant.local.json at the method root.
    config = REPO_ROOT / ".assistant.local.json"
    original_config_text = config.read_text(encoding="utf-8") if config.is_file() else None
    try:
        config.write_text(
            json.dumps({"paths": {"content_root": str(vault)}}, indent=2),
            encoding="utf-8",
        )
        r = subprocess.run(
            [str(LINT), "--require-vault"],
            capture_output=True,
            text=True,
        )
        return r.returncode, r.stdout, r.stderr
    finally:
        if original_config_text is None:
            try:
                config.unlink()
            except FileNotFoundError:
                pass
        else:
            config.write_text(original_config_text, encoding="utf-8")


def main() -> int:
    if not LINT.is_file():
        print(f"REPRO MECHANISM BROKEN: lint-provenance.py not at {LINT}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="lint-art-prefix-repro-") as td:
        vault = Path(td) / "vault"
        vault.mkdir()
        art_id = _build_fixture(vault)
        rc, stdout, stderr = _run_lint(vault)

    # Today (pre-fix): lint exits non-zero AND stdout-or-stderr contains
    # TARGET_VIOLATION on the self-referencing fixture. That's the bug.
    # (lint-provenance writes violations to stderr; we check both streams
    # to be robust across version drift.)
    combined = stdout + "\n" + stderr
    has_target_violation = TARGET_VIOLATION in combined
    if rc != 0 and has_target_violation:
        # Find the exact violation line for evidence.
        violation_lines = [
            line for line in combined.splitlines() if TARGET_VIOLATION in line and art_id in line
        ]
        print("BUG REPRODUCED")
        print(f"  fixture art-id: {art_id}")
        print(f"  lint exit code: {rc}")
        if violation_lines:
            print(f"  violation: {violation_lines[0].strip()}")
        else:
            # The violation fired but we couldn't find the exact line — still
            # the bug, but the evidence is less precise. Surface tails.
            print("  (couldn't isolate the exact violation line; showing tails)")
            for line in combined.splitlines()[-5:]:
                print(f"  | {line}")
        return 0

    if rc == 0:
        print("BUG GONE — lint is clean on the self-referencing fixture")
        print("  Falsifier F1 of #193 fired: bug does not exist as described,")
        print("  OR the fix in C3 has landed and this script flipped to its")
        print("  post-fix mode. Verify by inspecting recent commits to")
        print("  tools/lint-provenance.py.")
        return 1

    # lint failed but for a different violation than the one we're testing
    print(
        f"REPRO MECHANISM BROKEN: lint exited {rc} but TARGET_VIOLATION not in stdout/stderr",
        file=sys.stderr,
    )
    print(f"  fixture art-id: {art_id}", file=sys.stderr)
    print("  stdout tail:", file=sys.stderr)
    for line in stdout.splitlines()[-10:]:
        print(f"  | {line}", file=sys.stderr)
    print("  stderr tail:", file=sys.stderr)
    for line in stderr.splitlines()[-5:]:
        print(f"  | {line}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
