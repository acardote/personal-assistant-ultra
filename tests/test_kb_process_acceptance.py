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
  T18 — list shows [DRIFT] tag for drift candidates instead of `Candidate <kind>: ...`.
  T19 — apply on a drift candidate refuses with "use drift-apply" message.
  T20 — drift-apply happy path: amendment lands as `### <date> — ...` under the
        affected decision; original body preserved; lint clean; memo archived.
  T21 — drift-apply uses CURRENT session_id, not the routine session that emitted
        the drift memo (F4 closer).
  T22 — drift-apply refuses when the via-uuid no longer resolves in kb/decisions.md
        (F5: stale reference at apply time).
  T23 — drift-apply is idempotent: replay on the same memo refuses (F3).
  T24 — drift-dismiss archives the memo + writes the per-decision dismissal entry.
  T25 — drift-dismiss --reason writes the sidecar reason file.
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


def write_drift_memo(
    vault: Path, *, art_id: str, via_uuid: str,
    drift_claim: str = "Decision X says Y, but recent memory N indicates Z.",
    drift_confidence: str = "high",
    memory_id: str = "mem-zz",
    routine_session: str = "deadbeef",
) -> Path:
    """Drop a slice-1 schema drift candidate memo into .unprocessed/."""
    fm_lines = [
        "---",
        f"id: {art_id}",
        "kind: memo",
        "created_at: 2026-05-08T10:00:00Z",
        "title: 'Drift: test decision'",
        "drift_candidate: true",
        f"affects_decision: art://{via_uuid}",
        f"drift_claim: \"{drift_claim}\"",
        f"drift_confidence: {drift_confidence}",
        "produced_by:",
        f"  session_id: {routine_session}",
        "  query: kb-drift-scan",
        "  model: claude-opus-4-7",
        "  sources_cited:",
        f"    - mem://{memory_id}",
        f"    - art://{via_uuid}",
        "---",
        "",
        "## Drift candidate",
        "",
        f"{drift_claim}",
    ]
    path = vault / "artefacts" / "memo" / ".unprocessed" / f"{art_id}.md"
    path.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")
    return path


def seed_decision(
    vault: Path, *, title: str, scope: str, via_uuid: str,
    body: str = "Decision body text.",
    routine_session: str = "11111111",
) -> None:
    """Append a kb-scan-shaped decision section to kb/decisions.md (so the
    drift-walk apply path has a target to amend).

    Source memory's art-* file is also placed under artefacts/memo/ so the
    slice-1 lint's `affects_decision` resolution succeeds when drift-apply
    triggers lint-provenance --require-vault."""
    section = (
        f"\n## {title}\n"
        f"<!-- produced_by: session={routine_session}, query=\"x\", "
        f"at=2026-05-08T10:00:00Z, sources=[mem://test], via=art-{via_uuid} -->\n"
        f"- **Date:** 2026-05-08\n"
        f"- **Status:** decided\n"
        f"- **Last verified:** 2026-05-08\n"
        f"- **Expires:** never\n"
        f"- **Source:** mem://test\n"
        f"- **Scope:** {scope}\n\n"
        f"{body}\n"
    )
    path = vault / "kb" / "decisions.md"
    cur = path.read_text(encoding="utf-8") if path.is_file() else "# Decisions\n"
    path.write_text(cur + section, encoding="utf-8")
    # Place the source artefact so slice-1 lint's affects_decision resolves.
    art_target = vault / "artefacts" / "memo" / f"art-{via_uuid}.md"
    art_target.write_text(
        "---\nid: art-" + via_uuid + "\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
        "title: source\nproduced_by:\n  session_id: aaaaaaaa\n  query: x\n  model: m\n"
        "  sources_cited:\n    - https://x.test\n---\nbody",
        encoding="utf-8",
    )


def test_list_marks_drift_candidates():
    """T18: list output uses `[DRIFT]` tag for drift candidates so reviewers
    can group them at-a-glance."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_drift_memo(vault, art_id="art-d001",
                         via_uuid="aaaaaaaa-1111-2222-3333-444444444444")
        write_candidate_memo(
            vault, art_id="art-c1", kind="org", referent="Acme",
            sources=["mem://m1", "mem://m2"],
            proposed_diff="```diff\n+ ## Acme\n+ - test\n```",
        )
        r = run_proc(method, "list")
        assert "[DRIFT]" in r.stdout, r.stdout
        assert "[org]" in r.stdout, r.stdout
    print("  T18 PASS — list shows [DRIFT] tag for drift candidates")


def test_apply_refuses_drift_candidate():
    """T19: regular `apply` on a drift candidate must refuse and direct to
    `drift-apply`. Otherwise it would try to extract a ```diff block (which
    drift memos don't have) and emit a misleading error."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_drift_memo(vault, art_id="art-d002",
                         via_uuid="aaaaaaaa-1111-2222-3333-444444444444")
        r = run_proc(method, "apply", "art-d002", expect_rc=1)
        assert "drift candidate" in r.stderr
        assert "drift-apply" in r.stderr
        # And the kb file is untouched.
        assert (vault / "kb" / "people.md").read_text(encoding="utf-8") == "# People\n"
    print("  T19 PASS — apply refuses drift candidate")


def test_drift_apply_happy_path():
    """T20: drift-apply lands an amendment under the affected decision,
    preserves the original decision body, lint passes, memo archived."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        via_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        seed_decision(vault, title="Polestar in H2", scope="Polestar", via_uuid=via_uuid,
                      body="Polestar is in scope for the H2 customer plan.")
        write_drift_memo(
            vault, art_id="art-d003", via_uuid=via_uuid,
            drift_claim="Drop Polestar from H2; Acko replaces them.",
            drift_confidence="high",
            memory_id="mem-zz",
        )
        # Run with a fixed PA_SESSION_ID so we can verify the produced_by
        # comment carries the interactive session, not the routine one.
        r = run_proc(method, "drift-apply", "art-d003",
                     env_extra={"PA_SESSION_ID": "abcd1234"})
        kb_text = (vault / "kb" / "decisions.md").read_text(encoding="utf-8")
        # Original body preserved.
        assert "Polestar is in scope for the H2 customer plan." in kb_text
        # Amendment landed.
        assert "### " in kb_text and "drift amendment" in kb_text
        assert "Drop Polestar from H2" in kb_text
        # Memo archived.
        assert (vault / "artefacts" / "memo" / ".processed" / "art-d003.md").is_file()
        assert not (vault / "artefacts" / "memo" / ".unprocessed" / "art-d003.md").is_file()
    print("  T20 PASS — drift-apply happy path lands amendment + archives memo")


def test_drift_apply_uses_interactive_session():
    """T21 (F4 closer): the amendment's produced_by comment carries the
    CURRENT (interactive) PA_SESSION_ID, not the routine session that
    emitted the drift memo. Mirrors the F3 closer on slice 3 of #116."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        via_uuid = "bbbbbbbb-1111-2222-3333-444444444444"
        seed_decision(vault, title="Atlas leadership", scope="Atlas", via_uuid=via_uuid)
        write_drift_memo(vault, art_id="art-d004", via_uuid=via_uuid,
                         routine_session="deadbeef")
        r = run_proc(method, "drift-apply", "art-d004",
                     env_extra={"PA_SESSION_ID": "abcd1234"})
        kb_text = (vault / "kb" / "decisions.md").read_text(encoding="utf-8")
        # The amendment's produced_by carries `session=abcd1234`, NOT `session=deadbeef`.
        # Find the new `### ` block's produced_by line.
        idx = kb_text.find("### ")
        assert idx >= 0
        amend_block = kb_text[idx:]
        assert "session=abcd1234" in amend_block, amend_block[:500]
        assert "session=deadbeef" not in amend_block, amend_block[:500]
    print("  T21 PASS — drift-apply uses interactive session_id (F4)")


def test_drift_apply_refuses_stale_reference():
    """T22 (F5): drift-apply refuses to write when the via-uuid no longer
    resolves to any decision. The user could have renamed/deleted the
    decision between drift-scan emission and drift-apply."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # NO seed_decision call — the via-uuid doesn't exist in decisions.md.
        write_drift_memo(vault, art_id="art-d005",
                         via_uuid="cccccccc-1111-2222-3333-444444444444")
        r = run_proc(method, "drift-apply", "art-d005", expect_rc=1)
        assert "no longer resolves" in r.stderr or "doesn't resolve" in r.stderr or "does not resolve" in r.stderr, r.stderr
        # kb untouched.
        kb_text = (vault / "kb" / "decisions.md").read_text(encoding="utf-8")
        assert "###" not in kb_text
        # Memo still in .unprocessed/.
        assert (vault / "artefacts" / "memo" / ".unprocessed" / "art-d005.md").is_file()
    print("  T22 PASS — drift-apply refuses stale via-uuid (F5)")


def test_drift_apply_idempotent():
    """T23 (F3): running drift-apply twice on the same memo refuses the
    second time. Crash-recover replay must not duplicate amendments."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        via_uuid = "dddddddd-1111-2222-3333-444444444444"
        seed_decision(vault, title="Beta launch", scope="Beta", via_uuid=via_uuid)
        write_drift_memo(vault, art_id="art-d006", via_uuid=via_uuid)
        # First apply succeeds.
        r1 = run_proc(method, "drift-apply", "art-d006",
                      env_extra={"PA_SESSION_ID": "abcd1234"})
        # Manually move the memo back to .unprocessed/ to simulate a
        # crash that left the kb-write done but the archive incomplete.
        processed = vault / "artefacts" / "memo" / ".processed" / "art-d006.md"
        unprocessed = vault / "artefacts" / "memo" / ".unprocessed" / "art-d006.md"
        processed.replace(unprocessed)
        # Second apply must refuse — the via=art-d006 marker is in kb already.
        r2 = run_proc(method, "drift-apply", "art-d006", expect_rc=1)
        assert "already applied" in r2.stderr.lower() or "duplicate" in r2.stderr.lower(), r2.stderr
        # Verify the kb file has exactly ONE amendment, not two.
        kb_text = (vault / "kb" / "decisions.md").read_text(encoding="utf-8")
        assert kb_text.count("### ") == 1, f"expected exactly 1 amendment, got {kb_text.count('### ')}\n{kb_text}"
    print("  T23 PASS — drift-apply is idempotent on replay (F3)")


def test_drift_dismiss_records_dismissal_entry():
    """T24: drift-dismiss archives the memo to .rejected/ AND records a
    per-decision dismissal entry under .harvest/drift-dismissals/<via>.json
    so slice 4's suppression mechanism has the count to read."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        via_uuid = "eeeeeeee-1111-2222-3333-444444444444"
        write_drift_memo(vault, art_id="art-d007", via_uuid=via_uuid)
        r = run_proc(method, "drift-dismiss", "art-d007",
                     "--reason", "false positive — already resolved")
        assert (vault / "artefacts" / "memo" / ".rejected" / "art-d007.md").is_file()
        # Reason sidecar
        assert (vault / "artefacts" / "memo" / ".rejected" / "art-d007.reason.txt").is_file()
        # Dismissal entry under the per-decision JSON
        dismissals_path = vault / ".harvest" / "drift-dismissals" / f"{via_uuid}.json"
        assert dismissals_path.is_file()
        data = json.loads(dismissals_path.read_text(encoding="utf-8"))
        assert data["via_uuid"] == via_uuid
        assert len(data["dismissals"]) == 1
        assert data["dismissals"][0]["art_id"] == "art-d007"
        assert "false positive" in data["dismissals"][0]["reason"]
    print("  T24 PASS — drift-dismiss archives + records dismissal entry")


def test_drift_dismiss_no_double_count_on_replay():
    """T25: dismissing the SAME art_id twice (e.g., manual error) doesn't
    double-count the entry under <via>.json — the suppression threshold
    in slice 4 must measure unique dismissals, not retry attempts."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        via_uuid = "ffffffff-1111-2222-3333-444444444444"
        write_drift_memo(vault, art_id="art-d008", via_uuid=via_uuid)
        run_proc(method, "drift-dismiss", "art-d008", "--reason", "first")
        # Manually move it back to simulate accidental replay.
        rejected = vault / "artefacts" / "memo" / ".rejected" / "art-d008.md"
        unprocessed = vault / "artefacts" / "memo" / ".unprocessed" / "art-d008.md"
        rejected.replace(unprocessed)
        run_proc(method, "drift-dismiss", "art-d008", "--reason", "second")
        dismissals_path = vault / ".harvest" / "drift-dismissals" / f"{via_uuid}.json"
        data = json.loads(dismissals_path.read_text(encoding="utf-8"))
        assert len(data["dismissals"]) == 1, f"expected 1, got {len(data['dismissals'])}"
    print("  T25 PASS — drift-dismiss doesn't double-count replay")


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
    test_list_marks_drift_candidates()
    test_apply_refuses_drift_candidate()
    test_drift_apply_happy_path()
    test_drift_apply_uses_interactive_session()
    test_drift_apply_refuses_stale_reference()
    test_drift_apply_idempotent()
    test_drift_dismiss_records_dismissal_entry()
    test_drift_dismiss_no_double_count_on_replay()
    print("All kb-process tests passed.")
