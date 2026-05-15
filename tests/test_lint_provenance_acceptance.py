#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/lint-provenance.py — provenance lint per ADR-0003 (#85).

Tests:
  T1  — clean fixture (method glossary OK, vault KB grandfathered, no artefacts) exits 0.
  T2  — KB heading dated post-ADR-acceptance without produced_by exits 1.
  T3  — KB heading dated pre-ADR-acceptance without produced_by exits 0 (grandfathered).
  T4  — produced_by comment with non-canonical source exits 1.
  T5  — method glossary containing produced_by comment exits 1.
  T6  — artefact .md without YAML frontmatter exits 1.
  T7  — artefact .md with frontmatter missing required produced_by keys exits 1.
  T8  — artefact .md with non-canonical source in sources_cited exits 1.
  T9  — export artefact without sibling provenance.json exits 1.
  T10 — export artefact with valid sidecar exits 0.
  T11 — --require-vault with no config exits 2.
  T12 — --method-only skips vault checks even when vault config present.
  T13 — produced_by with malformed session id exits 1.
  T14 — KB section with no Date line is grandfathered (no produced_by required).
  T15 — produced_by with all required fields and canonical sources exits 0.
  T16 — undated heading with entry-shape body (bullet **field:**) requires produced_by.
  T17 — undated heading without entry shape (format docs) is grandfathered.
  T18 — `**Last verified:**` qualifies as a date marker for grandfathering.
  T19 — schema/format examples inside fenced code blocks don't trip heading detection.
  T20 — project-scoped artefact missing project_id fails (#88 / #96).
  T21 — project-scoped artefact with mismatched project_id fails.
  T22 — `art://<uuid>` is accepted as canonical source form.
  T23 — same uuid in two locations triggers artefact-uuid-collision.
  T24 — project-scoped export sidecar must carry project_id too.
  T25 — dangling art:// reference fails (#98).
  T26 — flat artefact citing art://<flat-uuid> resolves cleanly.
  T27 — self-referential art://<own-uuid> passes (semantic: derived_from etc.).
  T28 — flat artefact resolves art://<project-tier-uuid>.
  T29 — project directory with hand-rolled slug fails (#99).
  T30 — .template/ and other dot-prefixed directories are exempt from slug check.
  T31 — kind=memo with all 4 drift fields populated correctly passes (#137).
  T32 — drift_candidate: true with missing required drift field fails distinctly.
  T33 — kind=memo without any drift fields still passes (F1 backward-compat).
  T34 — drift_candidate: true with unresolvable affects_decision fails (F2).
  T35 — drift_candidate: true with shape-malformed affects_decision fails distinctly (F3).
  T36 — drift_confidence outside {high, medium, low} fails.
  T37 — drift_candidate: false with no other drift fields passes (flag-gated, F1).
  T38 — empty drift_candidate value fails (closes fail-open from review).
  T39 — non-bool drift_candidate value fails distinctly.
  T40 — drift_candidate: true on non-memo kind fails (kind=memo scope).
  T41 — `art://art-<uuid>` shape tolerated alongside the ADR-canonical
        `art://<uuid>` (regression for #193 / C3 / #196).
  T42 — `kb#<heading>` accepts literal heading text with spaces, punctuation,
        em-dashes, unicode (regression for #199 / C1 / #200).
  T43 — `kb#`-shaped degenerate forms (`kb#`, `kb# X`, `kb`, etc.) refused.
  T44 — `kb#X<trailing-whitespace>` refused at the regex level (YAML strips
        trailing whitespace before lint sees it, so this tests the
        defense-in-depth on direct-dict code paths).
  T45 — `source-pin` kind accepted with `upstream:` block in produced_by
        (regression for #198 / C1 / #201).
  T46 — `source-pin` missing `upstream:` block refused.
  T47 — `source-pin` with malformed `upstream:` (not a dict / missing `kind`
        subfield) refused.
  T48 — `source-pin` kind in wrong directory (artefacts/memo/) tolerated
        today; documents the gap until strict kind-vs-dir cross-check lands.
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
LINT = PROJ / "tools" / "lint-provenance.py"


def make_fixture(tmpdir: Path) -> tuple[Path, Path]:
    """Create method + vault skeletons. Returns (method_root, content_root).

    The method root is a fresh copy of the real one (so the script under test
    sees its config loader and a `kb/glossary.md`), pointed at our tmp vault.
    """
    method = tmpdir / "method"
    vault = tmpdir / "vault"
    method.mkdir()
    vault.mkdir()
    # Copy the bare minimum from real method root so _config.py can run.
    (method / "tools").mkdir()
    shutil.copy(PROJ / "tools" / "_config.py", method / "tools" / "_config.py")
    shutil.copy(PROJ / "tools" / "lint-provenance.py", method / "tools" / "lint-provenance.py")
    # Glossary placeholder (clean by default).
    (method / "kb").mkdir()
    (method / "kb" / "glossary.md").write_text("# Glossary\n\n## a-term\n- **Source:** foo\n", encoding="utf-8")
    # Vault structure.
    (vault / "kb").mkdir()
    (vault / "kb" / "people.md").write_text("# People\n", encoding="utf-8")
    (vault / "kb" / "org.md").write_text("# Org\n", encoding="utf-8")
    (vault / "kb" / "decisions.md").write_text("# Decisions\n", encoding="utf-8")
    (vault / "artefacts").mkdir()
    for kind in ("memo", "export", "analysis", "plan", "draft", "report"):
        (vault / "artefacts" / kind).mkdir()
    (method / ".assistant.local.json").write_text(json.dumps({
        "$schema_version": 1,
        "paths": {"content_root": str(vault.resolve())},
    }), encoding="utf-8")
    return method, vault


def run_lint(method: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(method / "tools" / "lint-provenance.py"), *args],
        capture_output=True, text=True,
    )


def test_clean_fixture_exits_0():
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        r = run_lint(method)
        assert r.returncode == 0, f"expected 0, got {r.returncode}\nSTDERR:\n{r.stderr}"
    print("  T1 PASS — clean fixture exits 0")


def test_post_adr_kb_heading_without_produced_by_fails():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "kb" / "decisions.md").write_text(
            "# Decisions\n\n## New decision\n- **Date:** 2026-06-01\n- **Status:** decided\n\nbody.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "kb-missing-produced-by" in r.stderr
    print("  T2 PASS — post-ADR KB heading without produced_by fails")


def test_pre_adr_kb_heading_grandfathered():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "kb" / "decisions.md").write_text(
            "# Decisions\n\n## Old decision\n- **Date:** 2026-04-01\n- **Status:** decided\n\nbody.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"got {r.returncode}\n{r.stderr}"
    print("  T3 PASS — pre-ADR KB heading grandfathered")


def test_non_canonical_source_in_kb_fails():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "kb" / "decisions.md").write_text(
            "# Decisions\n\n## A decision\n- **Date:** 2026-06-01\n"
            '<!-- produced_by: session=abcd1234, query="x", at=2026-06-01T10:00:00Z, sources=[slack-link.html] -->\n'
            "body.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "not canonical" in r.stderr or "kb-malformed-produced-by" in r.stderr
    print("  T4 PASS — non-canonical source in KB fails")


def test_glossary_with_produced_by_fails():
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        (method / "kb" / "glossary.md").write_text(
            "# Glossary\n\n## a-term\n"
            '<!-- produced_by: session=abcd1234, query="x", at=2026-06-01T10:00:00Z, sources=[https://x.test] -->\n'
            "definition.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "glossary-must-be-clean" in r.stderr
    print("  T5 PASS — glossary with produced_by fails")


def test_artefact_md_without_frontmatter_fails():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-001.md").write_text("just a body, no frontmatter", encoding="utf-8")
        r = run_lint(method)
        assert r.returncode == 1
        assert "artefact-missing-frontmatter" in r.stderr
    print("  T6 PASS — artefact .md without frontmatter fails")


def test_artefact_md_missing_produced_by_keys_fails():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-002.md").write_text(
            "---\nid: art-002\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
            "title: t\nproduced_by:\n  session_id: abcd1234\n  query: x\n---\nbody",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        # missing model + sources_cited
        assert "produced-by-missing-keys" in r.stderr or "empty-sources" in r.stderr
    print("  T7 PASS — artefact .md missing produced_by keys fails")


def test_artefact_md_non_canonical_source_fails():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-003.md").write_text(
            "---\nid: art-003\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
            "title: t\nproduced_by:\n  session_id: abcd1234\n  query: x\n  model: claude-opus-4-7\n"
            "  sources_cited:\n    - random-thing\n---\nbody",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "non-canonical-source" in r.stderr
    print("  T8 PASS — artefact .md non-canonical source fails")


def test_export_without_sidecar_fails():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "export" / "art-004.csv").write_text("a,b,c\n", encoding="utf-8")
        r = run_lint(method)
        assert r.returncode == 1
        assert "export-missing-sidecar" in r.stderr
    print("  T9 PASS — export without sidecar fails")


def test_export_with_valid_sidecar_passes():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "export" / "art-005.csv").write_text("a,b,c\n", encoding="utf-8")
        (vault / "artefacts" / "export" / "art-005.provenance.json").write_text(
            json.dumps({
                "session_id": "abcd1234",
                "query": "x",
                "model": "claude-opus-4-7",
                "sources_cited": ["https://x.test"],
            }), encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"got {r.returncode}\n{r.stderr}"
    print("  T10 PASS — export with valid sidecar passes")


def test_require_vault_no_config_exits_2():
    with tempfile.TemporaryDirectory() as td:
        method, _vault = make_fixture(Path(td))
        (method / ".assistant.local.json").unlink()
        r = run_lint(method, "--require-vault")
        assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"
    print("  T11 PASS — --require-vault with no config exits 2")


def test_method_only_skips_vault():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Add a vault violation that --method-only must NOT see.
        (vault / "kb" / "decisions.md").write_text(
            "# Decisions\n\n## broken\n- **Date:** 2026-06-01\n\nno provenance.\n",
            encoding="utf-8",
        )
        r = run_lint(method, "--method-only")
        assert r.returncode == 0, f"--method-only should ignore vault violations\n{r.stderr}"
    print("  T12 PASS — --method-only skips vault checks")


def test_malformed_session_id_fails():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "kb" / "decisions.md").write_text(
            "# Decisions\n\n## A\n- **Date:** 2026-06-01\n"
            '<!-- produced_by: session=NOTAHEX, query="x", at=2026-06-01T10:00:00Z, sources=[https://x.test] -->\n'
            "body.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "8 lowercase hex" in r.stderr
    print("  T13 PASS — malformed session id fails")


def test_kb_section_without_date_grandfathered():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Pre-existing entries (file headers, schema docs in same file) without
        # a Date line shouldn't trigger the lint.
        (vault / "kb" / "decisions.md").write_text(
            "# Decisions\n\n## Format\n\nThis file uses the schema below.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"got {r.returncode}\n{r.stderr}"
    print("  T14 PASS — section without Date is grandfathered")


def test_well_formed_produced_by_passes():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "kb" / "decisions.md").write_text(
            "# Decisions\n\n## A new decision\n- **Date:** 2026-06-01\n"
            '<!-- produced_by: session=abcd1234, query="why X", at=2026-06-01T10:00:00Z, '
            "sources=[kb#prior-heading, mem://mem-abc, https://x.test] -->\n"
            "body.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"got {r.returncode}\n{r.stderr}"
    print("  T15 PASS — well-formed produced_by passes")


def test_undated_entry_shape_requires_produced_by():
    """B1 fixup: people.md / org.md don't use `**Date:**`; an undated entry
    section can't slip past grandfathering by lacking a date marker."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "kb" / "people.md").write_text(
            "# People\n\n## Jane Doe\n- **Role / relation:** Engineer\n- **Source:** manual\n\nbody.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1, f"expected 1 (entry shape without produced_by), got {r.returncode}\n{r.stderr}"
        assert "entry-shape body" in r.stderr or "kb-missing-produced-by" in r.stderr
    print("  T16 PASS — undated entry-shape body requires produced_by")


def test_undated_format_section_grandfathered():
    """Format/schema sections (no bullet **field:** lines) stay grandfathered
    regardless of date — they're documentation, not entries."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "kb" / "people.md").write_text(
            "# People\n\n## Format\n\nEach entry follows the format below. See template.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"format section should be grandfathered\n{r.stderr}"
    print("  T17 PASS — format section is grandfathered")


def test_last_verified_qualifies_as_date_marker():
    """people.md / org.md use `**Last verified:**` instead of `**Date:**` —
    must qualify as the date marker for grandfathering decisions."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "kb" / "people.md").write_text(
            "# People\n\n## Pre-existing\n- **Role / relation:** Engineer\n"
            "- **Last verified:** 2026-04-01\n- **Source:** manual\n\nbody.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"pre-ADR Last-verified should be grandfathered\n{r.stderr}"
        # Now flip to post-ADR — same field — should NOW require produced_by.
        (vault / "kb" / "people.md").write_text(
            "# People\n\n## New\n- **Role / relation:** PM\n"
            "- **Last verified:** 2026-06-01\n- **Source:** manual\n\nbody.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "kb-missing-produced-by" in r.stderr
    print("  T18 PASS — `**Last verified:**` qualifies as date marker")


def test_code_fence_examples_dont_trip_lint():
    """The KB files document their entry schema inside fenced code blocks.
    Those `## <Title>` + `- **Field:**` examples must NOT be treated as real
    entries — they're documentation."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "kb" / "decisions.md").write_text(
            "# Decisions\n\nEach entry follows the format:\n\n"
            "```\n## <Decision title>\n- **Date:** <YYYY-MM-DD>\n- **Status:** decided\n```\n\n"
            "Real content below — none yet.\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"code-fence examples should be ignored\n{r.stderr}"
    print("  T19 PASS — fenced code examples ignored")


def _project_dirs(vault: Path, slug: str) -> Path:
    proj = vault / "projects" / slug
    (proj / "artefacts" / "memo").mkdir(parents=True, exist_ok=True)
    (proj / "artefacts" / "export").mkdir(parents=True, exist_ok=True)
    return proj


def _project_artefact_md(slug: str, art_uuid: str, project_id: str | None,
                         sources: list[str] = None) -> str:
    sources = sources or ["https://x.test"]
    src_lines = "\n".join(f"    - {s}" for s in sources)
    pid_line = f"\nproject_id: {project_id}" if project_id else ""
    return (
        f"---\nid: art-{art_uuid}\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
        f"title: t{pid_line}\nproduced_by:\n  session_id: aaaaaaaa\n  query: x\n  model: m\n"
        f"  sources_cited:\n{src_lines}\n---\nbody"
    )


def test_project_scoped_missing_project_id():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "memo" / "art-noproject.md").write_text(
            _project_artefact_md("20260507-test-aaaa", "noproject", None),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "artefact-missing-project-id" in r.stderr
    print("  T20 PASS — project-scoped artefact missing project_id fails")


def test_project_scoped_mismatched_project_id():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "memo" / "art-mismatch.md").write_text(
            _project_artefact_md("20260507-test-aaaa", "mismatch", "20260101-other-bbbb"),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "artefact-project-id-mismatch" in r.stderr
    print("  T21 PASS — project_id mismatch fails")


def test_art_canonical_source_accepted():
    """art://<uuid> is a canonical source form — provided the uuid resolves
    to an existing artefact in the vault (project tier ∪ flat tier)."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        # Create the artefact that art://orig-123 will reference.
        (proj / "artefacts" / "memo" / "art-orig-123.md").write_text(
            _project_artefact_md("20260507-test-aaaa", "orig-123", "20260507-test-aaaa"),
            encoding="utf-8",
        )
        # Create the referencing artefact.
        (proj / "artefacts" / "memo" / "art-uses-art-ref.md").write_text(
            _project_artefact_md("20260507-test-aaaa", "uses-art-ref", "20260507-test-aaaa",
                                  sources=["art://orig-123", "kb#some-heading"]),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"art:// should be canonical\n{r.stderr}"
    print("  T22 PASS — art://<uuid> accepted as canonical source (resolves)")


def test_uuid_collision_across_tiers():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Same uuid in flat AND in a project
        flat_md = vault / "artefacts" / "memo" / "art-collision.md"
        flat_md.write_text(
            "---\nid: art-collision\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
            "title: t\nproduced_by:\n  session_id: aaaaaaaa\n  query: x\n  model: m\n"
            "  sources_cited:\n    - https://x.test\n---\nbody",
            encoding="utf-8",
        )
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "memo" / "art-collision.md").write_text(
            _project_artefact_md("20260507-test-aaaa", "collision", "20260507-test-aaaa"),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "artefact-uuid-collision" in r.stderr
    print("  T23 PASS — uuid collision across tiers detected")


def test_project_scoped_export_sidecar_project_id():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        body = proj / "artefacts" / "export" / "art-sheet.csv"
        sidecar = proj / "artefacts" / "export" / "art-sheet.provenance.json"
        body.write_text("a,b\n1,2\n", encoding="utf-8")
        # Missing project_id
        sidecar.write_text(json.dumps({
            "session_id": "aaaaaaaa", "query": "x", "model": "m",
            "sources_cited": ["https://x.test"],
        }), encoding="utf-8")
        r = run_lint(method)
        assert r.returncode == 1
        assert "artefact-missing-project-id" in r.stderr
    print("  T24 PASS — project-scoped export sidecar requires project_id")


def test_dangling_art_ref_fails():
    """Per #98: art://<uuid> in sources_cited that doesn't resolve to any
    existing artefact in the vault triggers `artefact-dangling-art-ref`."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "memo" / "art-references-ghost.md").write_text(
            _project_artefact_md(
                "20260507-test-aaaa", "references-ghost", "20260507-test-aaaa",
                sources=["art://nonexistent-ghost-uuid"],
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "artefact-dangling-art-ref" in r.stderr
        assert "nonexistent-ghost-uuid" in r.stderr
    print("  T25 PASS — dangling art:// reference fails")


def test_flat_artefact_resolves_art_ref():
    """art://<uuid> resolving to a FLAT (project-less) artefact is also valid."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Create a flat artefact + a project artefact that references it.
        (vault / "artefacts" / "memo" / "art-flat-target.md").write_text(
            "---\nid: art-flat-target\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
            "title: t\nproduced_by:\n  session_id: aaaaaaaa\n  query: x\n  model: m\n"
            "  sources_cited:\n    - https://x.test\n---\nbody",
            encoding="utf-8",
        )
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "memo" / "art-references-flat.md").write_text(
            _project_artefact_md(
                "20260507-test-aaaa", "references-flat", "20260507-test-aaaa",
                sources=["art://flat-target"],
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"flat-target should resolve\n{r.stderr}"
    print("  T26 PASS — art://<uuid> resolves to flat artefact")


def test_self_reference_passes():
    """An artefact citing its own art:// uuid should resolve cleanly — the
    index includes self. derived_from / cycle patterns rely on this."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "memo" / "art-cycle.md").write_text(
            _project_artefact_md(
                "20260507-test-aaaa", "cycle", "20260507-test-aaaa",
                sources=["art://cycle"],
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"self-ref should resolve\n{r.stderr}"
    print("  T27 PASS — self-referential art:// passes")


def test_193_art_prefixed_uri_body_tolerated():
    """Regression for #193 / C3 (#196): the ref-check tolerates `art://art-<uuid>`
    URI bodies (with the redundant `art-` prefix) in addition to the canonical
    `art://<uuid>` shape from ADR-0003. Some hand-authored memos (e.g. the
    Vera vision memos that surfaced this bug) use the `art-`-prefixed form.

    Pre-fix, the index was keyed on bare uuid but the ref-check did NOT strip
    the `art-` prefix from URI bodies — so `art://art-<uuid>` always missed
    the index. Post-fix, both `:706` (sources_cited) and `:630`
    (affects_decision) strip a leading `art-` before lookup.

    Mirrors `tools/repros/lint-provenance-art-prefix-mismatch.py` semantics
    in a durable test. T22 covers the canonical bare-uuid shape; T29 covers
    the `art-`-prefixed shape — both must resolve."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "memo" / "art-c193-target.md").write_text(
            _project_artefact_md(
                "20260507-test-aaaa", "c193-target", "20260507-test-aaaa",
                sources=["art://art-c193-target"],  # `art-`-prefixed self-ref
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, (
            f"art://art-<id> self-reference should resolve (tolerance fix)\n"
            f"stderr: {r.stderr}"
        )
        assert "artefact-dangling-art-ref" not in r.stderr, (
            f"no dangling-art-ref expected\nstderr: {r.stderr}"
        )
    print("  T41 PASS — #193 art://art-<id> tolerated (in addition to ADR-canonical art://<id>)")


def test_flat_resolves_project_uuid():
    """Cross-tier resolution works in BOTH directions: a flat artefact citing
    art://<project-tier-uuid> must resolve too."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Project artefact (target).
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "memo" / "art-proj-target.md").write_text(
            _project_artefact_md("20260507-test-aaaa", "proj-target", "20260507-test-aaaa"),
            encoding="utf-8",
        )
        # Flat artefact (referencer).
        (vault / "artefacts" / "memo" / "art-flat-ref.md").write_text(
            "---\nid: art-flat-ref\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
            "title: t\nproduced_by:\n  session_id: aaaaaaaa\n  query: x\n  model: m\n"
            "  sources_cited:\n    - art://proj-target\n---\nbody",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"flat → project art:// should resolve\n{r.stderr}"
    print("  T28 PASS — flat artefact resolves art://<project-uuid>")


def test_198_source_pin_kind_accepted_with_upstream():
    """Regression for #198 / C1 (#201): `source-pin` is a valid artefact kind
    carrying an `upstream:` block in produced_by instead of `sources_cited:`.
    Used for pre-harvest snapshots of upstream content (granola meetings,
    slack threads) awaiting canonical `mem://` promotion. See ADR-0003
    Amendment 2."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "source-pin").mkdir()
        (proj / "artefacts" / "source-pin" / "art-pin-001.md").write_text(
            "---\n"
            "id: art-pin-001\n"
            "kind: source-pin\n"
            "project_id: 20260507-test-aaaa\n"
            "created_at: 2026-05-15\n"
            "title: \"Pinned upstream snapshot\"\n"
            "upstream:\n"
            "  kind: granola_note\n"
            "  granola_meeting_id: 25d38b46-7574-404b-b5b2-fcc2d17be67d\n"
            "  date: 2026-05-14T10:05:00+01:00\n"
            "produced_by:\n"
            "  session_id: aaaaaaaa\n"
            "  query: \"pin granola meeting before harvest lands the canonical mem\"\n"
            "  model: claude-opus-4-7\n"
            "---\n\n"
            "# Pinned upstream snapshot\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, (
            f"source-pin with upstream block should pass\nstderr: {r.stderr}"
        )
    print("  T45 PASS — #198 source-pin kind accepted with upstream block")


def test_198_source_pin_refuses_missing_upstream():
    """`source-pin` artefact without a top-level `upstream:` block
    must fail — F2 on #201."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "source-pin").mkdir()
        (proj / "artefacts" / "source-pin" / "art-pin-bad.md").write_text(
            "---\n"
            "id: art-pin-bad\n"
            "kind: source-pin\n"
            "project_id: 20260507-test-aaaa\n"
            "created_at: 2026-05-15\n"
            "title: \"Bad source-pin missing upstream\"\n"
            "produced_by:\n"
            "  session_id: aaaaaaaa\n"
            "  query: \"missing upstream\"\n"
            "  model: claude-opus-4-7\n"
            "---\n\n"
            "# Bad source-pin\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1, (
            f"source-pin without upstream should fail\nstderr: {r.stderr}"
        )
        assert "upstream" in r.stderr, (
            f"expected upstream-related violation\nstderr: {r.stderr}"
        )
    print("  T46 PASS — #198 source-pin without upstream block refused")


def test_198_source_pin_refuses_malformed_upstream():
    """`source-pin` with an `upstream:` field that isn't a dict, or missing
    `kind:` subfield, must fail."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "source-pin").mkdir()
        # upstream is a string, not a dict
        (proj / "artefacts" / "source-pin" / "art-pin-malformed.md").write_text(
            "---\n"
            "id: art-pin-malformed\n"
            "kind: source-pin\n"
            "project_id: 20260507-test-aaaa\n"
            "created_at: 2026-05-15\n"
            "title: \"Malformed upstream\"\n"
            "upstream: \"not-a-dict\"\n"
            "produced_by:\n"
            "  session_id: aaaaaaaa\n"
            "  query: \"malformed upstream\"\n"
            "  model: claude-opus-4-7\n"
            "---\n\n"
            "body\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1, (
            f"source-pin with non-dict upstream should fail\nstderr: {r.stderr}"
        )
        assert "malformed-upstream" in r.stderr or "upstream must be" in r.stderr, (
            f"expected malformed-upstream violation\nstderr: {r.stderr}"
        )
    print("  T47 PASS — #198 source-pin with non-dict upstream refused")


def test_198_source_pin_in_wrong_directory_fails():
    """A `kind: source-pin` artefact placed in `artefacts/memo/` (wrong
    directory) should fail the lint via the directory-walk: only files
    in a VALID_KINDS directory are indexed, but the kind-vs-directory
    mismatch isn't currently checked (the lint walks dir-by-kind). This
    test locks in expected behavior so a future strict-mode kind-dir
    cross-check can build on it."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        # kind: source-pin BUT in artefacts/memo/. With no kind-vs-dir
        # cross-check, the lint should still accept (today). This test
        # documents that behavior. Strict check is a future tightening.
        (proj / "artefacts" / "memo" / "art-misplaced-pin.md").write_text(
            "---\n"
            "id: art-misplaced-pin\n"
            "kind: source-pin\n"
            "project_id: 20260507-test-aaaa\n"
            "created_at: 2026-05-15\n"
            "title: \"Misplaced source-pin\"\n"
            "upstream:\n"
            "  kind: granola_note\n"
            "produced_by:\n"
            "  session_id: aaaaaaaa\n"
            "  query: \"misplaced\"\n"
            "  model: claude-opus-4-7\n"
            "---\n\n"
            "body\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        # Today: passes (kind is valid, frontmatter is valid, only check is
        # directory walk which finds the file in `artefacts/memo/`).
        assert r.returncode == 0, (
            f"source-pin in artefacts/memo/ tolerated today (no kind-dir cross-check)\n"
            f"stderr: {r.stderr}"
        )
    print("  T48 PASS — #198 source-pin in wrong dir documented (no strict-mode cross-check today)")


def test_199_kb_heading_literal_text_accepted():
    """Regression for #199 / C1 (#200): `kb#<heading>` accepts the literal
    heading text — spaces, punctuation, em-dashes are all canonical. The
    old shape `kb#[\\w\\-]+` rejected the natural human + agent form (every
    Vera vision memo carried `kb#Phase 1 is Atlas 2.0, Vera is Phase 2`
    and similar). No tool resolves `kb#` references programmatically; the
    lint just verifies shape. Widening aligns lint with how people write."""
    valid_headings = [
        "kb#simple",                                                  # slug-shape (back-compat)
        "kb#Phase 1 is Atlas 2.0, Vera is Phase 2",                   # spaces, digits, comma, period
        "kb#Core value props — Vera (Atlas v2.0) + BADAS",            # em-dash, parens
        "kb#Atlas exposes all its data sources via MCP",              # spaces only
        "kb#Vera (Atlas v2.0) becomes unified hub for live + historical",  # mixed
        "kb#x",                                                       # single char body
        "kb#Phase ❶ — Vera",                                          # unicode (per pr-challenger #1 on #202)
        "kb#Atlas#2.0-priorities",                                    # body contains `#` (real KB headings can)
    ]
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        proj = _project_dirs(vault, "20260507-test-aaaa")
        (proj / "artefacts" / "memo" / "art-kb-shapes.md").write_text(
            _project_artefact_md(
                "20260507-test-aaaa", "kb-shapes", "20260507-test-aaaa",
                sources=valid_headings,
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, (
            f"all literal kb# heading shapes should be canonical\nstderr: {r.stderr}"
        )
        assert "not canonical" not in r.stderr, (
            f"no non-canonical-source violations expected\nstderr: {r.stderr}"
        )
    print(f"  T42 PASS — #199 kb# literal heading text accepted ({len(valid_headings)} shape variants)")


def test_199_kb_heading_degenerate_shapes_refused():
    """Regression for #199 / C1 (#200) F2: the widened regex must still
    refuse degenerate shapes — `kb#` alone, `kb# X` (whitespace right after
    the `#`), and the bare `kb` (no `#`)."""
    # YAML loader strips trailing whitespace from scalar values, so trailing-
    # whitespace cases (`kb#X `, `kb#X\t`) cannot be exercised through the
    # YAML fixture path — they get normalized to `kb#X` before the lint sees
    # them. The regex still refuses them as defense-in-depth for any code
    # path that bypasses YAML normalization (e.g., direct dict construction);
    # see the direct-regex test `test_199_kb_regex_refuses_trailing_ws` below.
    bad_headings = [
        "kb#",                # no body
        "kb# X",              # leading whitespace after #
        "kb#\tX",             # leading tab after #
        "kb",                 # no # at all
        "kb#   ",             # only whitespace body
    ]
    for bad in bad_headings:
        with tempfile.TemporaryDirectory() as td:
            method, vault = make_fixture(Path(td))
            proj = _project_dirs(vault, "20260507-test-aaaa")
            (proj / "artefacts" / "memo" / "art-bad-kb.md").write_text(
                _project_artefact_md(
                    "20260507-test-aaaa", "bad-kb", "20260507-test-aaaa",
                    sources=[bad],
                ),
                encoding="utf-8",
            )
            r = run_lint(method)
            assert r.returncode == 1, (
                f"degenerate kb# shape {bad!r} should refuse but lint exited "
                f"{r.returncode}\nstderr: {r.stderr}"
            )
            assert "not canonical" in r.stderr, (
                f"expected non-canonical-source violation for {bad!r}\n"
                f"stderr: {r.stderr}"
            )
    print("  T43 PASS — #199 degenerate kb# shapes refused (5 cases)")


def test_199_kb_regex_refuses_trailing_ws():
    """Direct-regex test for trailing-whitespace refusal (per pr-challenger #2
    on #202). YAML strips trailing whitespace from scalar values before the
    lint sees them, so this case can't be exercised through the fixture path.
    But the regex itself defends against it for any future code path that
    bypasses YAML normalization."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "lint_provenance",
        PROJ / "tools" / "lint-provenance.py",
    )
    lint_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint_mod)
    CANON = lint_mod.CANONICAL_SOURCE_RE
    # Trailing whitespace must refuse.
    assert not CANON.match("kb#X "), "trailing space should refuse"
    assert not CANON.match("kb#X\t"), "trailing tab should refuse"
    assert not CANON.match("kb#X Y  "), "trailing double-space should refuse"
    # But valid shapes still pass.
    assert CANON.match("kb#X"), "single char should accept"
    assert CANON.match("kb#X Y"), "internal space should accept"
    assert CANON.match("kb#Atlas exposes data via MCP"), "production shape should accept"
    print("  T44 PASS — #199 regex refuses trailing whitespace (3 cases) + accepts internal whitespace")


def test_malformed_project_slug_fails():
    """Per #99: hand-rolled project directory names that don't match
    `<YYYYMMDD>-<short-name>-<4hex>` are refused. Several malformed shapes
    locked in to defend against future regex regressions."""
    bad_slugs = [
        "test",                       # no date, no hex
        "20260507-aaaa",              # missing short-name
        "20260507---aaaa",            # empty short-name (just hyphens)
        "20260507-Foo-aaaa",          # uppercase short-name
        "20260507-foo-bar",           # bad hex (not 4 lowercase hex)
        "20260507-foo-AAAA",          # uppercase hex
        "2026-05-07-foo-aaaa",        # date with hyphens not YYYYMMDD
    ]
    for bad_name in bad_slugs:
        with tempfile.TemporaryDirectory() as td:
            method, vault = make_fixture(Path(td))
            (vault / "projects").mkdir()
            bad = vault / "projects" / bad_name
            bad.mkdir()
            (bad / "project.md").write_text(
                f"---\nid: {bad_name}\ntitle: t\nstatus: active\n"
                f"started_at: 2026-05-08\nlast_active: 2026-05-08\n---\n",
                encoding="utf-8",
            )
            r = run_lint(method)
            assert r.returncode == 1, f"slug {bad_name!r} should fail; got rc={r.returncode}"
            assert "project-slug-malformed" in r.stderr, f"slug {bad_name!r}: {r.stderr}"
            assert repr(bad_name) in r.stderr or bad_name in r.stderr
    print("  T29 PASS — 7 malformed slug shapes all rejected")


def test_dot_prefixed_dirs_exempt_from_slug_check():
    """`.template/` and other dot-prefixed directories under projects/ are
    intentional exemptions (they hold scaffolding, not real projects)."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "projects").mkdir()
        # Create a .template dir that should NOT trigger the slug check.
        template = vault / "projects" / ".template"
        template.mkdir()
        (template / "project.md").write_text(
            "---\nid: <slug>\ntitle: <short>\n---\n", encoding="utf-8",
        )
        # Also a real conforming slug, to confirm the lint still walks others.
        real = vault / "projects" / "20260507-real-aaaa"
        real.mkdir()
        (real / "project.md").write_text(
            "---\nid: 20260507-real-aaaa\ntitle: r\nstatus: active\n"
            "started_at: 2026-05-07\nlast_active: 2026-05-07\n---\n",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"clean .template + real slug should pass\n{r.stderr}"
    print("  T30 PASS — dot-prefixed dirs exempt from slug check")


def _drift_memo_md(art_uuid: str, *, drift_candidate: str | None = None,
                   affects_decision: str | None = None,
                   drift_claim: str | None = None,
                   drift_confidence: str | None = None,
                   sources: list[str] = None) -> str:
    """Render a kind=memo artefact .md with optional drift fields.

    Each `None` field is omitted entirely from frontmatter (not emitted as
    empty string) — matches how a real producer would write the file."""
    sources = sources or ["https://x.test"]
    src_lines = "\n".join(f"    - {s}" for s in sources)
    optional_lines = []
    if drift_candidate is not None:
        optional_lines.append(f"drift_candidate: {drift_candidate}")
    if affects_decision is not None:
        optional_lines.append(f"affects_decision: {affects_decision}")
    if drift_claim is not None:
        optional_lines.append(f"drift_claim: {drift_claim}")
    if drift_confidence is not None:
        optional_lines.append(f"drift_confidence: {drift_confidence}")
    optional = ("\n" + "\n".join(optional_lines)) if optional_lines else ""
    return (
        f"---\nid: art-{art_uuid}\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
        f"title: drift candidate{optional}\nproduced_by:\n  session_id: aaaaaaaa\n"
        f"  query: scan kb decisions for drift\n  model: claude-opus-4-7\n"
        f"  sources_cited:\n{src_lines}\n---\nbody"
    )


def test_drift_candidate_well_formed_passes():
    """T31: kind=memo with all 4 drift fields populated correctly passes."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Create the artefact that affects_decision will resolve to.
        (vault / "artefacts" / "memo" / "art-source-decision.md").write_text(
            _drift_memo_md("source-decision"), encoding="utf-8",
        )
        (vault / "artefacts" / "memo" / "art-drift-candidate.md").write_text(
            _drift_memo_md(
                "drift-candidate",
                drift_candidate="true",
                affects_decision="art://source-decision",
                drift_claim="Decision X says Y, but recent memory N indicates Z.",
                drift_confidence="high",
                sources=["mem://mem-abc", "kb#decision-x"],
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"well-formed drift-candidate should pass\n{r.stderr}"
    print("  T31 PASS — well-formed drift-candidate memo passes")


def test_drift_candidate_missing_required_fails():
    """T32: drift_candidate: true but missing one of the 3 required fields
    fails with `drift-missing-required` (distinct error, not a generic one)."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-source.md").write_text(
            _drift_memo_md("source"), encoding="utf-8",
        )
        # Missing drift_claim entirely.
        (vault / "artefacts" / "memo" / "art-incomplete.md").write_text(
            _drift_memo_md(
                "incomplete",
                drift_candidate="true",
                affects_decision="art://source",
                drift_confidence="medium",
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "drift-missing-required" in r.stderr, f"expected drift-missing-required\n{r.stderr}"
        assert "drift_claim" in r.stderr, f"expected named missing field\n{r.stderr}"
    print("  T32 PASS — drift_candidate: true with missing field fails distinctly")


def test_memo_without_drift_fields_passes_backward_compat():
    """T33 (F1): kind=memo without any drift_* fields must pass — the validator
    gates on `drift_candidate: true`, not on field-presence."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-regular.md").write_text(
            _drift_memo_md("regular"), encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"regular memo (no drift fields) should pass\n{r.stderr}"
    print("  T33 PASS — regular memo with no drift fields passes (F1)")


def test_drift_affects_dangling_fails():
    """T34 (F2): drift_candidate: true with affects_decision pointing at a
    well-formed-but-unresolvable art:// reference fails with
    `drift-affects-dangling`. Without this check, downstream drift detection
    silently targets nonexistent decisions."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-ghost-target.md").write_text(
            _drift_memo_md(
                "ghost-target",
                drift_candidate="true",
                # well-formed UUID-like string, no such artefact exists in vault
                affects_decision="art://00000000-0000-0000-0000-000000000000",
                drift_claim="claim",
                drift_confidence="low",
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "drift-affects-dangling" in r.stderr, f"expected drift-affects-dangling\n{r.stderr}"
    print("  T34 PASS — unresolvable affects_decision fails with distinct error (F2)")


def test_drift_affects_malformed_distinct_from_missing():
    """T35 (F3): drift_candidate: true with affects_decision present but
    not in art://<id> shape (e.g., raw string) fails with a DIFFERENT error
    code from `drift-missing-required`. Operators triaging digest noise need
    to tell 'fix the memo' apart from 'add the field'."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-malformed.md").write_text(
            _drift_memo_md(
                "malformed",
                drift_candidate="true",
                affects_decision="not-an-art-ref",  # missing art:// prefix
                drift_claim="claim",
                drift_confidence="high",
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "drift-affects-malformed" in r.stderr, (
            f"expected drift-affects-malformed (distinct from missing)\n{r.stderr}"
        )
        # F3: when affects_decision is PRESENT but malformed, drift-missing-required
        # must NOT be emitted for that field — operators need distinct codes.
        # (drift-missing-required is reserved for genuine absence.)
        assert "drift-missing-required" not in r.stderr, (
            f"F3 violated: malformed affects_decision must not produce 'missing' code\n{r.stderr}"
        )
    print("  T35 PASS — malformed affects_decision distinct from missing (F3)")


def test_drift_confidence_invalid_value_fails():
    """T36: drift_confidence outside {high, medium, low} fails — guardrails
    in slice 5 filter by confidence, so invalid values must surface early."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-source.md").write_text(
            _drift_memo_md("source"), encoding="utf-8",
        )
        (vault / "artefacts" / "memo" / "art-bad-conf.md").write_text(
            _drift_memo_md(
                "bad-conf",
                drift_candidate="true",
                affects_decision="art://source",
                drift_claim="claim",
                drift_confidence="extreme",  # invalid
            ),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "drift-confidence-invalid" in r.stderr
    print("  T36 PASS — invalid drift_confidence value fails")


def test_drift_candidate_false_no_other_fields_passes():
    """T37 (F1 corner): drift_candidate: false with no other drift fields
    must pass — the flag is the gate. False == off, same as absent."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-flag-off.md").write_text(
            _drift_memo_md("flag-off", drift_candidate="false"),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 0, f"drift_candidate: false should be no-op\n{r.stderr}"
    print("  T37 PASS — drift_candidate: false skips drift validation")


def test_drift_candidate_empty_value_fails_open_protection():
    """T38: `drift_candidate:` (empty value) must fail loudly, not silently
    disable validation. Without this guard the lint is fail-open: a typo or
    half-written flag passes through with all required fields unchecked."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Hand-write the YAML so the empty-value parse path is exercised.
        (vault / "artefacts" / "memo" / "art-empty-flag.md").write_text(
            "---\nid: art-empty-flag\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
            "title: t\ndrift_candidate:\naffects_decision: garbage-not-checked\n"
            "produced_by:\n  session_id: aaaaaaaa\n  query: x\n  model: m\n"
            "  sources_cited:\n    - https://x.test\n---\nbody",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1, f"empty drift_candidate should fail\n{r.stderr}"
        assert "drift-candidate-malformed" in r.stderr, f"expected explicit code\n{r.stderr}"
    print("  T38 PASS — empty drift_candidate value fails (no fail-open)")


def test_drift_candidate_malformed_value_fails():
    """T39: `drift_candidate: maybe` (or any non-bool string) fails with
    `drift-candidate-malformed`. Same fail-open class as T38."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        (vault / "artefacts" / "memo" / "art-typo-flag.md").write_text(
            _drift_memo_md("typo-flag", drift_candidate="maybe"),
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "drift-candidate-malformed" in r.stderr
    print("  T39 PASS — non-bool drift_candidate value fails")


def test_drift_candidate_on_non_memo_fails():
    """T40: drift_candidate: true on `kind: analysis` (or any non-memo)
    fails with `drift-on-non-memo`. Spec scopes drift candidates to memos;
    silently allowing it elsewhere lets validation be bypassed via wrong kind."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Hand-write because _drift_memo_md hardcodes kind: memo.
        (vault / "artefacts" / "analysis" / "art-wrong-kind.md").write_text(
            "---\nid: art-wrong-kind\nkind: analysis\ncreated_at: 2026-06-01T10:00:00Z\n"
            "title: t\ndrift_candidate: true\nproduced_by:\n  session_id: aaaaaaaa\n"
            "  query: x\n  model: m\n  sources_cited:\n    - https://x.test\n---\nbody",
            encoding="utf-8",
        )
        r = run_lint(method)
        assert r.returncode == 1
        assert "drift-on-non-memo" in r.stderr
    print("  T40 PASS — drift_candidate: true on non-memo fails")


if __name__ == "__main__":
    print("Running test_lint_provenance_acceptance.py...")
    test_clean_fixture_exits_0()
    test_post_adr_kb_heading_without_produced_by_fails()
    test_pre_adr_kb_heading_grandfathered()
    test_non_canonical_source_in_kb_fails()
    test_glossary_with_produced_by_fails()
    test_artefact_md_without_frontmatter_fails()
    test_artefact_md_missing_produced_by_keys_fails()
    test_artefact_md_non_canonical_source_fails()
    test_export_without_sidecar_fails()
    test_export_with_valid_sidecar_passes()
    test_require_vault_no_config_exits_2()
    test_method_only_skips_vault()
    test_malformed_session_id_fails()
    test_kb_section_without_date_grandfathered()
    test_well_formed_produced_by_passes()
    test_undated_entry_shape_requires_produced_by()
    test_undated_format_section_grandfathered()
    test_last_verified_qualifies_as_date_marker()
    test_code_fence_examples_dont_trip_lint()
    test_project_scoped_missing_project_id()
    test_project_scoped_mismatched_project_id()
    test_art_canonical_source_accepted()
    test_uuid_collision_across_tiers()
    test_project_scoped_export_sidecar_project_id()
    test_dangling_art_ref_fails()
    test_flat_artefact_resolves_art_ref()
    test_self_reference_passes()
    test_193_art_prefixed_uri_body_tolerated()
    test_flat_resolves_project_uuid()
    test_198_source_pin_kind_accepted_with_upstream()
    test_198_source_pin_refuses_missing_upstream()
    test_198_source_pin_refuses_malformed_upstream()
    test_198_source_pin_in_wrong_directory_fails()
    test_199_kb_heading_literal_text_accepted()
    test_199_kb_heading_degenerate_shapes_refused()
    test_199_kb_regex_refuses_trailing_ws()
    test_malformed_project_slug_fails()
    test_dot_prefixed_dirs_exempt_from_slug_check()
    test_drift_candidate_well_formed_passes()
    test_drift_candidate_missing_required_fails()
    test_memo_without_drift_fields_passes_backward_compat()
    test_drift_affects_dangling_fails()
    test_drift_affects_malformed_distinct_from_missing()
    test_drift_confidence_invalid_value_fails()
    test_drift_candidate_false_no_other_fields_passes()
    test_drift_candidate_empty_value_fails_open_protection()
    test_drift_candidate_malformed_value_fails()
    test_drift_candidate_on_non_memo_fails()
    print("All lint-provenance tests passed.")
