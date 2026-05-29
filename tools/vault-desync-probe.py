#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Detect the vault main-worktree desync class (Child #251 of #249).

The desync: `refs/heads/main` (or another branch's ref) advances while
HEAD reflog records nothing for the move — typically because a tool
mutated the ref from outside a HEAD-aware operation. The working tree
remains frozen at the old ref state. The next `git merge`-class
operation captures the gap as staged "deletions" via `.git/AUTO_MERGE`,
and the operator sees a working tree gutted of files that exist in HEAD.

Reference incident: parent #249 evidence comments document the 2026-05-28
forensic walk. The probe in this file is the O(1) signal those comments
identified as cheaply detectable.

Two signals are checked:

  1. `.git/AUTO_MERGE` artifact present (definitive — git only writes
     this during a merge operation, and the file persists if the
     operation was aborted or interrupted before the commit step).

  2. HEAD reflog top != current HEAD value (smoking-gun signature). When
     HEAD is a symref to a branch and the branch advances WITHOUT a
     HEAD-aware command, HEAD's reflog isn't updated. Subsequent ops see
     `git reflog show HEAD -1` returning an older SHA than
     `git rev-parse HEAD`.

Either signal alone is sufficient to refuse a vault-touching operation.
Both together are the May 28 fingerprint exactly.

Usage:
    tools/vault-desync-probe.py <vault-path>           # human-readable banner
    tools/vault-desync-probe.py <vault-path> --quiet   # exit code only
    tools/vault-desync-probe.py <vault-path> --json    # structured output

Exit codes:
  0 — clean (no desync signal)
  1 — desync detected (at least one signal fired)
  2 — invocation error (path not a git worktree, etc.)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProbeResult:
    clean: bool
    signals: list[str] = field(default_factory=list)
    vault: Path | None = None


def _run_git(vault: Path, *args: str) -> str:
    """Run a git command in the vault, return stdout. Raises on non-zero."""
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _git_dir(vault: Path) -> Path:
    """Locate `.git` for the vault — handles both real dirs and worktree files."""
    raw = _run_git(vault, "rev-parse", "--git-dir").strip()
    p = Path(raw)
    if not p.is_absolute():
        p = vault / p
    return p


def probe(vault: Path) -> ProbeResult:
    """Detect the desync class. Returns ProbeResult."""
    if not (vault / ".git").exists():
        raise FileNotFoundError(f"{vault} is not a git worktree (no .git found)")

    signals: list[str] = []

    # Signal 1: .git/AUTO_MERGE artifact (definitive).
    # Use rev-parse --git-dir to handle worktree-file .git pointers correctly.
    git_dir = _git_dir(vault)
    auto_merge = git_dir / "AUTO_MERGE"
    if auto_merge.exists():
        signals.append(f"AUTO_MERGE artifact present at {auto_merge.relative_to(vault) if auto_merge.is_relative_to(vault) else auto_merge}")

    # Signal 2: HEAD reflog top != current HEAD.
    # When HEAD is a symref and its branch moved without a HEAD-aware op,
    # the reflog top is the older value.
    current_head = _run_git(vault, "rev-parse", "HEAD").strip()
    try:
        # `git reflog show HEAD -n 1 --format=%H` would be cleanest, but
        # not all git versions accept --format on reflog. Use plumbing:
        # read .git/logs/HEAD's last line and parse "<old> <new> ..."
        head_log = git_dir / "logs" / "HEAD"
        if head_log.exists():
            last = head_log.read_text(encoding="utf-8").rstrip("\n").splitlines()[-1]
            # Format: "<old-sha> <new-sha> <ident> <ts> <tz>\tmsg"
            new_sha = last.split(" ", 2)[1]
            if new_sha != current_head:
                signals.append(
                    f"HEAD reflog top ({new_sha[:8]}) != current HEAD ({current_head[:8]}) — "
                    f"ref advanced without a HEAD-aware operation"
                )
    except (IndexError, ValueError):
        # Empty or malformed reflog — skip this signal rather than false-positive.
        pass

    return ProbeResult(clean=not signals, signals=signals, vault=vault)


def _format_banner(result: ProbeResult) -> str:
    if result.clean:
        return f"[vault-desync-probe] clean ({result.vault})"
    lines = [f"[vault-desync-probe] DESYNC DETECTED in {result.vault}:"]
    for sig in result.signals:
        lines.append(f"  - {sig}")
    lines.append("[vault-desync-probe] Recovery: see RUNBOOK.md (Child #252 of #249 lands the one-shot helper).")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(
        prog="vault-desync-probe",
        description="Detect the vault main-worktree desync class (Child #251 of #249).",
    )
    p.add_argument("vault", help="Path to the vault (a git worktree).")
    p.add_argument(
        "--quiet", action="store_true",
        help="Suppress output; rely on exit code.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Print structured JSON to stdout.",
    )
    args = p.parse_args()

    vault = Path(args.vault).resolve()
    try:
        result = probe(vault)
    except FileNotFoundError as e:
        if not args.quiet:
            print(f"[vault-desync-probe] {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "clean": result.clean,
            "signals": result.signals,
            "vault": str(result.vault),
        }, indent=2))
    elif not args.quiet:
        out = _format_banner(result)
        print(out, file=sys.stdout if result.clean else sys.stderr)

    return 0 if result.clean else 1


if __name__ == "__main__":
    sys.exit(main())
