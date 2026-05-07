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
    print("All lint-provenance tests passed.")
