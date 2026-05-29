"""Acceptance tests for tools/vault-desync-probe.py (Child #251 of #249).

The probe checks two state-based signals (see tool docstring for the
history-based-signal rationale that pr-challenger blocked on review):

  1. `.git/AUTO_MERGE` present AND `.git/MERGE_HEAD` absent  → desync
  2. > MASS_DELETION_THRESHOLD HEAD-tracked files absent from WT  → desync

A legitimate merge in progress (MERGE_HEAD present) is NOT desync, even when
AUTO_MERGE is also present — the probe returns clean in that case.

Each test synthesizes a small git repo in a temp dir and asserts the verdict.

Run: `uv run python tests/test_vault_desync_probe_acceptance.py`
"""

from __future__ import annotations

import json as _json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "vault-desync-probe.py"
THRESHOLD = 50  # mirrors MASS_DELETION_THRESHOLD in the probe


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


def _commit_n_files(path: Path, n: int, prefix: str = "bulk") -> None:
    sub = path / prefix
    sub.mkdir(exist_ok=True)
    for i in range(n):
        (sub / f"f{i}.txt").write_text(f"x{i}\n", encoding="utf-8")
    _git(path, "add", f"{prefix}/")
    _git(path, "commit", "-q", "-m", f"bulk add {n} files")


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
            payload = _json.loads(r.stdout)
            self.assertTrue(payload["clean"])
            self.assertEqual(payload["signals"], [])


class ProbeAutoMergeSignal(unittest.TestCase):
    def test_auto_merge_without_merge_head_fires(self):
        """Post-failed-merge: AUTO_MERGE present, MERGE_HEAD absent → desync."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=2)
            head_tree = _git(vault, "rev-parse", "HEAD^{tree}").stdout.strip()
            (vault / ".git" / "AUTO_MERGE").write_text(head_tree + "\n", encoding="utf-8")
            r = _run_probe(vault)
            self.assertEqual(r.returncode, 1)
            self.assertIn("AUTO_MERGE", r.stderr)
            self.assertIn("DESYNC DETECTED", r.stderr)

    def test_auto_merge_with_merge_head_does_not_fire(self):
        """Mid-conflict-resolution: both AUTO_MERGE and MERGE_HEAD present → NOT desync (pr-challenger B4)."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=2)
            head_sha = _git(vault, "rev-parse", "HEAD").stdout.strip()
            head_tree = _git(vault, "rev-parse", "HEAD^{tree}").stdout.strip()
            # Synthesize both files — emulates a merge git left in conflict state.
            (vault / ".git" / "AUTO_MERGE").write_text(head_tree + "\n", encoding="utf-8")
            (vault / ".git" / "MERGE_HEAD").write_text(head_sha + "\n", encoding="utf-8")
            r = _run_probe(vault)
            self.assertEqual(r.returncode, 0, msg=f"probe should return clean during legit merge; stderr={r.stderr!r}")
            self.assertIn("clean", r.stdout)


class ProbeMassDeletionSignal(unittest.TestCase):
    """The state-based primary signal: WT/HEAD tree mismatch via mass deletions."""

    def test_mass_deletion_via_silent_ref_move_fires(self):
        """May-28 reproduction: ref advanced behind WT, then probe sees mass-delete shape."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)

            # Capture the "old" HEAD (the state WT will end up frozen at).
            old_head = _git(vault, "rev-parse", "HEAD").stdout.strip()

            # Create a feature branch that adds many files.
            _git(vault, "checkout", "-q", "-b", "feature")
            _commit_n_files(vault, THRESHOLD + 10, prefix="bulk")
            new_head = _git(vault, "rev-parse", "HEAD").stdout.strip()

            # Back to main and *rewind* WT/index to the old state, then bypass git to
            # silently advance refs/heads/main to the new commit. The WT now lacks
            # all THRESHOLD+10 bulk/ files but HEAD claims to have them.
            _git(vault, "checkout", "-q", "main")
            (vault / ".git" / "refs" / "heads" / "main").write_text(
                new_head + "\n", encoding="utf-8",
            )

            # Sanity: HEAD resolves to new_head, WT lacks the bulk/ dir.
            self.assertEqual(_git(vault, "rev-parse", "HEAD").stdout.strip(), new_head)
            self.assertFalse((vault / "bulk").exists())

            r = _run_probe(vault)
            self.assertEqual(r.returncode, 1, msg=f"stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn("HEAD-tracked files absent", r.stderr)

    def test_checkout_main_after_silent_move_still_detected(self):
        """pr-challenger B1: an innocuous `git checkout main` after the silent move
        used to clear the reflog-based signal. The state-based signal stays positive.
        """
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _git(vault, "checkout", "-q", "-b", "feature")
            _commit_n_files(vault, THRESHOLD + 10, prefix="bulk")
            new_head = _git(vault, "rev-parse", "HEAD").stdout.strip()
            _git(vault, "checkout", "-q", "main")
            (vault / ".git" / "refs" / "heads" / "main").write_text(
                new_head + "\n", encoding="utf-8",
            )
            # The B1 trigger: a no-op-looking checkout that previously blinded the probe.
            _git(vault, "checkout", "-q", "main", check=False)

            r = _run_probe(vault)
            self.assertEqual(r.returncode, 1, msg="B1 regression: state-based signal must survive `git checkout main`")
            self.assertIn("HEAD-tracked files absent", r.stderr)

    def test_below_threshold_does_not_fire(self):
        """A small batch of deletions (kb-process moving a handful of memos) should NOT trip the gate."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _commit_n_files(vault, 5, prefix="small")
            # Delete a few files (way below threshold).
            for i in range(3):
                (vault / "small" / f"f{i}.txt").unlink()
            r = _run_probe(vault)
            self.assertEqual(r.returncode, 0,
                             msg=f"3 unstaged deletions should be below threshold {THRESHOLD}; stderr={r.stderr!r}")


class ProbeInvocationErrors(unittest.TestCase):
    def test_non_git_path_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_a_repo = Path(tmp) / "not-a-repo"
            not_a_repo.mkdir()
            r = _run_probe(not_a_repo)
            self.assertEqual(r.returncode, 2)
            self.assertIn("not a git worktree", r.stderr)


class ProbeReflogDisabledStillWorks(unittest.TestCase):
    """pr-challenger B3: with `core.logAllRefUpdates=false`, history-based signals
    are structurally absent. The probe must still function via state-based signals.
    """

    def test_no_reflog_still_detects_mass_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _git(vault, "config", "core.logAllRefUpdates", "false")
            # Wipe existing reflogs so the config takes effect from a clean slate.
            for log in (vault / ".git" / "logs").rglob("*"):
                if log.is_file():
                    log.unlink()
            _git(vault, "checkout", "-q", "-b", "feature")
            _commit_n_files(vault, THRESHOLD + 10, prefix="bulk")
            new_head = _git(vault, "rev-parse", "HEAD").stdout.strip()
            _git(vault, "checkout", "-q", "main")
            (vault / ".git" / "refs" / "heads" / "main").write_text(
                new_head + "\n", encoding="utf-8",
            )
            r = _run_probe(vault)
            self.assertEqual(r.returncode, 1,
                             msg="B3 regression: probe must work when reflog is disabled")


class ProbePerformance(unittest.TestCase):
    def test_runs_under_one_second_on_vault_shaped_repo(self):
        """~2.5k tracked files is the canonical vault shape. The strict <200ms target
        in the parent issue is a watch-on-real-vault check; CI machines vary, so we
        assert <1000ms here and let local timing surface regressions.
        """
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _commit_n_files(vault, 2500, prefix="files")
            t0 = time.monotonic()
            r = _run_probe(vault, "--quiet")
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.assertEqual(r.returncode, 0)
            self.assertLess(elapsed_ms, 1000,
                            msg=f"probe took {elapsed_ms:.0f}ms — preflight viability falsifier")


if __name__ == "__main__":
    unittest.main(verbosity=2)
