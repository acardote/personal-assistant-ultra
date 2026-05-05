#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""On-demand / local-fallback harvest wrapper.

Status: this script is no longer the production *primary* scheduled-harvest
path (see #25). The primary path is now a Claude Code routine, documented at
`templates/routines/harvest-routine.md`. This script is still the entry
point for two non-primary uses:

  - **On-demand harvests from a terminal** (e.g. "harvest since lunch")
    that the user wants to run interactively without consuming a routine
    slot. Invoked directly from the shell by the user.
  - **The launchd alternative scheduler** (`templates/launchd/`). The
    plist invokes this script — it is the body of that path, not a
    fallback for it. Available for users on plan tiers without routine
    access or who prefer strictly local execution.

Why a wrapper instead of `claude -p` directly:
  - `claude -p` exits 0 when the session ran, regardless of what the skill
    accomplished inside. Without this wrapper, harvest-failed runs would
    not surface in any user-visible signal — exactly the F1 silent-failure
    the challenger flagged on PR #24.
  - Concurrent fires (scheduled at 7:07am while user runs an on-demand
    harvest at 7:06am) would race on the dedup state files. This wrapper
    holds an fcntl.flock on `.harvest/.lock` so only one runs at a time.
  - F2 cross-machine durability: this wrapper auto-commits + pushes the
    vault after a successful harvest, so machine B sees the new memory
    objects on next pull. Without this, dedup state stays per-machine and
    cross-machine claims break.

Usage:
    tools/scheduled-harvest.py                              # default: harvest since yesterday
    tools/scheduled-harvest.py --prompt "<custom prompt>"   # override harvest prompt
    tools/scheduled-harvest.py --no-commit                  # skip the git commit/push
    tools/scheduled-harvest.py --status-only                # just print latest run status

Wrapper writes a JSON status file at `<content_root>/.harvest/runs/<utc-ts>.json`
on every fire, regardless of success. Stale-run detection: the newest entry
in `runs/` should be < 26h old; if not, the routine is silently broken.

Exits non-zero on:
  - Cannot acquire lock (another run in progress).
  - claude -p exits non-zero.
  - Run status file says ok: false (set by harvest internals — future).
  - git commit/push fails (when --commit, the default).

Defaults to "harvest since yesterday" but the FIRST run on a given content_root
(detected via absence of any prior runs/<...>.json) widens to "harvest the last
30 days" for cold-start backfill. Subsequent runs use the daily window.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402


def utcnow_iso(compact: bool = False) -> str:
    fmt = "%Y-%m-%dT%H%M%SZ" if compact else "%Y-%m-%dT%H:%M:%SZ"
    return _dt.datetime.now(_dt.timezone.utc).strftime(fmt)


def write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@contextmanager
def acquire_lock(lock_path: Path):
    """fcntl.flock-based exclusive lock. Returns the file handle.
    Raises OSError if another process holds the lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise RuntimeError(
            f"could not acquire lock at {lock_path} — another harvest is already running."
        )
    try:
        yield fh
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def git_commit_and_push(content_root: Path, message: str) -> tuple[bool, str]:
    """Best-effort git commit + push from content_root. Returns (ok, detail)."""
    if not (content_root / ".git").is_dir():
        return True, "content_root is not a git repo — skipping commit/push"
    try:
        subprocess.run(["git", "-C", str(content_root), "add", "-A"], check=True, capture_output=True, text=True)
        # If nothing changed, exit cleanly.
        diff = subprocess.run(
            ["git", "-C", str(content_root), "diff", "--cached", "--quiet"],
            capture_output=True, text=True,
        )
        if diff.returncode == 0:
            return True, "no changes to commit"
        subprocess.run(
            ["git", "-C", str(content_root), "commit", "-m", message],
            check=True, capture_output=True, text=True,
        )
        push = subprocess.run(
            ["git", "-C", str(content_root), "push"],
            capture_output=True, text=True,
        )
        if push.returncode != 0:
            return False, f"git push failed: {push.stderr.strip()}"
        return True, "committed and pushed"
    except subprocess.CalledProcessError as e:
        return False, f"git operation failed: {e.stderr.strip() if e.stderr else e}"


def show_latest_status(runs_dir: Path) -> int:
    if not runs_dir.exists():
        print("no runs/ directory yet — routine has never fired.", file=sys.stderr)
        return 1
    files = sorted(runs_dir.glob("*.json"))
    if not files:
        print("no run status files — routine has never fired.", file=sys.stderr)
        return 1
    latest = files[-1]
    payload = json.loads(latest.read_text(encoding="utf-8"))
    print(json.dumps(payload, indent=2))
    return 0 if payload.get("ok", False) else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scheduled harvest wrapper for the personal-assistant skill.")
    parser.add_argument("--prompt", help="Override the harvest prompt sent to `claude -p`.")
    parser.add_argument("--no-commit", action="store_true", help="Skip the git commit/push step.")
    parser.add_argument("--status-only", action="store_true", help="Print the latest run status JSON and exit.")
    parser.add_argument("--cold-start-days", type=int, default=30, help="Backfill window on first run (default: 30 days).")
    args = parser.parse_args(argv[1:])

    cfg = load_config(require_explicit_content_root=True)
    method_root = cfg.method_root
    content_root = cfg.content_root
    harvest_dir = content_root / ".harvest"
    runs_dir = harvest_dir / "runs"
    lock_path = harvest_dir / ".lock"

    if args.status_only:
        return show_latest_status(runs_dir)

    ts_compact = utcnow_iso(compact=True)
    status_path = runs_dir / f"{ts_compact}.json"
    status: dict = {
        "started_at": utcnow_iso(),
        "method_root": str(method_root),
        "content_root": str(content_root),
        "ok": False,
        "scheduler": "launchd",
        "phase": "init",
        "error": None,
    }
    write_status(status_path, status)

    # Cold-start detection: status_path was already written above (so runs/
    # has ≥1 file), filter to OTHER files to see if any prior run exists.
    other_files = [p for p in runs_dir.glob("*.json") if p != status_path]
    cold_start = not other_files

    if args.prompt:
        prompt = args.prompt
    elif cold_start:
        prompt = f"Run /personal-assistant harvest --since {args.cold_start_days}d (cold start backfill)"
    else:
        prompt = "Run /personal-assistant harvest --since yesterday"
    status["prompt"] = prompt
    status["cold_start"] = cold_start
    write_status(status_path, status)

    try:
        with acquire_lock(lock_path):
            status["phase"] = "running claude -p"
            write_status(status_path, status)

            # Run claude -p from method-root cwd so relative paths resolve.
            proc = subprocess.run(
                ["claude", "-p", prompt],
                cwd=str(method_root),
                capture_output=True, text=True,
            )
            status["claude_returncode"] = proc.returncode
            status["claude_stdout_tail"] = "\n".join(proc.stdout.splitlines()[-50:])
            status["claude_stderr_tail"] = "\n".join(proc.stderr.splitlines()[-50:])

            if proc.returncode != 0:
                status["phase"] = "claude_failed"
                status["error"] = f"claude -p exited {proc.returncode}"
                status["ended_at"] = utcnow_iso()
                write_status(status_path, status)
                print(f"[scheduled-harvest] claude -p failed (rc={proc.returncode}); see {status_path}", file=sys.stderr)
                return proc.returncode

            if not args.no_commit:
                status["phase"] = "git_commit_push"
                write_status(status_path, status)
                ok, detail = git_commit_and_push(
                    content_root,
                    message=f"harvest {status['started_at']}\n\nautomated by tools/scheduled-harvest.py",
                )
                status["git"] = {"ok": ok, "detail": detail}
                if not ok:
                    status["phase"] = "git_failed"
                    status["error"] = detail
                    status["ended_at"] = utcnow_iso()
                    write_status(status_path, status)
                    print(f"[scheduled-harvest] git failed: {detail}", file=sys.stderr)
                    return 1

            status["phase"] = "ok"
            status["ok"] = True
            status["ended_at"] = utcnow_iso()
            write_status(status_path, status)
            print(f"[scheduled-harvest] ok; status at {status_path}", file=sys.stderr)
            return 0
    except RuntimeError as exc:
        status["phase"] = "lock_failed"
        status["error"] = str(exc)
        status["ended_at"] = utcnow_iso()
        write_status(status_path, status)
        print(f"[scheduled-harvest] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        status["phase"] = "unhandled_exception"
        status["error"] = repr(exc)
        status["ended_at"] = utcnow_iso()
        write_status(status_path, status)
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv))
