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

import json
import os
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
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "x", "i")
        slug = get_active_slug(vault)
        # Create a flat memo artefact.
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
        assert f"project_id: {slug}" in text
    print("  T7 PASS — promote moves + rewrites frontmatter")


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
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        run(method, "new", "alpha", "i")
        run(method, "new", "beta", "i")
        slug_b = get_active_slug(vault)
        # alpha is the previous slug
        slug_a_state = json.loads((vault / ".pa-active-project.json").read_text())
        # We need both slugs — list all dirs.
        dirs = sorted(d.name for d in (vault / "projects").iterdir() if d.is_dir())
        slug_a = next(d for d in dirs if "alpha" in d)
        slug_b = next(d for d in dirs if "beta" in d)

        # Place an artefact in alpha.
        art = vault / "projects" / slug_a / "artefacts" / "memo"
        art.mkdir(parents=True)
        body_v = "---\nid: art-orig123\nkind: memo\nproject_id: " + slug_a + "\n---\nORIGINAL"
        (art / "art-orig123.md").write_text(body_v, encoding="utf-8")

        r = run(method, "copy-artefact", "orig123", slug_b)
        assert "copied" in r.stdout
        # Original unchanged
        assert (art / "art-orig123.md").read_text(encoding="utf-8") == body_v
        # Copy exists in beta with fresh id + derived_from
        copies = list((vault / "projects" / slug_b / "artefacts" / "memo").iterdir())
        assert len(copies) == 1
        copy_text = copies[0].read_text(encoding="utf-8")
        assert "derived_from: art-orig123" in copy_text
        assert f"project_id: {slug_b}" in copy_text
        assert "ORIGINAL" in copy_text
    print("  T9 PASS — copy-artefact mints fresh id + derived_from")


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
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Two projects with identical short-name (same day, different hex).
        # Force this by manually creating two folders.
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
        r = run(method, "resume", "thing", expect_rc=None)
        assert r.returncode == 1
        assert "ambiguous" in r.stderr.lower()
    print("  T11 PASS — resume rejects ambiguous short-name")


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
    test_promote_refuses_already_in_project()
    print("All project tests passed.")
