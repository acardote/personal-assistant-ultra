"""Acceptance tests for tools/vault-desync-recover.py (Child #252 of #249).

End-to-end: synthesize the May-28 desync shape in a temp repo, run recovery,
assert the probe is clean afterwards AND that user-uncommitted edits survive.

Run: `uv run python tests/test_vault_desync_recover_acceptance.py`
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROBE = Path(__file__).resolve().parent.parent / "tools" / "vault-desync-probe.py"
RECOVER = Path(__file__).resolve().parent.parent / "tools" / "vault-desync-recover.py"
THRESHOLD = 50  # mirrors probe


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


def _synthesize_desync(vault: Path, *, extra_files: int = THRESHOLD + 10) -> None:
    """Build a May-28-shape desync in `vault`: WT frozen at an older commit while
    refs/heads/main points to a newer one (containing `extra_files` more files)."""
    _git(vault, "checkout", "-q", "-b", "feature")
    sub = vault / "bulk"
    sub.mkdir(exist_ok=True)
    for i in range(extra_files):
        (sub / f"f{i}.txt").write_text(f"x{i}\n", encoding="utf-8")
    _git(vault, "add", "bulk/")
    _git(vault, "commit", "-q", "-m", "bulk feature commit")
    new_head = _git(vault, "rev-parse", "HEAD").stdout.strip()
    _git(vault, "checkout", "-q", "main")
    # Bypass plumbing: direct ref-file write — the exact synthesis the probe tests use.
    (vault / ".git" / "refs" / "heads" / "main").write_text(new_head + "\n", encoding="utf-8")
    # Also synthesize an AUTO_MERGE artifact (the failed-merge fingerprint).
    head_tree = _git(vault, "rev-parse", "HEAD^{tree}").stdout.strip()
    (vault / ".git" / "AUTO_MERGE").write_text(head_tree + "\n", encoding="utf-8")


def _run_recover(vault: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(RECOVER), str(vault), *extra],
        capture_output=True, text=True, check=False,
    )


def _run_probe(vault: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PROBE), str(vault), *extra],
        capture_output=True, text=True, check=False,
    )


class RecoverCleanCases(unittest.TestCase):
    def test_clean_vault_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=2)
            r = _run_recover(vault, "--yes")
            self.assertEqual(r.returncode, 0)
            self.assertIn("nothing to recover", r.stdout)

    def test_clean_vault_dry_run_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            r = _run_recover(vault, "--dry-run", "--yes")
            self.assertEqual(r.returncode, 0)


class RecoverDesyncReproduction(unittest.TestCase):
    def test_may28_shape_recovers_to_clean(self):
        """Synthesize the May-28 shape (silent ref move + AUTO_MERGE), recover,
        assert probe is clean afterwards."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _synthesize_desync(vault)

            # Pre-check: probe should currently fire.
            self.assertEqual(_run_probe(vault, "--quiet").returncode, 1)

            r = _run_recover(vault, "--yes")
            self.assertEqual(r.returncode, 0, msg=f"recover stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn("recovery complete", r.stdout)

            # Post-check: probe must now be clean.
            self.assertEqual(_run_probe(vault, "--quiet").returncode, 0,
                             msg="probe still fires after recovery — desync wasn't cleared")

            # Verify the previously-missing files are now on disk.
            self.assertTrue((vault / "bulk" / "f0.txt").exists())
            self.assertTrue((vault / "bulk" / f"f{THRESHOLD}.txt").exists())

            # Verify .git/AUTO_MERGE was removed.
            self.assertFalse((vault / ".git" / "AUTO_MERGE").exists())

    def test_recover_preserves_user_modifications(self):
        """User-edited files (M / M-staged) must survive recovery, even when the
        desync fires alongside them. This is the load-bearing safety property
        called out in #252's falsifier.

        Synthesis order matters: desync first, THEN the user edit on top, so the
        user edit is genuinely unrelated to the silent-ref-move state.
        """
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=2)
            _synthesize_desync(vault)

            # NOW the user edits a tracked file (file-1.txt exists in HEAD and WT
            # with the same "content-1\n"; the user's change is the only diff for
            # that path).
            (vault / "file-1.txt").write_text("USER-EDIT-PRESERVED\n", encoding="utf-8")
            _git(vault, "add", "file-1.txt")

            # Confirm probe fires from the desync (not from the user edit).
            self.assertEqual(_run_probe(vault, "--quiet").returncode, 1)

            r = _run_recover(vault, "--yes")
            self.assertEqual(r.returncode, 0, msg=f"stderr={r.stderr!r}")

            # User edit survived?
            content = (vault / "file-1.txt").read_text(encoding="utf-8")
            self.assertEqual(content, "USER-EDIT-PRESERVED\n",
                             "user-staged modification was reverted by recovery — falsifier hit")

    def test_recover_preserves_untracked_outputs(self):
        """Untracked files (kb-process outputs, weekly updates, etc.) must survive."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            (vault / "untracked-thing.txt").write_text("survives\n", encoding="utf-8")
            _synthesize_desync(vault)
            r = _run_recover(vault, "--yes")
            self.assertEqual(r.returncode, 0)
            self.assertTrue((vault / "untracked-thing.txt").exists())
            self.assertEqual((vault / "untracked-thing.txt").read_text(encoding="utf-8"), "survives\n")


class RecoverNonInteractive(unittest.TestCase):
    def test_no_tty_no_yes_aborts(self):
        """In a script context without --yes, the recovery should refuse rather than
        silently apply destructive changes."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _synthesize_desync(vault)
            # subprocess.run defaults to non-tty stdin (captures it), so this exercises
            # the "non-interactive context, no --yes" branch.
            r = _run_recover(vault)  # no --yes
            self.assertEqual(r.returncode, 3,
                             msg=f"expected exit 3 (declined); got {r.returncode}, stderr={r.stderr!r}")


class RecoverPerformance(unittest.TestCase):
    def test_recover_completes_within_bound_on_vault_shape(self):
        """May-28 had 238 deletions to restore; the recovery must complete in a
        reasonable wall-clock so operators don't bail mid-recovery."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _synthesize_desync(vault, extra_files=250)
            t0 = time.monotonic()
            r = _run_recover(vault, "--yes")
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.assertEqual(r.returncode, 0)
            # 5 seconds is generous for ~250 git-checkout invocations chunked at 100.
            self.assertLess(elapsed_ms, 5000,
                            msg=f"recovery took {elapsed_ms:.0f}ms — falsifier bound")


if __name__ == "__main__":
    unittest.main(verbosity=2)
