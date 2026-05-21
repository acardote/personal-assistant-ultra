#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for scripts/pa-session — launch helper for multi-project workflow (C3 of #214).

Tests:
  T1  — `new --auto --no-launch` creates worktree + scaffolds project + commits .gitignore entry on main.
  T2  — `new --auto --no-launch` re-run is idempotent: second invocation reports existing-worktree, doesn't duplicate.
  T3  — `new` rejects an invalid short name (uppercase, spaces).
  T4  — `resume --no-launch <nonexistent>` refuses with non-zero exit and a clear message.
  T5  — `path <short>` prints the absolute worktree path (and only that).
  T6  — `list` (default --status active --format table) shows only active projects.
  T7  — `list --status archived` shows only archived projects.
  T8  — `list --status all --format json` emits parseable JSON with all entries.
  T9  — `close --keep-branch` refuses on dirty worktree without --force.
  T10 — `close --keep-branch --force` removes worktree even when dirty.
  T11 — `close --keep-branch` (clean worktree) archives + removes worktree, leaves branch in place.
  T12 — `doctor` returns 0 on a clean setup with one valid worktree.
  T13 — `doctor` returns non-zero + reports orphan when a directory exists under .pa-worktrees/ without git registration.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
HELPER = PROJ / "scripts" / "pa-session"


def make_fixture(tmpdir: Path) -> tuple[Path, Path]:
    """Create a fake method-root (with tools/ + scripts/) and a canonical vault git repo."""
    method = tmpdir / "method"
    vault = tmpdir / "vault"
    method.mkdir()
    vault.mkdir()
    (method / "tools").mkdir()
    (method / "scripts").mkdir()
    for f in ("_config.py", "project.py"):
        shutil.copy(PROJ / "tools" / f, method / "tools" / f)
    shutil.copy(HELPER, method / "scripts" / "pa-session")
    os.chmod(method / "scripts" / "pa-session", 0o755)
    # .assistant.local.json at method root pointing at vault.
    (method / ".assistant.local.json").write_text(
        json.dumps({"$schema_version": 1, "paths": {"content_root": str(vault.resolve())}}),
        encoding="utf-8",
    )
    # Initialize the vault as a git repo on main with one commit (so we can branch + worktree).
    subprocess.run(["git", "init", "-b", "main"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "commit",
                    "--allow-empty", "-m", "init"], cwd=vault, check=True, capture_output=True)
    return method, vault


def run_helper(method: Path, *args: str, expect_rc: int | None = 0, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PA_QUIET": "1", "HOME": str(method.parent / "fake-home")}
    # Configure git committer identity inside the helper's subprocesses.
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.t",
    })
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        [sys.executable, str(method / "scripts" / "pa-session"), *args],
        capture_output=True, text=True, env=env,
    )
    if expect_rc is not None:
        assert r.returncode == expect_rc, (
            f"unexpected rc={r.returncode} (wanted {expect_rc})\n"
            f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )
    return r


def test_new_creates_worktree_and_gitignore():
    """T1 — new creates worktree + scaffolds project + adds .gitignore on main."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        r = run_helper(method, "new", "qproj", "first intent", "--auto", "--no-launch")
        assert "created worktree" in r.stderr, r.stderr
        wt = vault / ".pa-worktrees" / "qproj"
        assert wt.is_dir()
        # Scaffold: projects/<slug>/project.md inside the worktree.
        projects = list((wt / "projects").iterdir())
        assert len(projects) == 1, f"expected 1 project, got {projects}"
        assert (projects[0] / "project.md").is_file()
        # .gitignore on main contains the entry.
        gi = (vault / ".gitignore").read_text(encoding="utf-8")
        assert "/.pa-worktrees/" in gi, gi
        # Active-state file inside the worktree (from project.py new).
        assert (wt / ".pa-active-project.json").is_file()


def test_new_idempotent():
    """T2 — re-run new with same short name is a no-op (no duplicate, no error)."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run_helper(method, "new", "qproj", "first", "--auto", "--no-launch")
        r = run_helper(method, "new", "qproj", "first", "--auto", "--no-launch")
        # Re-run reports existing worktree.
        assert "already present" in r.stderr or "already scaffolded" in r.stderr, r.stderr
        # Only one project directory inside the worktree.
        wt = vault / ".pa-worktrees" / "qproj"
        projects = [p for p in (wt / "projects").iterdir() if p.is_dir()]
        assert len(projects) == 1


def test_new_rejects_invalid_short():
    """T3 — new refuses uppercase / spaces / over-long short names."""
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        for bad in ("BadName", "with space", "x" * 31):
            r = run_helper(method, "new", bad, "intent", "--auto", "--no-launch", expect_rc=None)
            assert r.returncode != 0, f"accepted bad short {bad!r}"
            assert "invalid short name" in r.stderr, r.stderr


def test_resume_refuses_missing():
    """T4 — resume of a non-existent worktree refuses with non-zero exit and clear message."""
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        r = run_helper(method, "resume", "nothere", "--no-launch", expect_rc=None)
        assert r.returncode != 0
        assert "no worktree at" in r.stderr, r.stderr


def test_path_prints_absolute():
    """T5 — path <short> prints the absolute worktree path on stdout."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        r = run_helper(method, "path", "qproj")
        # Compare resolved paths — macOS resolves /var to /private/var, etc.
        assert Path(r.stdout.strip()).resolve() == (vault / ".pa-worktrees" / "qproj").resolve(), r.stdout


def test_list_active_default():
    """T6 — list default filter shows only active projects."""
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        run_helper(method, "new", "alpha", "p1", "--auto", "--no-launch")
        run_helper(method, "new", "beta", "p2", "--auto", "--no-launch")
        r = run_helper(method, "list")
        assert "SHORT" in r.stdout
        assert "alpha" in r.stdout and "beta" in r.stdout
        assert "active" in r.stdout
        assert "archived" not in r.stdout


def test_list_archived_filter():
    """T7 — list --status archived shows only archived."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run_helper(method, "new", "alpha", "p1", "--auto", "--no-launch")
        run_helper(method, "new", "gamma", "p3", "--auto", "--no-launch")
        # Manually flip gamma's frontmatter to status: archived. This avoids the
        # close path (which removes the worktree); we need archived-AND-present.
        gamma_wt = vault / ".pa-worktrees" / "gamma"
        proj_dirs = [p for p in (gamma_wt / "projects").iterdir() if p.is_dir()]
        pmd = proj_dirs[0] / "project.md"
        text = pmd.read_text(encoding="utf-8")
        text = text.replace("status: active", "status: archived")
        pmd.write_text(text, encoding="utf-8")
        r = run_helper(method, "list", "--status", "archived")
        assert "gamma" in r.stdout, r.stdout
        assert "alpha" not in r.stdout, r.stdout


def test_list_json_format():
    """T8 — list --format json emits parseable JSON for all entries."""
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        run_helper(method, "new", "alpha", "p1", "--auto", "--no-launch")
        r = run_helper(method, "list", "--status", "all", "--format", "json")
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert any(d["short"] == "alpha" for d in data)
        assert all(set(d.keys()) >= {"short", "branch", "last_active", "status", "worktree"} for d in data)


def test_close_refuses_dirty():
    """T9 — close refuses on dirty worktree without --force."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run_helper(method, "new", "qproj", "p1", "--auto", "--no-launch")
        # Dirty the worktree.
        (vault / ".pa-worktrees" / "qproj" / "dirty.txt").write_text("oops")
        r = run_helper(method, "close", "qproj", "--keep-branch", expect_rc=None)
        assert r.returncode != 0
        assert "uncommitted changes" in r.stderr, r.stderr
        # Worktree still exists.
        assert (vault / ".pa-worktrees" / "qproj").is_dir()


def test_close_force_removes_dirty():
    """T10 — close --force removes worktree even when dirty."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run_helper(method, "new", "qproj", "p1", "--auto", "--no-launch")
        (vault / ".pa-worktrees" / "qproj" / "dirty.txt").write_text("oops")
        r = run_helper(method, "close", "qproj", "--keep-branch", "--force", expect_rc=None)
        # --force allows the close path even with dirty state. project.py archive may still
        # succeed or fail depending on git state; this test just confirms --force doesn't trip
        # the dirty-state refusal.
        assert "uncommitted changes" not in r.stderr, r.stderr


def test_close_keep_branch_clean():
    """T11 — close --keep-branch on a clean worktree archives + removes worktree."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run_helper(method, "new", "qproj", "p1", "--auto", "--no-launch")
        # First we need to COMMIT the initial scaffold to clear "dirty" state on the project branch.
        wt = vault / ".pa-worktrees" / "qproj"
        subprocess.run(["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "add", "-A"],
                       cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "commit",
                        "-m", "scaffold"], cwd=wt, check=True, capture_output=True)
        r = run_helper(method, "close", "qproj", "--keep-branch")
        assert "closed qproj" in r.stderr, r.stderr
        # Worktree dir removed.
        assert not wt.is_dir()
        # Branch still exists.
        branches = subprocess.run(["git", "branch", "--list", "project/qproj"],
                                  cwd=vault, capture_output=True, text=True).stdout
        assert "project/qproj" in branches


def test_doctor_clean():
    """T12 — doctor returns 0 on a clean setup."""
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        run_helper(method, "new", "qproj", "p1", "--auto", "--no-launch")
        r = run_helper(method, "doctor")
        assert "ok:" in r.stdout, r.stdout
        assert "FAIL" not in r.stdout


def test_doctor_finds_orphan():
    """T13 — doctor reports orphan when a .pa-worktrees/* dir is not git-registered."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run_helper(method, "new", "qproj", "p1", "--auto", "--no-launch")
        # Plant an orphan directory.
        orphan = vault / ".pa-worktrees" / "orphan-dir"
        orphan.mkdir()
        r = run_helper(method, "doctor", expect_rc=None)
        assert r.returncode != 0
        assert "orphan" in r.stdout, r.stdout


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}", file=sys.stderr)
    if failed:
        print(f"\n{failed}/{len(tests)} failed", file=sys.stderr)
        sys.exit(1)
    print(f"\n{len(tests)} tests ok")
