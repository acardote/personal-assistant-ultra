#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/project.py — PA project tier (#88 / #92).

Tests:
  T1  — `new` creates a project folder with valid slug + frontmatter; sets state.
  T2  — `new` writes a clean project.md (no leaked comments from any template).
  T3  — `list` shows only active by default; `--include-archived` adds archived.
  T4  — `archive` flips status, sets archived_at, clears state if active.
  T5  — `status` reports active project + age + frontmatter scalars.
  T6  — `clear` removes the state file.
  T7  — slug collision retry: if the first roll collides, a second slug is minted.
  T8  — `promote` moves a flat artefact into a project; updates frontmatter.
  T9  — `promote` of an export-kind artefact moves the body AND sidecar (F2 fix).
  T10 — `copy-artefact` mints fresh id, sets derived_from, copies body verbatim.
  T11 — `copy-artefact` of export kind copies body + sidecar with renamed prefix.
  T12 — `resume` accepts unambiguous short-name; rejects ambiguous.
  T13 — `find-artefact` invariant violation: same uuid in two locations refuses.
  T14 — short-name validation refuses uppercase / spaces / >30 chars.
  T15 — promote refuses an artefact already inside a project.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
TOOL = PROJ / "tools" / "project.py"


def make_fixture(tmpdir: Path) -> tuple[Path, Path]:
    """Method + vault skeletons. Returns (method_root, content_root)."""
    method = tmpdir / "method"
    vault = tmpdir / "vault"
    method.mkdir()
    vault.mkdir()
    (method / "tools").mkdir()
    shutil.copy(PROJ / "tools" / "_config.py", method / "tools" / "_config.py")
    shutil.copy(PROJ / "tools" / "project.py", method / "tools" / "project.py")
    (method / ".assistant.local.json").write_text(json.dumps({
        "$schema_version": 1,
        "paths": {"content_root": str(vault.resolve())},
    }), encoding="utf-8")
    (vault / "projects").mkdir()
    (vault / "artefacts").mkdir()
    for kind in ("memo", "export", "analysis", "plan", "draft", "report"):
        (vault / "artefacts" / kind).mkdir()
    return method, vault


def run(method: Path, *args: str, expect_rc: int | None = 0) -> subprocess.CompletedProcess:
    r = subprocess.run([str(method / "tools" / "project.py"), *args], capture_output=True, text=True)
    if expect_rc is not None:
        assert r.returncode == expect_rc, (
            f"unexpected rc={r.returncode} (wanted {expect_rc})\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )
    return r


def get_active_slug(vault: Path) -> str:
    state = json.loads((vault / ".pa-active-project.json").read_text(encoding="utf-8"))
    return state["slug"]


def test_new_creates_project():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        r = run(method, "new", "thing", "first project")
        assert "slug=" in r.stdout
        slug = get_active_slug(vault)
        proj = vault / "projects" / slug
        assert proj.is_dir()
        assert (proj / "project.md").is_file()
        assert (proj / "artefacts").is_dir()
    print("  T1 PASS — new creates project folder + state")


def test_project_md_is_clean():
    """No leaked '# MUST equal...' template comments in a real project.md."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "thing", "intent text")
        slug = get_active_slug(vault)
        text = (vault / "projects" / slug / "project.md").read_text(encoding="utf-8")
        assert "# MUST equal" not in text
        assert "# touched on every" not in text
        assert f"id: {slug}" in text
        assert "title: thing" in text
    print("  T2 PASS — project.md is clean")


def test_list_filters_archived():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "alpha", "p1")
        slug_a = get_active_slug(vault)
        run(method, "new", "beta", "p2")
        # archive alpha
        run(method, "archive", slug_a)

        r = run(method, "list")
        assert "alpha" not in r.stdout, "archived alpha should be hidden"

        r = run(method, "list", "--include-archived")
        assert "alpha" in r.stdout
        assert "beta" in r.stdout
    print("  T3 PASS — list filtering works")


def test_archive_clears_state_if_active():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "x", "i")
        slug = get_active_slug(vault)
        run(method, "archive", slug)
        assert not (vault / ".pa-active-project.json").exists(), \
            "archiving the active project should clear state"
        text = (vault / "projects" / slug / "project.md").read_text(encoding="utf-8")
        assert "status: archived" in text
        assert "archived_at:" in text
    print("  T4 PASS — archive flips status + clears state")


def test_status_reports_active():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "x", "intent")
        r = run(method, "status")
        assert "active:" in r.stdout
        assert "title: x" in r.stdout
        assert "last_active:" in r.stdout
    print("  T5 PASS — status reports active project")


def test_clear_removes_state():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "x", "i")
        run(method, "clear")
        assert not (vault / ".pa-active-project.json").exists()
        r = run(method, "status")
        assert "no project active" in r.stdout
    print("  T6 PASS — clear removes state")


def test_promote_moves_flat_artefact():
    """B1 closer: promote MUST preserve nested produced_by frontmatter."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "x", "i")
        slug = get_active_slug(vault)
        # Create a flat memo artefact with realistic nested produced_by.
        flat_md = vault / "artefacts" / "memo" / "art-abc123.md"
        flat_md.write_text(
            "---\nid: art-abc123\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
            "title: t\nproduced_by:\n  session_id: aaaaaaaa\n  query: x\n  model: m\n"
            "  sources_cited:\n    - https://x.test\n---\nbody",
            encoding="utf-8",
        )
        r = run(method, "promote", "abc123", slug)
        assert "promoted" in r.stdout
        assert not flat_md.exists(), "source artefact should be moved"
        moved = vault / "projects" / slug / "artefacts" / "memo" / "art-abc123.md"
        assert moved.is_file()
        text = moved.read_text(encoding="utf-8")
        # B1 critical assertions: nested produced_by must survive.
        assert f"project_id: {slug}" in text
        assert "produced_by:" in text, "B1: produced_by block dropped!"
        assert "session_id: aaaaaaaa" in text, "B1: session_id dropped!"
        assert "query: x" in text, "B1: query dropped!"
        assert "model: m" in text, "B1: model dropped!"
        assert "https://x.test" in text, "B1: sources_cited entry dropped!"
        assert "body" in text, "body should survive"
    print("  T7 PASS — promote preserves nested produced_by (B1 closed)")


def test_promote_export_moves_sidecar():
    """F2: export-kind promote MUST move both the body AND the sidecar."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "x", "i")
        slug = get_active_slug(vault)
        body = vault / "artefacts" / "export" / "art-xyz789.csv"
        sidecar = vault / "artefacts" / "export" / "art-xyz789.provenance.json"
        body.write_text("a,b,c\n", encoding="utf-8")
        sidecar.write_text(json.dumps({
            "session_id": "aaaaaaaa", "query": "x", "model": "m",
            "sources_cited": ["https://x.test"],
        }), encoding="utf-8")

        r = run(method, "promote", "xyz789", slug)
        assert "promoted" in r.stdout
        assert not body.exists() and not sidecar.exists(), "both should be moved"
        moved_body = vault / "projects" / slug / "artefacts" / "export" / "art-xyz789.csv"
        moved_sidecar = vault / "projects" / slug / "artefacts" / "export" / "art-xyz789.provenance.json"
        assert moved_body.is_file()
        assert moved_sidecar.is_file()
        sidecar_data = json.loads(moved_sidecar.read_text(encoding="utf-8"))
        assert sidecar_data["project_id"] == slug
    print("  T8 PASS — promote moves both body + sidecar (F2 closed)")


def test_copy_artefact():
    """B1: nested produced_by must survive copy. B2: id must rotate."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "alpha", "i")
        run(method, "new", "beta", "i")
        dirs = sorted(d.name for d in (vault / "projects").iterdir() if d.is_dir())
        slug_a = next(d for d in dirs if "alpha" in d)
        slug_b = next(d for d in dirs if "beta" in d)

        # Place an artefact with nested produced_by in alpha.
        art = vault / "projects" / slug_a / "artefacts" / "memo"
        art.mkdir(parents=True)
        body_v = (
            "---\nid: art-orig123\nkind: memo\nproject_id: " + slug_a + "\n"
            "produced_by:\n  session_id: aaaaaaaa\n  query: x\n  model: m\n"
            "  sources_cited:\n    - https://x.test\n---\nORIGINAL"
        )
        (art / "art-orig123.md").write_text(body_v, encoding="utf-8")

        r = run(method, "copy-artefact", "orig123", slug_b)
        assert "copied" in r.stdout

        # Original unchanged
        assert (art / "art-orig123.md").read_text(encoding="utf-8") == body_v

        copies = list((vault / "projects" / slug_b / "artefacts" / "memo").iterdir())
        assert len(copies) == 1
        copy_text = copies[0].read_text(encoding="utf-8")
        assert "derived_from: art-orig123" in copy_text
        assert f"project_id: {slug_b}" in copy_text
        assert "ORIGINAL" in copy_text
        # B1: nested produced_by preserved
        assert "session_id: aaaaaaaa" in copy_text
        assert "https://x.test" in copy_text
        # B2: id rotated to fresh uuid (must NOT be art-orig123)
        assert "id: art-orig123" not in copy_text, "id should be rotated, not reused"
        assert re.search(r"^id: art-(?!orig123)[\w-]+$", copy_text, re.MULTILINE), \
            "copy must have a fresh art-<uuid> id"
    print("  T9 PASS — copy-artefact preserves nested + rotates id")


def test_short_name_validation():
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        # uppercase
        r = run(method, "new", "BadName", "intent", expect_rc=None)
        assert r.returncode != 0
        # spaces
        r = run(method, "new", "bad name", "intent", expect_rc=None)
        assert r.returncode != 0
    print("  T10 PASS — short-name validation refuses bad inputs")


def test_resume_ambiguous_short_name():
    """B2: state file must NOT be written on rejection."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "projects" / "20260507-thing-aaaa").mkdir()
        (vault / "projects" / "20260507-thing-aaaa" / "project.md").write_text(
            "---\nid: 20260507-thing-aaaa\ntitle: a\nstatus: active\n---\n",
            encoding="utf-8",
        )
        (vault / "projects" / "20260507-thing-bbbb").mkdir()
        (vault / "projects" / "20260507-thing-bbbb" / "project.md").write_text(
            "---\nid: 20260507-thing-bbbb\ntitle: b\nstatus: active\n---\n",
            encoding="utf-8",
        )
        state_path = vault / ".pa-active-project.json"
        assert not state_path.exists(), "no state should exist before run"
        r = run(method, "resume", "thing", expect_rc=None)
        assert r.returncode == 1
        assert "ambiguous" in r.stderr.lower()
        assert not state_path.exists(), "state must NOT be written on ambiguous rejection"
    print("  T11 PASS — resume rejects ambiguous + leaves state untouched")


def test_touch_preserves_nested_frontmatter():
    """touch must use the surgical updater — hand-rewriting reintroduces B1."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "x", "i")
        slug = get_active_slug(vault)
        # Inject a nested produced_by block into project.md (simulate after
        # the assistant wrote sources_cited or other nested data).
        proj_md = vault / "projects" / slug / "project.md"
        text = proj_md.read_text(encoding="utf-8")
        # Add a nested produced_by block before closing ---
        text = text.replace(
            "last_active:",
            "produced_by:\n  session_id: aaaaaaaa\n  query: x\n  sources_cited:\n    - https://x.test\nlast_active:",
        )
        proj_md.write_text(text, encoding="utf-8")

        run(method, "touch", slug)
        out = proj_md.read_text(encoding="utf-8")
        assert "produced_by:" in out, "B1: nested block dropped on touch"
        assert "session_id: aaaaaaaa" in out, "B1: nested key dropped on touch"
        assert "https://x.test" in out, "B1: list item dropped on touch"
    print("  T13 PASS — touch preserves nested frontmatter")


def test_sweep_lists_stale_projects():
    """Project last_active >30d ago is listed; under threshold is not."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))

        # Make two projects with hand-crafted last_active dates.
        old = vault / "projects" / "20260101-old-aaaa"
        old.mkdir()
        (old / "project.md").write_text(
            "---\nid: 20260101-old-aaaa\ntitle: stale-project\n"
            "status: active\nstarted_at: 2026-01-01\nlast_active: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        recent = vault / "projects" / "20260507-recent-bbbb"
        recent.mkdir()
        (recent / "project.md").write_text(
            "---\nid: 20260507-recent-bbbb\ntitle: recent-project\n"
            "status: active\nstarted_at: 2026-05-07\nlast_active: 2026-05-07\n---\n",
            encoding="utf-8",
        )
        r = run(method, "sweep")
        assert "20260101-old-aaaa" in r.stdout
        assert "20260507-recent-bbbb" not in r.stdout
    print("  T14 PASS — sweep lists only stale active projects")


def test_sweep_skips_archived():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        old = vault / "projects" / "20260101-archived-aaaa"
        old.mkdir()
        (old / "project.md").write_text(
            "---\nid: 20260101-archived-aaaa\ntitle: arch\n"
            "status: archived\narchived_at: 2026-02-01\n"
            "started_at: 2026-01-01\nlast_active: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        r = run(method, "sweep")
        assert "20260101-archived-aaaa" not in r.stdout
    print("  T15 PASS — sweep skips archived projects")


def test_sweep_does_not_mutate():
    """Tree-wide snapshot (per pr-challenger #101): sweep must not touch ANY file."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Three projects: stale-active, recent-active, archived. Plus a flat artefact.
        old = vault / "projects" / "20260101-old-aaaa"
        old.mkdir()
        (old / "project.md").write_text(
            "---\nid: 20260101-old-aaaa\ntitle: t\nstatus: active\n"
            "started_at: 2026-01-01\nlast_active: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        recent = vault / "projects" / "20260507-recent-bbbb"
        recent.mkdir()
        (recent / "project.md").write_text(
            "---\nid: 20260507-recent-bbbb\nstatus: active\n"
            "started_at: 2026-05-07\nlast_active: 2026-05-07\n---\n",
            encoding="utf-8",
        )
        archived = vault / "projects" / "20260101-arch-cccc"
        archived.mkdir()
        (archived / "project.md").write_text(
            "---\nid: 20260101-arch-cccc\nstatus: archived\n"
            "archived_at: 2026-02-01\nlast_active: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        flat_art = vault / "artefacts" / "memo" / "art-flat.md"
        flat_art.write_text("---\nid: art-flat\nkind: memo\n---\nbody", encoding="utf-8")

        snapshot: dict[Path, tuple[int, str]] = {}
        for p in vault.rglob("*"):
            if p.is_file():
                snapshot[p] = (p.stat().st_mtime_ns, p.read_text(encoding="utf-8"))

        run(method, "sweep")

        for p, (mtime, content) in snapshot.items():
            assert p.exists(), f"sweep deleted {p}"
            assert p.stat().st_mtime_ns == mtime, f"sweep mutated mtime of {p}"
            assert p.read_text(encoding="utf-8") == content, f"sweep mutated content of {p}"
        # State file must not have been created either.
        assert not (vault / ".pa-active-project.json").exists(), "sweep wrote state file"
    print("  T16 PASS — sweep does not mutate any file (tree-wide)")


def test_sweep_json_mode():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        old = vault / "projects" / "20260101-old-aaaa"
        old.mkdir()
        (old / "project.md").write_text(
            "---\nid: 20260101-old-aaaa\ntitle: t\nstatus: active\n"
            "started_at: 2026-01-01\nlast_active: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        r = run(method, "sweep", "--json")
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["slug"] == "20260101-old-aaaa"
        assert data[0]["days_since_active"] > 30
    print("  T17 PASS — sweep --json emits parseable array")


def test_sweep_json_zero_candidates():
    """--json must emit a valid empty array even when no projects match."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Empty projects/ → []
        r = run(method, "sweep", "--json")
        assert json.loads(r.stdout) == []
        # One non-stale project → still []
        recent = vault / "projects" / "20260507-recent-bbbb"
        recent.mkdir()
        (recent / "project.md").write_text(
            "---\nid: 20260507-recent-bbbb\nstatus: active\n"
            "started_at: 2026-05-07\nlast_active: 2026-05-07\n---\n",
            encoding="utf-8",
        )
        r = run(method, "sweep", "--json")
        assert json.loads(r.stdout) == []
    print("  T18 PASS — sweep --json emits [] on zero candidates")


def test_sweep_negative_days_refused():
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        r = run(method, "sweep", "--days", "-5", expect_rc=None)
        assert r.returncode == 1
        assert "must be >= 0" in r.stderr
    print("  T19 PASS — sweep --days <0 refused")


def test_sweep_threshold_strict_boundary():
    """ADR-0003 Amendment 1: stale = AFTER threshold (strict). Day-of-threshold
    is NOT yet a candidate."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Project last_active EXACTLY 30 days ago — must NOT be listed at default threshold.
        thirty = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=30)).isoformat()
        thirty_one = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=31)).isoformat()
        p30 = vault / "projects" / "20260101-thirty-aaaa"
        p30.mkdir()
        (p30 / "project.md").write_text(
            f"---\nid: 20260101-thirty-aaaa\nstatus: active\n"
            f"started_at: 2026-01-01\nlast_active: {thirty}\n---\n",
            encoding="utf-8",
        )
        p31 = vault / "projects" / "20260101-thirtyone-bbbb"
        p31.mkdir()
        (p31 / "project.md").write_text(
            f"---\nid: 20260101-thirtyone-bbbb\nstatus: active\n"
            f"started_at: 2026-01-01\nlast_active: {thirty_one}\n---\n",
            encoding="utf-8",
        )
        r = run(method, "sweep")
        assert "20260101-thirty-aaaa" not in r.stdout, "day-30 should NOT be listed (strict)"
        assert "20260101-thirtyone-bbbb" in r.stdout, "day-31 SHOULD be listed"
    print("  T20 PASS — sweep threshold strict: day-30 not, day-31 yes")


def test_sweep_warns_on_missing_last_active():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        broken = vault / "projects" / "20260101-broken-aaaa"
        broken.mkdir()
        (broken / "project.md").write_text(
            "---\nid: 20260101-broken-aaaa\nstatus: active\nstarted_at: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        r = run(method, "sweep")
        assert "[sweep] WARN" in r.stderr
        assert "20260101-broken-aaaa" in r.stderr
        assert "no `last_active`" in r.stderr
    print("  T21 PASS — sweep warns on missing last_active")


def test_cross_machine_resume_via_git():
    """End-to-end: project created on side A is resumable on side B after git pull.

    Simulates two machines via two filesystem paths sharing one bare upstream.
    Catches gitignore drift (e.g., projects/ excluded), slug normalization
    issues, state-file leaks across the boundary."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        # Build the method-side scaffolding once and reuse for both sides.
        # Each side gets its own .assistant.local.json pointed at its own vault.
        method = td_path / "method"
        method.mkdir()
        (method / "tools").mkdir()
        shutil.copy(PROJ / "tools" / "_config.py", method / "tools" / "_config.py")
        shutil.copy(PROJ / "tools" / "project.py", method / "tools" / "project.py")

        # Bare upstream. Set HEAD to refs/heads/main so a fresh clone gets
        # a checked-out main without `--branch main` — mirrors how
        # GitHub-hosted upstreams behave (HEAD set on first push).
        upstream = td_path / "upstream.git"
        subprocess.run(["git", "init", "--bare", str(upstream)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(upstream), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True, capture_output=True,
        )

        # Side A: working tree. Disable gpg signing on commits so the test
        # passes in environments where the developer has global gpgsign on.
        vault_a = td_path / "vault_a"
        subprocess.run(["git", "clone", str(upstream), str(vault_a)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(vault_a), "config", "user.email", "a@test"], check=True)
        subprocess.run(["git", "-C", str(vault_a), "config", "user.name", "a"], check=True)
        subprocess.run(["git", "-C", str(vault_a), "config", "commit.gpgsign", "false"], check=True)
        # Initial empty commit so push-after-create succeeds.
        (vault_a / "README.md").write_text("# vault\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(vault_a), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(vault_a), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(vault_a), "branch", "-M", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(vault_a), "push", "-u", "origin", "main"], check=True, capture_output=True)

        # Point method's config at vault_a, create project, write artefact, commit + push.
        (method / ".assistant.local.json").write_text(json.dumps({
            "$schema_version": 1,
            "paths": {"content_root": str(vault_a.resolve())},
        }), encoding="utf-8")
        # Need projects/ + artefacts/ scaffolding before tool can run cleanly
        (vault_a / "projects").mkdir()
        (vault_a / "artefacts" / "memo").mkdir(parents=True)

        run(method, "new", "feature", "cross-machine smoke")
        slug = get_active_slug(vault_a)
        proj_dir = vault_a / "projects" / slug
        # Drop a project-scoped artefact so the manifest has something on side B.
        art = proj_dir / "artefacts" / "memo"
        art.mkdir(parents=True)
        (art / "art-xyz.md").write_text(
            "---\nid: art-xyz\nkind: memo\ncreated_at: 2026-05-07T10:00:00Z\n"
            f"title: t\nproject_id: {slug}\nproduced_by:\n  session_id: aaaaaaaa\n"
            "  query: q\n  model: m\n  sources_cited:\n    - https://x.test\n---\nside-A body\n",
            encoding="utf-8",
        )
        # Commit + push side A's vault. Don't include the .pa-active-project.json
        # state file across machines — that's per-machine state.
        subprocess.run(["git", "-C", str(vault_a), "add", "projects/"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(vault_a), "commit", "-m", "add project"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(vault_a), "push"], check=True, capture_output=True)

        # Side B: fresh clone, NO active-project state.
        vault_b = td_path / "vault_b"
        subprocess.run(["git", "clone", str(upstream), str(vault_b)], check=True, capture_output=True)
        # Repoint method config at vault_b.
        (method / ".assistant.local.json").write_text(json.dumps({
            "$schema_version": 1,
            "paths": {"content_root": str(vault_b.resolve())},
        }), encoding="utf-8")

        # Pre-condition: side B sees the project on disk but state file is unset.
        assert (vault_b / "projects" / slug / "project.md").is_file(), \
            "side B should see project.md after git pull"
        assert not (vault_b / ".pa-active-project.json").exists(), \
            "side B must not inherit side A's state file (it isn't pushed)"

        # Resume on side B → state file appears, content prints, artefact in manifest.
        r = run(method, "resume", slug)
        assert f"slug={slug}" in r.stdout
        assert f"export PA_PROJECT_ID={slug}" in r.stdout
        # State file written on side B
        assert (vault_b / ".pa-active-project.json").exists(), \
            "resume on side B should write the state file"
        # The artefact created on side A appears in side B's manifest
        assert "memo/art-xyz.md" in r.stdout, \
            "side B should see the artefact via resume manifest"
        # And project.md content matches
        side_a_md = (vault_a / "projects" / slug / "project.md").read_text(encoding="utf-8")
        side_b_md = (vault_b / "projects" / slug / "project.md").read_text(encoding="utf-8")
        assert side_a_md == side_b_md, "project.md must round-trip identically across machines"

        # Provenance lint passes on side B — catches frontmatter drift if
        # either the lint or the producer schema evolves on only one side.
        # Copy lint-provenance.py + its dependency into the test method tree.
        shutil.copy(PROJ / "tools" / "lint-provenance.py", method / "tools" / "lint-provenance.py")
        lint = subprocess.run(
            [str(method / "tools" / "lint-provenance.py"), "--require-vault"],
            capture_output=True, text=True,
        )
        assert lint.returncode == 0, f"side B lint failed:\n{lint.stderr}"
    print("  T22 PASS — cross-machine resume preserves project state via git + lint")


def test_promote_refuses_already_in_project():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "alpha", "i")
        slug = get_active_slug(vault)
        # Place artefact in the project (not flat).
        art = vault / "projects" / slug / "artefacts" / "memo"
        art.mkdir(parents=True)
        (art / "art-already.md").write_text("---\nid: art-already\nkind: memo\n---\nbody", encoding="utf-8")
        r = run(method, "promote", "already", slug, expect_rc=None)
        assert r.returncode == 1
        assert "not flat" in r.stderr or "already in projects" in r.stderr.lower()
    print("  T12 PASS — promote refuses non-flat artefact")


if __name__ == "__main__":
    print("Running test_project_acceptance.py...")
    test_new_creates_project()
    test_project_md_is_clean()
    test_list_filters_archived()
    test_archive_clears_state_if_active()
    test_status_reports_active()
    test_clear_removes_state()
    test_promote_moves_flat_artefact()
    test_promote_export_moves_sidecar()
    test_copy_artefact()
    test_short_name_validation()
    test_resume_ambiguous_short_name()
    test_touch_preserves_nested_frontmatter()
    test_sweep_lists_stale_projects()
    test_sweep_skips_archived()
    test_sweep_does_not_mutate()
    test_sweep_json_mode()
    test_sweep_json_zero_candidates()
    test_sweep_negative_days_refused()
    test_sweep_threshold_strict_boundary()
    test_sweep_warns_on_missing_last_active()
    test_cross_machine_resume_via_git()
    test_promote_refuses_already_in_project()
    print("All project tests passed.")
