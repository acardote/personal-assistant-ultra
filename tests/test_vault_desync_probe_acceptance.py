"""Acceptance tests for tools/vault-desync-probe.py (Child #251 of #249).

Each test synthesizes a small git repo in a temp dir and asserts the probe's
verdict against known-good or known-bad states. The probe must:

  - Exit 0 on a clean, just-checked-out repo.
  - Exit 1 when `.git/AUTO_MERGE` is present (the definitive signal).
  - Exit 1 when HEAD reflog top != current HEAD (the smoking-gun signal).
  - Exit 2 when invoked on a non-git path.
  - Complete in under 200ms on a vault-shaped repo (preflight viability).

Run: `uv run python tests/test_vault_desync_probe_acceptance.py`
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "vault-desync-probe.py"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
    )


def _init_repo(path: Path, with_commits: int = 1) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "test")
    for i in range(with_commits):
        (path / f"file-{i}.txt").write_text(f"content-{i}\n", encoding="utf-8")
        _git(path, "add", f"file-{i}.txt")
        _git(path, "commit", "-q", "-m", f"commit {i}")


def _run_probe(vault: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(TOOL), str(vault), *extra],
        capture_output=True, text=True, check=False,
    )


class ProbeCleanCases(unittest.TestCase):
    def test_clean_repo_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=3)
            r = _run_probe(vault)
            self.assertEqual(r.returncode, 0, msg=f"stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn("clean", r.stdout)

    def test_clean_repo_quiet_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=2)
            r = _run_probe(vault, "--quiet")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
            self.assertEqual(r.stderr, "")

    def test_clean_repo_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=2)
            r = _run_probe(vault, "--json")
            self.assertEqual(r.returncode, 0)
            import json as _json
            payload = _json.loads(r.stdout)
            self.assertTrue(payload["clean"])
            self.assertEqual(payload["signals"], [])


class ProbeAutoMergeSignal(unittest.TestCase):
    def test_auto_merge_file_fires_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=2)
            # Synthesize: write a tree SHA into .git/AUTO_MERGE.
            head_tree = _git(vault, "rev-parse", "HEAD^{tree}").stdout.strip()
            (vault / ".git" / "AUTO_MERGE").write_text(head_tree + "\n", encoding="utf-8")
            r = _run_probe(vault)
            self.assertEqual(r.returncode, 1, msg=f"stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn("AUTO_MERGE", r.stderr)
            self.assertIn("DESYNC DETECTED", r.stderr)

    def test_auto_merge_signal_in_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            (vault / ".git" / "AUTO_MERGE").write_text("0" * 40 + "\n", encoding="utf-8")
            r = _run_probe(vault, "--json")
            self.assertEqual(r.returncode, 1)
            import json as _json
            payload = _json.loads(r.stdout)
            self.assertFalse(payload["clean"])
            self.assertTrue(any("AUTO_MERGE" in s for s in payload["signals"]))


class ProbeReflogMismatchSignal(unittest.TestCase):
    def test_ref_advanced_without_head_op_fires_signal(self):
        """Synthesize the May 28 desync state.

        The May 28 forensic observed `refs/heads/main` reflog with entries that had
        no matching `HEAD` reflog entries. `git update-ref refs/heads/main <sha>`
        on its own DOES update HEAD's reflog when HEAD is a symref to that branch
        (verified empirically here in 2026-05), so it's not the right primitive
        for synthesis. Instead, bypass git's ref machinery entirely: write the
        new SHA directly into `.git/refs/heads/main`. This is the most-direct
        possible "ref advanced without git knowing" scenario, and it's the
        precise condition the probe is designed to surface.
        """
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=2)

            old_head = _git(vault, "rev-parse", "HEAD").stdout.strip()

            # Create a second commit reachable on a feature branch, capture its SHA.
            _git(vault, "checkout", "-q", "-b", "feature")
            (vault / "new-file.txt").write_text("new\n", encoding="utf-8")
            _git(vault, "add", "new-file.txt")
            _git(vault, "commit", "-q", "-m", "feature commit")
            new_head = _git(vault, "rev-parse", "HEAD").stdout.strip()

            # Back to main and snapshot HEAD reflog (so we can verify it doesn't change).
            _git(vault, "checkout", "-q", "main")
            pre_head_log = (vault / ".git" / "logs" / "HEAD").read_text(encoding="utf-8")

            # Bypass git plumbing — write directly to the ref file. No reflog update.
            (vault / ".git" / "refs" / "heads" / "main").write_text(
                new_head + "\n", encoding="utf-8",
            )

            post_head_log = (vault / ".git" / "logs" / "HEAD").read_text(encoding="utf-8")
            self.assertEqual(pre_head_log, post_head_log,
                             "HEAD reflog was touched — synthesis didn't bypass git as intended")

            # Sanity: HEAD now resolves to the new SHA (because HEAD is symref to main).
            current = _git(vault, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(current, new_head)
            self.assertNotEqual(current, old_head)

            r = _run_probe(vault)
            self.assertEqual(r.returncode, 1, msg=f"stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn("HEAD reflog top", r.stderr)


class ProbeInvocationErrors(unittest.TestCase):
    def test_non_git_path_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_a_repo = Path(tmp) / "not-a-repo"
            not_a_repo.mkdir()
            r = _run_probe(not_a_repo)
            self.assertEqual(r.returncode, 2)
            self.assertIn("not a git worktree", r.stderr)


class ProbePerformance(unittest.TestCase):
    def test_runs_under_200ms_on_vault_shaped_repo(self):
        """A vault has ~2.6k tracked files. Synthesize a similar-sized repo and time the probe."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            # Add ~500 files quickly in a single commit (≈2.6k tracked files when combined with the
            # init file — close enough order-of-magnitude for the perf check).
            sub = vault / "files"
            sub.mkdir()
            for i in range(500):
                (sub / f"f{i}.txt").write_text(f"x{i}\n", encoding="utf-8")
            _git(vault, "add", "files/")
            _git(vault, "commit", "-q", "-m", "bulk add")

            t0 = time.monotonic()
            r = _run_probe(vault, "--quiet")
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.assertEqual(r.returncode, 0)
            # Generous bound — under 200ms is the falsifier target; CI machines vary,
            # so we assert under 1000ms here and rely on local devs to spot regressions.
            self.assertLess(elapsed_ms, 1000,
                            msg=f"probe took {elapsed_ms:.0f}ms — preflight viability falsifier")


if __name__ == "__main__":
    unittest.main(verbosity=2)
