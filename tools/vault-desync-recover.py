#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""One-shot recovery from the vault main-worktree desync class (Child #252 of #249).

The desync class (see parent #249's evidence): `refs/heads/main` advances while
the working tree stays frozen at an earlier commit. The next `git merge`-class
operation captures the gap as staged "deletions" via `.git/AUTO_MERGE`. The
operator sees a working tree gutted of files that exist in HEAD.

This tool reverses that state without losing user-uncommitted edits:

  1. Detect the desync via `tools/vault-desync-probe.py`. If clean, exit 0
     with "nothing to recover" — the tool is idempotent and safe to run
     against any vault.

  2. Restore HEAD-tracked files that are absent from the working tree.
     Iterates over `git diff --diff-filter=D --name-only HEAD` and runs
     `git checkout HEAD -- <path>` per file. Both staged and unstaged D
     entries are restored — the desync produces both depending on when
     in the lifecycle the operator notices.

  3. Remove `.git/AUTO_MERGE` (and any related auto-merge artifacts) once
     the desync's primary signature is gone. `MERGE_HEAD` is left alone
     because it would only be present during a legitimate merge (the
     probe wouldn't have fired in that case).

  4. Re-run the probe to verify recovery. Exit non-0 if the desync persists
     (suggests a deeper issue that needs operator inspection).

What this tool does NOT touch:
  - Modifications staged in the index (`M ` in `git status`). These could be
    legitimate uncommitted edits — recovery should not silently revert them.
  - Working-tree modifications (` M`). Same reasoning.
  - Untracked files. Operator-authored outputs (e.g., `.processed/` memos)
    are never touched.

If the desync state included modifications that the operator wants to discard,
the operator should run the recovery first (this tool) and then choose what
to do with the surviving modifications via `git restore`.

Usage:
    tools/vault-desync-recover.py <vault-path>            # interactive (asks before each phase)
    tools/vault-desync-recover.py <vault-path> --yes      # non-interactive (default in scripts)
    tools/vault-desync-recover.py <vault-path> --dry-run  # show what would be restored, no changes

Exit codes:
  0 — clean (either nothing to recover, or recovery succeeded and probe is now clean)
  1 — recovery ran but probe still fires after (deeper desync; manual inspection needed)
  2 — invocation error (path not a git worktree, etc.)
  3 — operator declined the interactive confirmation
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parent / "vault-desync-probe.py"
AUTO_MERGE_ARTIFACTS = ("AUTO_MERGE",)  # NOT MERGE_HEAD — that means legitimate merge


def _git(vault: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True, text=True, check=check,
    )


def _git_dir(vault: Path) -> Path:
    raw = _git(vault, "rev-parse", "--git-dir").stdout.strip()
    p = Path(raw)
    if not p.is_absolute():
        p = vault / p
    return p


def _probe(vault: Path) -> tuple[int, str]:
    """Run the desync probe; return (exit_code, stderr_banner)."""
    if not PROBE.is_file():
        # Probe not present (older method-repo checkout). Treat as "cannot assess".
        return 0, "[vault-desync-recover] probe missing — skipping pre-check"
    r = subprocess.run(
        [str(PROBE), str(vault)],
        capture_output=True, text=True,
    )
    return r.returncode, r.stderr


def _deletions(vault: Path) -> list[str]:
    """Return HEAD-tracked files absent from the working tree (staged + unstaged D)."""
    r = _git(vault, "diff", "--diff-filter=D", "--name-only", "HEAD", check=False)
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line]


def _restore_from_head(vault: Path, paths: list[str]) -> None:
    """git checkout HEAD -- <paths> in chunks to avoid argv-length limits.

    Chunking matters here — the May-28 incident had 238 paths. POSIX ARG_MAX
    is usually generous (256KB+), but linked-worktree path lengths can be
    long and we'd rather not depend on the limit.
    """
    CHUNK = 100
    for i in range(0, len(paths), CHUNK):
        chunk = paths[i : i + CHUNK]
        _git(vault, "checkout", "HEAD", "--", *chunk)


def _clear_auto_merge(vault: Path) -> list[str]:
    """Remove `.git/AUTO_MERGE` if present. Return the names of files removed."""
    git_dir = _git_dir(vault)
    removed: list[str] = []
    for name in AUTO_MERGE_ARTIFACTS:
        p = git_dir / name
        if p.exists():
            p.unlink()
            removed.append(name)
    return removed


def main() -> int:
    p = argparse.ArgumentParser(
        prog="vault-desync-recover",
        description="One-shot recovery from the vault desync class (Child #252 of #249).",
    )
    p.add_argument("vault", help="Path to the vault (a git worktree).")
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be restored without making changes.",
    )
    args = p.parse_args()

    vault = Path(args.vault).resolve()
    if not (vault / ".git").exists():
        print(f"[vault-desync-recover] {vault} is not a git worktree", file=sys.stderr)
        return 2

    # Phase 1: probe.
    exit_code, banner = _probe(vault)
    if exit_code == 0:
        print(f"[vault-desync-recover] {vault} is clean — nothing to recover.")
        return 0
    if exit_code == 2:
        print(banner, file=sys.stderr)
        print("[vault-desync-recover] probe could not assess; aborting recovery.", file=sys.stderr)
        return 2

    # Probe fired — collect the recoverable shape.
    deletions = _deletions(vault)
    git_dir = _git_dir(vault)
    artifacts = [name for name in AUTO_MERGE_ARTIFACTS if (git_dir / name).exists()]

    print(banner, file=sys.stderr)
    print(f"\n[vault-desync-recover] Recovery plan for {vault}:")
    print(f"  - restore {len(deletions)} HEAD-tracked files into the working tree")
    if artifacts:
        print(f"  - remove {len(artifacts)} auto-merge artifact(s): {', '.join(artifacts)}")
    print(f"\n[vault-desync-recover] Modifications and untracked files are NOT touched.")

    if args.dry_run:
        print("\n[vault-desync-recover] --dry-run: no changes made.")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("[vault-desync-recover] non-interactive context and --yes not passed; aborting.",
                  file=sys.stderr)
            return 3
        ans = input("\nProceed with recovery? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("[vault-desync-recover] declined.")
            return 3

    # Phase 2: restore deletions.
    if deletions:
        print(f"\n[vault-desync-recover] restoring {len(deletions)} files from HEAD...")
        _restore_from_head(vault, deletions)

    # Phase 3: clear auto-merge artifacts.
    if artifacts:
        removed = _clear_auto_merge(vault)
        print(f"[vault-desync-recover] removed: {', '.join(f'.git/{n}' for n in removed)}")

    # Phase 4: verify probe now clean.
    exit_code_after, banner_after = _probe(vault)
    if exit_code_after == 0:
        print("\n[vault-desync-recover] recovery complete. Final `git status`:")
        st = _git(vault, "status", "--short")
        sys.stdout.write(st.stdout)
        return 0
    else:
        print(banner_after, file=sys.stderr)
        print("\n[vault-desync-recover] recovery did NOT clear the desync. "
              "Inspect signals above; manual investigation needed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
