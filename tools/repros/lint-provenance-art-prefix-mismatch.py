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

import datetime as dt
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
    today = dt.date.today().isoformat()  # pr-reviewer nit: no hardcoded dates.
    yyyymmdd = today.replace("-", "")

    # Minimal vault skeleton: .assistant.local.json + kb/ + projects/
    (vault / "kb").mkdir(parents=True, exist_ok=True)
    _write(vault / "kb" / "people.md", "# People\n")
    _write(vault / "kb" / "org.md", "# Org\n")
    _write(vault / "kb" / "decisions.md", "# Decisions\n")

    # Project slug per ADR-0003 Amendment 1: <YYYYMMDD>-<name>-<4hex>
    proj_slug = f"{yyyymmdd}-repro-{art_uuid[:4]}"
    proj = vault / "projects" / proj_slug
    proj.mkdir(parents=True, exist_ok=True)
    _write(
        proj / "project.md",
        "---\n"
        f"project_id: {proj_slug}\n"
        "status: active\n"
        f"created_at: {today}\n"
        f"last_active: {today}\n"
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
        f"created_at: {today}\n"
        "produced_by:\n"
        "  session_id: repro-session\n"
        "  model: claude-opus-4-7\n"
        "  query: c1-repro-fixture\n"
        "  sources_cited:\n"
        f"    - art://{art_id}\n"  # the offending self-reference
        "title: \"Repro memo\"\n"
        "audience: [\"repro\"]\n"
        "---\n\n"
        "# Repro memo\n\n"
        "This memo's sources_cited contains a self-reference to its own art-id.\n"
        "Today's lint should report this as `artefact-dangling-art-ref` despite\n"
        "the file existing at the expected location — that's the bug.\n",
    )
    return art_id


def _run_lint(vault: Path) -> tuple[int, str, str]:
    """Run lint-provenance.py --require-vault --content-root <vault>.

    Per pr-challenger B1 + pr-reviewer on #195: uses the `--content-root`
    CLI flag added in this same PR so the repro doesn't have to mutate the
    method repo's `.assistant.local.json`. The previous design's signal-
    safety hole (SIGTERM/SIGKILL skipping finally, concurrent invocations
    racing the config file) is gone — no shared mutable state to corrupt."""
    r = subprocess.run(
        [str(LINT), "--require-vault", "--content-root", str(vault)],
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stdout, r.stderr


def main() -> int:
    if not LINT.is_file():
        print(f"REPRO MECHANISM BROKEN: lint-provenance.py not at {LINT}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="lint-art-prefix-repro-") as td:
        vault = Path(td) / "vault"
        vault.mkdir()
        art_id = _build_fixture(vault)
        rc, stdout, stderr = _run_lint(vault)

    combined = stdout + "\n" + stderr
    # Per pr-challenger S2 on #195: tighten the falsifier match. Find the
    # specific violation line that mentions BOTH the target kind AND our
    # fixture's art_id (so unrelated rules firing the same kind on a
    # different fixture artefact can't masquerade as our bug).
    target_lines = [
        line for line in combined.splitlines()
        if TARGET_VIOLATION in line and f"art://{art_id}" in line
    ]
    bug_fingerprint_present = len(target_lines) >= 1

    if rc != 0 and bug_fingerprint_present:
        print("BUG REPRODUCED")
        print(f"  fixture art-id: {art_id}")
        print(f"  lint exit code: {rc}")
        print(f"  violation: {target_lines[0].strip()}")
        if len(target_lines) > 1:
            # Multiple hits on the same fingerprint shouldn't happen on a
            # minimal fixture; surface them as a heads-up.
            print(f"  (unexpected: {len(target_lines)} matching lines)")
        return 0

    if not bug_fingerprint_present:
        # Per pr-challenger S3 on #195: BUG GONE covers any clean outcome
        # where the targeted violation is absent — even if lint emits an
        # unrelated warning on the fixture (rc != 0 for a different
        # reason). Otherwise C3 would have to land "lint exits 0 on the
        # fixture" in addition to fixing the prefix mismatch — an
        # over-constraint that the script shouldn't enforce.
        print("BUG GONE — targeted violation absent on the self-referencing fixture")
        print(f"  fixture art-id: {art_id}")
        print(f"  lint exit code: {rc}")
        if rc != 0:
            # Surface what lint DID complain about, so the operator can
            # tell whether C3's fix is clean or shifted to a new problem.
            tail = "\n".join(combined.splitlines()[-10:])
            print(f"  lint output (tail):\n{tail}")
        print("  Falsifier F1 of #193 fired: bug does not exist as described,")
        print("  OR the fix in C3 has landed.")
        return 1

    # Should be unreachable: bug_fingerprint_present is True but rc == 0.
    # That would mean lint reported the target violation as a non-error,
    # which is shape-shifting we should surface for operator review.
    print(
        f"REPRO MECHANISM BROKEN: target violation present in output but lint exited {rc}",
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
