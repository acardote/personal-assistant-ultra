"""Acceptance tests for the vault pre-commit hook (Child #253 of #249).

The hook is a class-level defense in depth: when commit attempts run on a
desynced vault, the hook runs the probe and refuses. Bypass available via
`PA_VAULT_HOOK_DISABLE=1` for operator override.

Run: `uv run python tests/test_vault_hook_acceptance.py`
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_TEMPLATE = REPO_ROOT / "templates" / "git-hooks" / "pre-commit"
THRESHOLD = 50  # mirrors probe


def _git(cwd: Path, env: dict | None = None, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
        env={**os.environ, **(env or {})},
    )


def _init_repo(path: Path, with_commits: int = 1) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, None, "init", "-q", "-b", "main")
    _git(path, None, "config", "user.email", "test@example.com")
    _git(path, None, "config", "user.name", "test")
    for i in range(with_commits):
        (path / f"file-{i}.txt").write_text(f"content-{i}\n", encoding="utf-8")
        _git(path, None, "add", f"file-{i}.txt")
        _git(path, None, "commit", "-q", "-m", f"commit {i}")


def _install_hook(vault: Path) -> None:
    """Mimics pa-session's install: copy template + chmod +x."""
    dest = vault / ".git" / "hooks" / "pre-commit"
    dest.write_bytes(HOOK_TEMPLATE.read_bytes())
    dest.chmod(0o755)


def _synthesize_desync(vault: Path, *, extra_files: int = THRESHOLD + 10) -> None:
    _git(vault, None, "checkout", "-q", "-b", "feature")
    sub = vault / "bulk"
    sub.mkdir(exist_ok=True)
    for i in range(extra_files):
        (sub / f"f{i}.txt").write_text(f"x{i}\n", encoding="utf-8")
    _git(vault, None, "add", "bulk/")
    _git(vault, None, "commit", "-q", "-m", "bulk feature commit")
    new_head = _git(vault, None, "rev-parse", "HEAD").stdout.strip()
    _git(vault, None, "checkout", "-q", "main")
    (vault / ".git" / "refs" / "heads" / "main").write_text(new_head + "\n", encoding="utf-8")


class VaultHookCases(unittest.TestCase):
    def test_hook_allows_commit_on_clean_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _install_hook(vault)
            # Make a benign change and commit.
            (vault / "new.txt").write_text("hi\n", encoding="utf-8")
            r = _git(vault, {"PA_METHOD_ROOT": str(REPO_ROOT)}, "add", "new.txt", check=False)
            self.assertEqual(r.returncode, 0)
            r = _git(vault, {"PA_METHOD_ROOT": str(REPO_ROOT)}, "commit", "-q", "-m", "benign", check=False)
            self.assertEqual(r.returncode, 0,
                             msg=f"hook should ALLOW commit on clean vault; stderr={r.stderr!r}")

    def test_hook_refuses_commit_on_desynced_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _install_hook(vault)
            _synthesize_desync(vault)
            # Attempt a commit. Without the hook this would succeed and push bad state;
            # with the hook it should refuse.
            (vault / "another.txt").write_text("data\n", encoding="utf-8")
            r = _git(vault, {"PA_METHOD_ROOT": str(REPO_ROOT)}, "add", "another.txt", check=False)
            self.assertEqual(r.returncode, 0)
            r = _git(vault, {"PA_METHOD_ROOT": str(REPO_ROOT)}, "commit", "-q", "-m", "should-fail", check=False)
            self.assertNotEqual(r.returncode, 0,
                                msg=f"hook should REFUSE commit on desynced vault; stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn("desync", r.stderr.lower(), msg=f"expected diagnostic; stderr={r.stderr!r}")

    def test_hook_bypass_via_env(self):
        """PA_VAULT_HOOK_DISABLE=1 must let the operator override (use sparingly)."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _install_hook(vault)
            _synthesize_desync(vault)
            (vault / "bypass.txt").write_text("bypass\n", encoding="utf-8")
            _git(vault, {"PA_METHOD_ROOT": str(REPO_ROOT)}, "add", "bypass.txt")
            r = _git(
                vault,
                {"PA_METHOD_ROOT": str(REPO_ROOT), "PA_VAULT_HOOK_DISABLE": "1"},
                "commit", "-q", "-m", "bypass",
                check=False,
            )
            self.assertEqual(r.returncode, 0,
                             msg=f"bypass env var should allow commit; stderr={r.stderr!r}")

    def test_hook_fires_from_linked_worktree(self):
        """B1 regression (PR #257 review): commits from `.pa-worktrees/<X>/` must
        also be guarded. The hook anchors on `--git-common-dir`, not
        `--show-toplevel`, so it sees the CANONICAL vault's .git/ state and
        runs the probe against the vault path, not the worktree path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _install_hook(vault)

            # Synthesize the desync (touches the canonical vault's state).
            _synthesize_desync(vault)

            # Create a linked worktree on a different branch.
            wt = vault / ".pa-worktrees" / "test-wt"
            wt.parent.mkdir(parents=True, exist_ok=True)
            _git(vault, None, "worktree", "add", "-b", "project/test-wt", str(wt))

            # Attempt a commit FROM THE WORKTREE. The hook should still fire
            # because the vault is desynced even though the worktree itself is fine.
            (wt / "wt-file.txt").write_text("from worktree\n", encoding="utf-8")
            _git(wt, {"PA_METHOD_ROOT": str(REPO_ROOT)}, "add", "wt-file.txt")
            r = _git(wt, {"PA_METHOD_ROOT": str(REPO_ROOT)}, "commit", "-q", "-m", "wt commit", check=False)
            self.assertNotEqual(r.returncode, 0,
                                msg=f"hook should refuse worktree commit when vault is desynced; "
                                    f"stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn("desync", r.stderr.lower())

    def test_hook_fails_open_on_probe_invocation_error(self):
        """B2 regression (PR #257 review): when the probe errors with exit 2
        (not desync), the hook fails OPEN with a warning rather than refusing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _install_hook(vault)

            # Stub the probe to exit 2 ("not a git worktree" or similar invocation error).
            stub_method = Path(tmp) / "stubmethod"
            (stub_method / "tools").mkdir(parents=True)
            stub_probe = stub_method / "tools" / "vault-desync-probe.py"
            stub_probe.write_text(
                "#!/usr/bin/env python3\n"
                "import sys; sys.stderr.write('[stub] simulated invocation error\\n'); sys.exit(2)\n",
                encoding="utf-8",
            )
            stub_probe.chmod(0o755)

            (vault / "open.txt").write_text("data\n", encoding="utf-8")
            env = {"PA_METHOD_ROOT": str(stub_method), "HOME": str(Path(tmp) / "nohome")}
            _git(vault, env, "add", "open.txt")
            r = _git(vault, env, "commit", "-q", "-m", "open", check=False)
            self.assertEqual(r.returncode, 0,
                             msg=f"hook should FAIL OPEN on probe exit 2; stderr={r.stderr!r}")
            self.assertIn("hook fails open", r.stderr.lower())

    def test_hook_disengaged_when_method_root_missing(self):
        """If the method repo can't be located, the hook warns and allows commit
        rather than blocking the operator. The mechanical guard fails open at
        the hook layer (the probe-based gates in pa-session still cover)."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _init_repo(vault, with_commits=1)
            _install_hook(vault)
            (vault / "nomethod.txt").write_text("x\n", encoding="utf-8")
            # Point PA_METHOD_ROOT at a nonexistent path; the hook should still allow.
            env = {"PA_METHOD_ROOT": str(Path(tmp) / "nonexistent"), "HOME": str(Path(tmp) / "alsono")}
            _git(vault, env, "add", "nomethod.txt")
            r = _git(vault, env, "commit", "-q", "-m", "nomethod", check=False)
            self.assertEqual(r.returncode, 0,
                             msg=f"hook should ALLOW when probe is unreachable; stderr={r.stderr!r}")
            self.assertIn("hook disengaged", r.stderr.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
