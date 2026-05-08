#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""Acceptance tests for tools/kb-process.py — interactive consumer (#121 / #116).

Tests:
  T1  — list returns 0 rows on empty .unprocessed/
  T2  — list with one memo returns the row + correct kind + referent
  T3  — show prints the memo body
  T4  — apply person candidate appends to people.md with produced_by comment
  T5  — apply uses CURRENT session_id, NOT the routine session from frontmatter (F3)
  T6  — apply rolls back kb write if lint fails (F5: kb⟺processed atomic)
  T7  — apply moves memo to .processed/ on success
  T8  — apply refuses if memo's via=<art-id> already in target kb (F4 idempotency)
  T9  — reject moves memo to .rejected/ without touching kb
  T10 — reject --reason writes sidecar with reason
  T11 — apply on glossary candidate refuses with the editorial-rules message
  T12 — apply on org candidate appends to org.md
  T13 — apply on decision candidate appends to decisions.md
  T14 — list --json emits parseable array
  T15 — apply refuses memo whose diff has lines without `+` prefix (strict).
  T16 — apply refuses + reports clearly when lint-provenance.py is missing.
  T17 — list --count: 0 when empty, exact count when populated, ignores non-art-*.md.
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
KBPROC = PROJ / "tools" / "kb-process.py"


def make_fixture(tmpdir: Path) -> tuple[Path, Path]:
    """Method + vault skeletons. Includes lint-provenance.py so T6 (rollback)
    can fire the lint refusal path."""
    method = tmpdir / "method"
    vault = tmpdir / "vault"
    method.mkdir()
    vault.mkdir()
    (method / "tools").mkdir()
    shutil.copy(PROJ / "tools" / "_config.py", method / "tools" / "_config.py")
    shutil.copy(PROJ / "tools" / "kb-process.py", method / "tools" / "kb-process.py")
    shutil.copy(PROJ / "tools" / "lint-provenance.py", method / "tools" / "lint-provenance.py")
    (method / "kb").mkdir()
    (method / "kb" / "glossary.md").write_text("# Glossary\n", encoding="utf-8")
    (method / ".assistant.local.json").write_text(json.dumps({
        "$schema_version": 1,
        "paths": {"content_root": str(vault.resolve())},
    }), encoding="utf-8")
    # Vault structure
    (vault / "kb").mkdir()
    (vault / "kb" / "people.md").write_text("# People\n", encoding="utf-8")
    (vault / "kb" / "org.md").write_text("# Org\n", encoding="utf-8")
    (vault / "kb" / "decisions.md").write_text("# Decisions\n", encoding="utf-8")
    (vault / "artefacts").mkdir()
    (vault / "artefacts" / "memo").mkdir()
    (vault / "artefacts" / "memo" / ".unprocessed").mkdir()
    return method, vault


def write_candidate_memo(vault: Path, *, art_id: str, kind: str, referent: str,
                        sources: list[str], proposed_diff: str,
                        routine_session: str = "deadbeef") -> Path:
    """Drop a candidate memo into .unprocessed/."""
    fm_lines = [
        "---",
        f"id: {art_id}",
        "kind: memo",
        f"created_at: 2026-05-08T10:00:00Z",
        f"title: 'Candidate {kind}: {referent}'",
        "produced_by:",
        f"  session_id: {routine_session}",
        "  query: kb-scan",
        "  model: claude-opus-4-7",
        "  sources_cited:",
    ]
    for s in sources:
        fm_lines.append(f"    - {s}")
    fm_lines += [
        "---",
        "",
        f"## Candidate {kind}: {referent}",
        "",
        "**Proposed diff:**",
        "",
        proposed_diff,
    ]
    path = vault / "artefacts" / "memo" / ".unprocessed" / f"{art_id}.md"
    path.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")
    return path


def run_proc(method: Path, *args: str, expect_rc: int | None = 0,
             env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        [str(method / "tools" / "kb-process.py"), *args],
        capture_output=True, text=True, env=env,
    )
    if expect_rc is not None:
        assert r.returncode == expect_rc, (
            f"unexpected rc={r.returncode} (wanted {expect_rc})\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )
    return r


def make_person_diff() -> str:
    return (
        "```diff\n"
        "+ ## Brendan Strum\n"
        "+ - **Role / relation:** Engineer at Hub International\n"
        "+ - **Last verified:** 2026-05-08\n"
        "+ - **Source:** scan-driven candidate\n"
        "+ \n"
        "+ Brendan appears in 2 source memories about partnership discussions.\n"
        "```"
    )


def test_list_empty():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        r = run_proc(method, "list")
        assert "no unprocessed memos" in r.stdout
    print("  T1 PASS — list returns 0 rows on empty")


def test_list_with_memo():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-test1", kind="person", referent="Jane Doe",
            sources=["mem://m1", "mem://m2"], proposed_diff=make_person_diff(),
        )
        r = run_proc(method, "list")
        assert "art-test1" in r.stdout
        assert "[person]" in r.stdout
        assert "Jane Doe" in r.stdout
    print("  T2 PASS — list shows kind + referent")


def test_show():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-test1", kind="person", referent="Jane Doe",
            sources=["mem://m1", "mem://m2"], proposed_diff=make_person_diff(),
        )
        r = run_proc(method, "show", "art-test1")
        assert "Candidate person: Jane Doe" in r.stdout
        assert "Brendan Strum" in r.stdout  # from the diff body
    print("  T3 PASS — show prints memo body")


def test_apply_person_appends_with_provenance():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-bs1", kind="person", referent="Brendan Strum",
            sources=["mem://m1", "mem://m2"], proposed_diff=make_person_diff(),
        )
        r = run_proc(method, "apply", "art-bs1",
                     env_extra={"PA_SESSION_ID": "abcd1234"})
        people_text = (vault / "kb" / "people.md").read_text(encoding="utf-8")
        assert "## Brendan Strum" in people_text
        # produced_by comment with the CURRENT session id
        assert "session=abcd1234" in people_text
        # mem:// sources from the routine session preserved
        assert "mem://m1" in people_text
        # via=<art-id> for idempotency tracking
        assert "via=art-bs1" in people_text
    print("  T4 PASS — apply appends person + produced_by")


def test_apply_uses_current_not_routine_session():
    """F3 closer: routine session deadbeef must NOT leak into the kb file."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-bs2", kind="person", referent="Brendan Strum",
            sources=["mem://m1", "mem://m2"],
            proposed_diff=make_person_diff(),
            routine_session="deadbeef",  # this is the ROUTINE session
        )
        r = run_proc(method, "apply", "art-bs2",
                     env_extra={"PA_SESSION_ID": "abcd1234"})
        people_text = (vault / "kb" / "people.md").read_text(encoding="utf-8")
        assert "session=abcd1234" in people_text  # current
        # The routine session id must NOT appear in the kb file
        # (it's in the memo's frontmatter, but kb-process must NOT propagate it).
        assert "session=deadbeef" not in people_text
    print("  T5 PASS — apply uses current session, not routine (F3)")


def test_apply_rolls_back_on_lint_failure():
    """F5 closer: if lint refuses post-write, rollback so kb⟺processed atomic."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Inject a memo whose proposed diff has a non-canonical source form
        # (a path-based artefact reference) — that's NOT what would normally
        # land but tests that lint failure rolls back.
        # Easier path: write an entry with no produced_by comment by patching
        # the diff to lack the dated header — simulate a lint failure differently.
        #
        # Actually the lint runs against the ENTIRE vault, not just our diff.
        # Easiest forced-failure: pre-corrupt people.md so any append fails the lint.
        # We do that by making people.md contain an entry-shape body without
        # produced_by — that triggers `kb-missing-produced-by` from
        # the entry-shape rule (T16 in lint-provenance tests).
        (vault / "kb" / "people.md").write_text(
            "# People\n\n## Pre-existing Bad\n"
            "- **Role / relation:** Engineer\n"
            "- **Source:** manual\n\nbody.\n",
            encoding="utf-8",
        )
        original_text = (vault / "kb" / "people.md").read_text(encoding="utf-8")
        write_candidate_memo(
            vault, art_id="art-rollback", kind="person", referent="Test Person",
            sources=["mem://m1"], proposed_diff=make_person_diff(),
        )
        r = run_proc(method, "apply", "art-rollback",
                     env_extra={"PA_SESSION_ID": "abcd1234"},
                     expect_rc=1)
        assert "rolled back" in r.stderr.lower()
        # Rollback: people.md must equal the pre-write state.
        assert (vault / "kb" / "people.md").read_text(encoding="utf-8") == original_text
        # Memo must still be in .unprocessed/
        assert (vault / "artefacts" / "memo" / ".unprocessed" / "art-rollback.md").is_file()
        # Memo must NOT be in .processed/
        processed = vault / "artefacts" / "memo" / ".processed"
        if processed.is_dir():
            assert not (processed / "art-rollback.md").is_file()
    print("  T6 PASS — apply rolls back on lint failure (F5)")


def test_apply_moves_memo_to_processed():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-bs3", kind="person", referent="Jane Smith",
            sources=["mem://m1", "mem://m2"], proposed_diff=make_person_diff(),
        )
        r = run_proc(method, "apply", "art-bs3",
                     env_extra={"PA_SESSION_ID": "abcd1234"})
        assert not (vault / "artefacts" / "memo" / ".unprocessed" / "art-bs3.md").is_file()
        assert (vault / "artefacts" / "memo" / ".processed" / "art-bs3.md").is_file()
    print("  T7 PASS — apply archives memo to .processed/")


def test_apply_idempotent():
    """F4 closer: second apply on the same memo refuses (via=<id> already present)."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-dup", kind="person", referent="Jane Doe",
            sources=["mem://m1", "mem://m2"], proposed_diff=make_person_diff(),
        )
        r1 = run_proc(method, "apply", "art-dup",
                      env_extra={"PA_SESSION_ID": "abcd1234"})
        # Re-create the memo in .unprocessed/ to simulate a retry
        write_candidate_memo(
            vault, art_id="art-dup", kind="person", referent="Jane Doe",
            sources=["mem://m1", "mem://m2"], proposed_diff=make_person_diff(),
        )
        r2 = run_proc(method, "apply", "art-dup",
                      env_extra={"PA_SESSION_ID": "abcd1234"},
                      expect_rc=1)
        assert "already applied" in r2.stderr
        people_text = (vault / "kb" / "people.md").read_text(encoding="utf-8")
        # Only ONE entry, not two
        assert people_text.count("via=art-dup") == 1
    print("  T8 PASS — apply refuses duplicate (F4)")


def test_reject():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-rej", kind="person", referent="Test",
            sources=["mem://m1", "mem://m2"], proposed_diff=make_person_diff(),
        )
        r = run_proc(method, "reject", "art-rej")
        assert (vault / "artefacts" / "memo" / ".rejected" / "art-rej.md").is_file()
        # KB untouched
        people_text = (vault / "kb" / "people.md").read_text(encoding="utf-8")
        assert "Test" not in people_text or "Test" not in people_text.split("\n")[1]
    print("  T9 PASS — reject moves to .rejected/, kb untouched")


def test_reject_with_reason():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-rej2", kind="person", referent="Test",
            sources=["mem://m1", "mem://m2"], proposed_diff=make_person_diff(),
        )
        r = run_proc(method, "reject", "art-rej2", "--reason", "duplicate of art-bs1")
        reason_path = vault / "artefacts" / "memo" / ".rejected" / "art-rej2.reason.txt"
        assert reason_path.is_file()
        assert "duplicate of art-bs1" in reason_path.read_text(encoding="utf-8")
    print("  T10 PASS — reject --reason writes sidecar")


def test_apply_glossary_refuses():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-gloss", kind="glossary", referent="memory object",
            sources=["mem://m1"], proposed_diff="```diff\n+ ## memory object\n+ - definition\n```",
        )
        r = run_proc(method, "apply", "art-gloss",
                     env_extra={"PA_SESSION_ID": "abcd1234"},
                     expect_rc=1)
        assert "PR-only provenance" in r.stderr
        # Memo stays in .unprocessed/
        assert (vault / "artefacts" / "memo" / ".unprocessed" / "art-gloss.md").is_file()
    print("  T11 PASS — apply refuses glossary candidates (PR-only)")


def test_apply_org():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-org1", kind="org", referent="Acme Corp",
            sources=["mem://m1", "mem://m2"],
            proposed_diff=(
                "```diff\n"
                "+ ## Acme Corp\n"
                "+ - **Relation to user:** customer\n"
                "+ - **Last verified:** 2026-05-08\n"
                "+ - **Source:** scan-driven candidate\n"
                "+ \n"
                "+ Acme appears in 2 source memories.\n"
                "```"
            ),
        )
        r = run_proc(method, "apply", "art-org1",
                     env_extra={"PA_SESSION_ID": "abcd1234"})
        org_text = (vault / "kb" / "org.md").read_text(encoding="utf-8")
        assert "## Acme Corp" in org_text
        assert "via=art-org1" in org_text
    print("  T12 PASS — apply on org → org.md")


def test_apply_decision():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-dec1", kind="decision", referent="Use Plan B for migration",
            sources=["mem://m1"],
            proposed_diff=(
                "```diff\n"
                "+ ## Use Plan B for migration\n"
                "+ - **Date:** 2026-05-08\n"
                "+ - **Status:** decided\n"
                "+ - **Source:** mem://m1\n"
                "+ \n"
                "+ The team decided in the Q2 review to go with Plan B.\n"
                "```"
            ),
        )
        r = run_proc(method, "apply", "art-dec1",
                     env_extra={"PA_SESSION_ID": "abcd1234"})
        dec_text = (vault / "kb" / "decisions.md").read_text(encoding="utf-8")
        assert "## Use Plan B for migration" in dec_text
        assert "via=art-dec1" in dec_text
    print("  T13 PASS — apply on decision → decisions.md")


def test_list_json():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_candidate_memo(
            vault, art_id="art-j1", kind="org", referent="Acme",
            sources=["mem://m1", "mem://m2"],
            proposed_diff="```diff\n+ ## Acme\n+ - test\n```",
        )
        r = run_proc(method, "list", "--json")
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["art_id"] == "art-j1"
        assert data[0]["kind"] == "org"
        assert data[0]["referent"] == "Acme"
    print("  T14 PASS — list --json emits parseable array")


def test_apply_strict_diff_format():
    """T15: a diff with non-blank lines lacking `+` prefix is refused."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        bad_diff = (
            "```diff\n"
            "+ ## SomePerson\n"
            "but this line has no plus prefix\n"  # the producer-bug
            "+ - **Source:** test\n"
            "```"
        )
        write_candidate_memo(
            vault, art_id="art-baddiff", kind="person", referent="SomePerson",
            sources=["mem://m1", "mem://m2"], proposed_diff=bad_diff,
        )
        r = run_proc(method, "apply", "art-baddiff",
                     env_extra={"PA_SESSION_ID": "abcd1234"},
                     expect_rc=1)
        assert "lacks `+` prefix" in r.stderr
        # Memo stays in .unprocessed/
        assert (vault / "artefacts" / "memo" / ".unprocessed" / "art-baddiff.md").is_file()
    print("  T15 PASS — strict diff format refuses non-conforming memos")


def test_apply_hard_fails_on_missing_lint():
    """T16: a configured vault with missing lint-provenance.py hard-fails
    (no soft-pass); the F5 gate depends on the lint actually running."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Remove lint-provenance.py from the test method tree.
        (method / "tools" / "lint-provenance.py").unlink()
        write_candidate_memo(
            vault, art_id="art-nolint", kind="person", referent="Test Person",
            sources=["mem://m1", "mem://m2"], proposed_diff=make_person_diff(),
        )
        r = run_proc(method, "apply", "art-nolint",
                     env_extra={"PA_SESSION_ID": "abcd1234"},
                     expect_rc=1)
        assert "lint-provenance" in r.stderr
        assert "refusing to proceed" in r.stderr or "rolled back" in r.stderr.lower()
        # Memo stays in .unprocessed/
        assert (vault / "artefacts" / "memo" / ".unprocessed" / "art-nolint.md").is_file()
    print("  T16 PASS — apply hard-fails on missing lint")


def test_list_count():
    """T17: list --count prints just the integer; ignores non-`art-*.md` files."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Empty
        r = run_proc(method, "list", "--count")
        assert r.stdout.strip() == "0"
        # One memo
        write_candidate_memo(
            vault, art_id="art-c1", kind="org", referent="Acme",
            sources=["mem://m1", "mem://m2"],
            proposed_diff="```diff\n+ ## Acme\n+ - test\n```",
        )
        r = run_proc(method, "list", "--count")
        assert r.stdout.strip() == "1"
        # Plus a sidecar / unrelated file in the same dir — must NOT inflate.
        unprocessed = vault / "artefacts" / "memo" / ".unprocessed"
        (unprocessed / "art-c1.provenance.json").write_text("{}", encoding="utf-8")
        (unprocessed / "notes.txt").write_text("hello", encoding="utf-8")
        (unprocessed / "art-broken.md.bak").write_text("---\n", encoding="utf-8")
        r = run_proc(method, "list", "--count")
        assert r.stdout.strip() == "1", f"sidecars/unrelated should be ignored: stdout={r.stdout!r}"
    print("  T17 PASS — list --count ignores non-art-*.md")


if __name__ == "__main__":
    print("Running test_kb_process_acceptance.py...")
    test_list_empty()
    test_list_with_memo()
    test_show()
    test_apply_person_appends_with_provenance()
    test_apply_uses_current_not_routine_session()
    test_apply_rolls_back_on_lint_failure()
    test_apply_moves_memo_to_processed()
    test_apply_idempotent()
    test_reject()
    test_reject_with_reason()
    test_apply_glossary_refuses()
    test_apply_org()
    test_apply_decision()
    test_list_json()
    test_apply_strict_diff_format()
    test_apply_hard_fails_on_missing_lint()
    test_list_count()
    print("All kb-process tests passed.")
