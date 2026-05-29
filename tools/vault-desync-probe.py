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

Two signals are checked (state-based, not history-based — history-based
signals like a HEAD-reflog gap are cleared by an innocuous `git checkout
main` while the working tree stays stale, and they're structurally absent
when `core.logAllRefUpdates=false`; both are tested against, see
`tests/test_vault_desync_probe_acceptance.py`):

  1. `.git/AUTO_MERGE` present AND `.git/MERGE_HEAD` absent. AUTO_MERGE
     persists when a merge wrote the auto-resolved tree but didn't
     advance to a commit; MERGE_HEAD presence means a legitimate merge
     is in progress (mid-conflict-resolution), which is NOT the desync
     class. Both files together = normal merge state; AUTO_MERGE alone
     = the post-failed-merge desync.

  2. WT-vs-HEAD tree mismatch surfaces enough D entries to clear a
     "user-intentional-delete" threshold. Specifically, count files in
     HEAD's tree that are absent from the working tree (`git diff
     --diff-filter=D --name-only HEAD`) and fire if the count exceeds
     `MASS_DELETION_THRESHOLD`. Below the threshold, the diff is
     plausibly an in-flight user edit (e.g., `kb-process` moving 5
     memos from `.unprocessed/` to `.processed/`); above it, no realistic
     manual workflow produces that many deletions in one uncommitted
     batch. The May-28 incident had 233 staged D + 5 unstaged D — well
     above the threshold.

Skipped entirely when a legitimate merge is in progress (MERGE_HEAD
present): the probe is a desync gate, not a merge-state gate.

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


# Below this many WT-vs-HEAD deletions the probe treats the diff as a
# plausible in-flight user workflow (kb-process moves, a few-file `git rm`,
# etc.). Above it, no realistic manual flow produces that many uncommitted
# deletions in one batch; the May-28 incident had 238. The threshold is
# deliberately high — false-negatives in the 1-50 range are acceptable
# (recovery still works), but false-positives would block legitimate flows
# and the probe would get bypassed in practice.
MASS_DELETION_THRESHOLD = 50


@dataclass(frozen=True)
class ProbeResult:
    clean: bool
    signals: list[str] = field(default_factory=list)
    vault: Path | None = None


def _run_git(vault: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _git_dir(vault: Path) -> Path:
    """Locate `.git` for the vault — handles both real dirs and worktree files."""
    raw = _run_git(vault, "rev-parse", "--git-dir").stdout.strip()
    p = Path(raw)
    if not p.is_absolute():
        p = vault / p
    return p


def probe(vault: Path) -> ProbeResult:
    """Detect the desync class. Returns ProbeResult.

    Signals are state-based (not history-based) — see module docstring for the
    full rationale, including pr-challenger B1/B3 reproductions on the original
    reflog-based design.
    """
    if not (vault / ".git").exists():
        raise FileNotFoundError(f"{vault} is not a git worktree (no .git found)")

    signals: list[str] = []
    git_dir = _git_dir(vault)
    auto_merge = git_dir / "AUTO_MERGE"
    merge_head = git_dir / "MERGE_HEAD"

    # MERGE_HEAD present = legitimate merge-in-progress. NOT the desync class.
    # The probe is a desync gate, not a merge-state gate — return clean and let
    # the user resolve / abort their merge through normal means.
    if merge_head.exists():
        return ProbeResult(clean=True, signals=[], vault=vault)

    # Signal 1: AUTO_MERGE present without MERGE_HEAD.
    # git only writes AUTO_MERGE during a merge operation. With MERGE_HEAD
    # present this is a normal in-flight merge (handled above). Without
    # MERGE_HEAD, the merge wrote the auto-resolved tree but didn't reach the
    # commit step — that's the May-28 fingerprint exactly.
    if auto_merge.exists():
        rel = (
            auto_merge.relative_to(vault)
            if auto_merge.is_relative_to(vault)
            else auto_merge
        )
        signals.append(f"AUTO_MERGE present without MERGE_HEAD ({rel}) — failed-merge artifact, the May-28 desync fingerprint")

    # Signal 2: WT/HEAD tree mismatch surfacing mass deletions.
    # `git diff --diff-filter=D --name-only HEAD` returns files HEAD tracks
    # that the working tree lacks (combining staged + unstaged D states). On
    # a fresh repo with no HEAD yet, the command errors — treat that as
    # "cannot assess" rather than "clean" (probe is conservative; a brand-new
    # repo has no desync to detect anyway).
    r = _run_git(vault, "diff", "--diff-filter=D", "--name-only", "HEAD", check=False)
    if r.returncode == 0:
        missing = [line for line in r.stdout.splitlines() if line]
        if len(missing) > MASS_DELETION_THRESHOLD:
            signals.append(
                f"{len(missing)} HEAD-tracked files absent from working tree "
                f"(threshold {MASS_DELETION_THRESHOLD}) — likely WT frozen behind HEAD"
            )

    return ProbeResult(clean=not signals, signals=signals, vault=vault)


def _format_banner(result: ProbeResult) -> str:
    if result.clean:
        return f"[vault-desync-probe] clean ({result.vault})"
    lines = [f"[vault-desync-probe] DESYNC DETECTED in {result.vault}:"]
    for sig in result.signals:
        lines.append(f"  - {sig}")
    lines.append(
        f"[vault-desync-probe] Recovery: tools/vault-desync-recover.py {result.vault} "
        "(child #254 of #249 will land the full RELEASE.md runbook)."
    )
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
