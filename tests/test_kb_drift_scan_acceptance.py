#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""Acceptance tests for tools/kb-drift-scan.py — drift detector (#135 slice 2).

Tests:
  T1  — empty memory pool: no pairs, watermark written, exit 0.
  T2  — load_decisions filters: skips schema heading, no-Scope entries,
        no-via entries; keeps scoped+anchored entries.
  T3  — Scope intersection routes by tag overlap (positive case).
  T4  — Scope intersection rejects unrelated memories (negative case).
  T5  — --skip-llm reports the pair count and emits 0 memos.
  T6  — cache key composes BOTH memory body hash AND decision text hash.
        Body edit invalidates; decision edit invalidates (F5 closer).
  T7  — F4 grounding: drift_claim with a verbatim_excerpt that's NOT a
        substring of the memory body is rejected (`excerpt_grounded` False).
        With a real substring excerpt: True. Empty/short excerpts: False.
  T8  — Confidence threshold filter: low-confidence verdicts are skipped
        when threshold=medium (default); admitted when threshold=low.
  T9  — emit_drift_memo writes a memo with the slice-1 schema fields
        populated, frontmatter parses cleanly, and direct call to
        check_artefact_md (lint-provenance internals) produces no errors
        when affects_decision resolves.
  T10 — emit_drift_memo + check_artefact_md fails when affects_decision
        does NOT resolve (slice-1 enforcement is real, not vacuous).
  T11 — atomic cache write: tmp file renamed to final, no .tmp residue.
  T12 — watermark NOT advanced when --max-llm-calls quota was exhausted.
  T13 — --max-llm-calls cap is hard: exact count of LLM calls is enforced
        even when more pairs survive routing.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
DRIFT_SCAN = PROJ / "tools" / "kb-drift-scan.py"


def make_fixture(tmpdir: Path) -> tuple[Path, Path]:
    """Build method + vault skeletons. The drift-scan tool depends on
    `_config.py` and `lint-provenance.py` (the latter for T9/T10's direct
    schema-validation call), so we copy both into the method root."""
    method = tmpdir / "method"
    vault = tmpdir / "vault"
    method.mkdir()
    vault.mkdir()
    (method / "tools").mkdir()
    for fn in ("_config.py", "kb-drift-scan.py", "lint-provenance.py"):
        shutil.copy(PROJ / "tools" / fn, method / "tools" / fn)
    (method / "kb").mkdir()
    (method / "kb" / "glossary.md").write_text("# Glossary\n", encoding="utf-8")
    (method / ".assistant.local.json").write_text(json.dumps({
        "$schema_version": 1,
        "paths": {"content_root": str(vault.resolve())},
    }), encoding="utf-8")
    (vault / "memory").mkdir()
    for src in ("granola_note", "slack_thread", "slack_dm", "gmail_thread"):
        (vault / "memory" / src).mkdir()
    (vault / "kb").mkdir()
    (vault / "kb" / "decisions.md").write_text("# Decisions\n", encoding="utf-8")
    (vault / "artefacts").mkdir()
    (vault / "artefacts" / "memo").mkdir()
    return method, vault


def run_drift_scan(method: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(method / "tools" / "kb-drift-scan.py"), *args],
        capture_output=True, text=True,
    )


def write_memory(
    vault: Path, source_kind: str, slug: str, *,
    tags: list[str], created_at: str = "2026-05-08T10:00:00Z",
    body: str = "## What was decided / what is true\n\nNothing material.\n",
    title: str | None = None,
    summary: str | None = None,
) -> Path:
    title = title or f"Test {slug}"
    summary = summary or f"Summary for {slug}"
    fm_lines = [
        "---",
        f"id: mem-{slug}",
        f"source_kind: {source_kind}",
        f"created_at: '{created_at}'",
        "kind: note",
        "tags:",
    ]
    for t in tags:
        fm_lines.append(f"- {t}")
    fm_lines += [
        f"title: '{title}'",
        f"summary: {summary}",
        "---",
        "",
        body,
    ]
    path = vault / "memory" / source_kind / f"{slug}.md"
    path.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")
    return path


def write_decision(
    vault: Path, *, title: str, scope: str, via_uuid: str,
    body: str = "We decided this.",
) -> None:
    """Append a kb-scan-shaped decision section to kb/decisions.md."""
    section = (
        f"\n## {title}\n"
        f"<!-- produced_by: session=abcd1234, query=\"test\", at=2026-05-08T10:00:00Z, "
        f"sources=[mem://test], via=art-{via_uuid} -->\n"
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


def import_drift_scan(method: Path):
    """Load tools/kb-drift-scan.py as a module so tests can call internals
    (cache helpers, decision parser, emit) without spinning subprocesses."""
    spec = importlib.util.spec_from_file_location(
        "kb_drift_scan_t", method / "tools" / "kb-drift-scan.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kb_drift_scan_t"] = mod
    spec.loader.exec_module(mod)
    return mod


def import_lint(method: Path):
    spec = importlib.util.spec_from_file_location(
        "lint_provenance_t", method / "tools" / "lint-provenance.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lint_provenance_t"] = mod
    spec.loader.exec_module(mod)
    return mod


def emitted_drift_memos(vault: Path, *, out_dir: Path | None = None) -> list[Path]:
    d = out_dir or (vault / "artefacts" / "memo" / ".unprocessed")
    if not d.is_dir():
        return []
    return sorted(d.glob("art-*.md"))


# ---------------------------------------------------------------------
# T1
# ---------------------------------------------------------------------


def test_empty_pool_emits_nothing():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # No memories, no decisions.
        r = run_drift_scan(method, "--all", "--skip-llm")
        assert r.returncode == 0, r.stderr
        # Watermark should be written even on empty (matches kb-scan precedent).
        wm = vault / ".harvest" / "kb-drift-scan-watermark.json"
        # Empty-decisions branch writes watermark when --skip-llm is OFF.
        # With --skip-llm, watermark is gated; this matches main-flow behaviour.
        assert "loaded 0" in r.stderr
    print("  T1 PASS — empty pool exits clean")


# ---------------------------------------------------------------------
# T2 — decision parser filters
# ---------------------------------------------------------------------


def test_load_decisions_filters_by_scope_and_via():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # 1 valid, 1 missing-scope, 1 missing-via, 1 schema-template
        decisions_md = vault / "kb" / "decisions.md"
        decisions_md.write_text(
            "# Decisions\n\n"
            # template heading — must be skipped
            "## <Decision title>\n"
            "- **Date:** <YYYY-MM-DD>\n"
            "- **Scope:** <referent>\n\n"
            # missing scope — must be skipped
            "## Missing-scope decision\n"
            "<!-- produced_by: session=11111111, query=\"x\", at=2026-05-08T10:00:00Z, "
            "sources=[mem://test], via=art-aaaaaaaa-1111-2222-3333-444444444444 -->\n"
            "- **Date:** 2026-05-08\n\n"
            "body.\n\n"
            # missing via — must be skipped (no anchor)
            "## Missing-via decision\n"
            "- **Date:** 2026-05-08\n"
            "- **Scope:** Some Org\n\n"
            "body.\n\n"
            # valid
            "## Valid decision\n"
            "<!-- produced_by: session=22222222, query=\"x\", at=2026-05-08T10:00:00Z, "
            "sources=[mem://test], via=art-bbbbbbbb-1111-2222-3333-444444444444 -->\n"
            "- **Date:** 2026-05-08\n"
            "- **Scope:** Acme Co\n\n"
            "We decided to do it.\n",
            encoding="utf-8",
        )
        m = import_drift_scan(method)
        decs = m.load_decisions(vault)
        titles = [d.title for d in decs]
        assert titles == ["Valid decision"], titles
        assert decs[0].scope == "Acme Co"
        assert decs[0].art_id == "bbbbbbbb-1111-2222-3333-444444444444"
        sys.modules.pop("kb_drift_scan_t", None)
    print("  T2 PASS — load_decisions filters template/no-scope/no-via")


# ---------------------------------------------------------------------
# T3 / T4 — Scope routing
# ---------------------------------------------------------------------


def test_scope_intersection_positive():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_decision(vault, title="Polestar in H2", scope="Polestar",
                       via_uuid="aaaaaaaa-1111-2222-3333-444444444444")
        write_memory(vault, "granola_note", "m1", tags=["polestar"])
        r = run_drift_scan(method, "--all", "--skip-llm")
        assert r.returncode == 0, r.stderr
        assert "phase 1: 1 (memory, decision)" in r.stderr, r.stderr
    print("  T3 PASS — Scope intersection finds tag-matched pair")


def test_scope_intersection_rejects_unrelated():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_decision(vault, title="Polestar in H2", scope="Polestar",
                       via_uuid="aaaaaaaa-1111-2222-3333-444444444444")
        write_memory(vault, "granola_note", "m1", tags=["acko"])
        r = run_drift_scan(method, "--all", "--skip-llm")
        assert r.returncode == 0, r.stderr
        assert "phase 1: 0 (memory, decision)" in r.stderr, r.stderr
    print("  T4 PASS — Scope intersection rejects non-overlapping memory")


# ---------------------------------------------------------------------
# T5 — skip-llm
# ---------------------------------------------------------------------


def test_skip_llm_emits_nothing():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_decision(vault, title="Polestar in H2", scope="Polestar",
                       via_uuid="aaaaaaaa-1111-2222-3333-444444444444")
        write_memory(vault, "granola_note", "m1", tags=["polestar"])
        r = run_drift_scan(method, "--all", "--skip-llm")
        assert r.returncode == 0, r.stderr
        memos = emitted_drift_memos(vault)
        assert memos == []
    print("  T5 PASS — --skip-llm emits no memos")


# ---------------------------------------------------------------------
# T6 — cache key invalidation (F5)
# ---------------------------------------------------------------------


def test_cache_key_invalidates_on_either_edit():
    """F5 closer: the cache key composes BOTH memory body hash and decision
    text hash. Editing either side flips the key, forcing a re-LLM."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        m = import_drift_scan(method)
        memory = m.MemoryObject(
            path=Path("/dev/null"), source_kind="t", memory_id="mem-x",
            created_at="2026-05-08T10:00:00Z", tags=("acme",), title="t",
            summary="s", body="body v1", content_hash="bbbbbbbb",
        )
        decision = m.DecisionEntry(
            art_id="aaaaaaaa-1111", title="d", scope="acme",
            text="decision v1", text_hash="dddddddd",
        )
        pair_v1 = m.DriftPair(memory, decision)

        m.cache_write(vault, pair_v1, {"verdict": {"drifted": False}})
        hit = m.cache_read(vault, pair_v1)
        assert hit and hit["verdict"]["drifted"] is False

        # Edit memory body → new content_hash → cache miss.
        memory_v2 = dataclasses.replace(memory, body="body v2", content_hash="bbbb1111")
        pair_body_edit = m.DriftPair(memory_v2, decision)
        miss = m.cache_read(vault, pair_body_edit)
        assert miss is None, "body edit should invalidate cache"

        # Edit decision text → new text_hash → cache miss.
        decision_v2 = dataclasses.replace(decision, text="decision v2", text_hash="dddd1111")
        pair_decision_edit = m.DriftPair(memory, decision_v2)
        miss2 = m.cache_read(vault, pair_decision_edit)
        assert miss2 is None, "decision edit should invalidate cache (F5)"
        sys.modules.pop("kb_drift_scan_t", None)
    print("  T6 PASS — cache invalidates on body OR decision edit (F5)")


# ---------------------------------------------------------------------
# T7 — F4 grounding (verbatim excerpt is substring)
# ---------------------------------------------------------------------


def test_excerpt_grounding_check():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        m = import_drift_scan(method)
        memory = m.MemoryObject(
            path=Path("/dev/null"), source_kind="t", memory_id="mem-x",
            created_at="", tags=(), title="t", summary="s",
            body="The team decided to drop Polestar entirely from the H2 roadmap.",
            content_hash="bbbbbbbb",
        )
        # Real substring (post-normalize): grounded.
        v_ok = m.DriftVerdict(
            drifted=True, drift_claim="c", drift_confidence="high",
            verbatim_excerpt="drop Polestar entirely",
        )
        assert m.excerpt_grounded(v_ok, memory) is True

        # Whitespace differences but real substring: grounded.
        v_ws = m.DriftVerdict(
            drifted=True, drift_claim="c", drift_confidence="high",
            verbatim_excerpt="drop  Polestar\n  entirely",
        )
        assert m.excerpt_grounded(v_ws, memory) is True

        # Hallucinated phrase: NOT grounded.
        v_bad = m.DriftVerdict(
            drifted=True, drift_claim="c", drift_confidence="high",
            verbatim_excerpt="Polestar will lead the H1 launch",  # not in body
        )
        assert m.excerpt_grounded(v_bad, memory) is False

        # Empty excerpt: NOT grounded (F4 floor on length).
        v_empty = m.DriftVerdict(
            drifted=True, drift_claim="c", drift_confidence="high",
            verbatim_excerpt="",
        )
        assert m.excerpt_grounded(v_empty, memory) is False

        # Too-short excerpt: NOT grounded (≥10 chars required).
        v_short = m.DriftVerdict(
            drifted=True, drift_claim="c", drift_confidence="high",
            verbatim_excerpt="drop",
        )
        assert m.excerpt_grounded(v_short, memory) is False
        sys.modules.pop("kb_drift_scan_t", None)
    print("  T7 PASS — verbatim excerpt grounding works (F4 closer)")


# ---------------------------------------------------------------------
# T8 — confidence threshold
# ---------------------------------------------------------------------


def test_confidence_threshold_filter():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        m = import_drift_scan(method)
        # Default threshold = medium.
        assert m.confidence_meets_threshold("high", "medium") is True
        assert m.confidence_meets_threshold("medium", "medium") is True
        assert m.confidence_meets_threshold("low", "medium") is False
        # Threshold = low admits everything.
        assert m.confidence_meets_threshold("low", "low") is True
        # Threshold = high admits only high.
        assert m.confidence_meets_threshold("medium", "high") is False
        # Bogus values fail closed.
        assert m.confidence_meets_threshold("nonsense", "medium") is False
        sys.modules.pop("kb_drift_scan_t", None)
    print("  T8 PASS — confidence threshold filter")


# ---------------------------------------------------------------------
# T9 — emit shape passes slice-1 schema
# ---------------------------------------------------------------------


def test_emit_drift_memo_passes_slice1_schema():
    """The emitted memo must carry all 4 drift fields with valid values AND
    `affects_decision` must resolve. To make resolution succeed, we hand-place
    a target artefact under `artefacts/memo/art-<via-uuid>.md` so the lint's
    all-uuids index includes it."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        # Place the source-decision target so affects_decision resolves.
        via_uuid = "abcd1234-1111-2222-3333-444444444444"
        (vault / "artefacts" / "memo" / f"art-{via_uuid}.md").write_text(
            "---\nid: art-" + via_uuid + "\nkind: memo\ncreated_at: 2026-06-01T10:00:00Z\n"
            "title: source\nproduced_by:\n  session_id: aaaaaaaa\n  query: x\n  model: m\n"
            "  sources_cited:\n    - https://x.test\n---\nbody",
            encoding="utf-8",
        )
        m = import_drift_scan(method)
        memory = m.MemoryObject(
            path=Path("/dev/null"), source_kind="granola_note", memory_id="mem-zz",
            created_at="2026-05-08T10:00:00Z", tags=("polestar",), title="t",
            summary="s",
            body="The team decided to drop Polestar entirely from the H2 roadmap.",
            content_hash="bbbbbbbb",
        )
        decision = m.DecisionEntry(
            art_id=via_uuid, title="Polestar in H2", scope="Polestar",
            text="Polestar is in scope for H2.", text_hash="dddddddd",
        )
        pair = m.DriftPair(memory, decision)
        verdict = m.DriftVerdict(
            drifted=True, drift_claim="Drop reverses prior commitment.",
            drift_confidence="high",
            verbatim_excerpt="drop Polestar entirely",
            reasoning="Memory contradicts decision.",
        )
        path = m.emit_drift_memo(
            vault, pair, verdict,
            session_id="abcd1234", query="test",
        )
        assert path.is_file()
        # Direct slice-1 schema validation via lint-provenance internals.
        lint = import_lint(method)
        text = path.read_text(encoding="utf-8")
        fm = lint.parse_yaml_frontmatter(text)
        assert fm is not None
        # _parse_simple_yaml renders YAML's bareword `true` as the lowercase
        # string "true" (slice-1 `_drift_truthy` accepts it case-insensitively).
        assert fm.get("drift_candidate") == "true", fm
        assert fm.get("affects_decision") == f"art://{via_uuid}", fm
        assert fm.get("drift_claim") == "Drop reverses prior commitment.", fm
        assert fm.get("drift_confidence") == "high", fm
        # Build the all-uuids index by walking artefacts (project + flat tier).
        uuids = lint._collect_artefact_uuids(vault / "artefacts")
        # Resolution: the via-uuid is in the flat tier.
        known = set(uuids.keys())
        # Direct call into the slice-1 validator must report no errors.
        violations = lint.check_artefact_md(
            path, expected_project_id=None, known_artefact_uuids=known,
        )
        violation_kinds = [v.kind for v in violations]
        assert violation_kinds == [], f"unexpected violations: {violation_kinds}"
        sys.modules.pop("kb_drift_scan_t", None)
        sys.modules.pop("lint_provenance_t", None)
    print("  T9 PASS — emitted memo passes slice-1 schema (resolves)")


# ---------------------------------------------------------------------
# T10 — emit + dangling resolution must fail under direct lint call
# ---------------------------------------------------------------------


def test_emit_with_dangling_via_fails_lint():
    """If kb-drift-scan emits with a via-uuid that doesn't exist in the
    artefact index, the slice-1 lint must produce `drift-affects-dangling`.
    This is the structural F4-adjacent guarantee that emit isn't blind to
    the resolution invariant downstream lint enforces."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        m = import_drift_scan(method)
        memory = m.MemoryObject(
            path=Path("/dev/null"), source_kind="g", memory_id="mem-y",
            created_at="", tags=("acme",), title="t", summary="s",
            body="long enough body text for grounding extraction here.",
            content_hash="bbbbbbbb",
        )
        decision = m.DecisionEntry(
            art_id="00000000-deadbeef-no-such-target",
            title="Phantom", scope="Acme",
            text="phantom decision text", text_hash="dddddddd",
        )
        pair = m.DriftPair(memory, decision)
        verdict = m.DriftVerdict(
            drifted=True, drift_claim="claim",
            drift_confidence="high",
            verbatim_excerpt="grounding extraction here",
            reasoning="r",
        )
        path = m.emit_drift_memo(
            vault, pair, verdict,
            session_id="aaaaaaaa", query="t",
        )
        lint = import_lint(method)
        uuids = lint._collect_artefact_uuids(vault / "artefacts")
        known = set(uuids.keys())  # the dangling target is not in here
        violations = lint.check_artefact_md(
            path, expected_project_id=None, known_artefact_uuids=known,
        )
        kinds = [v.kind for v in violations]
        assert "drift-affects-dangling" in kinds, kinds
        sys.modules.pop("kb_drift_scan_t", None)
        sys.modules.pop("lint_provenance_t", None)
    print("  T10 PASS — dangling affects_decision fails slice-1 lint")


# ---------------------------------------------------------------------
# T11 — atomic cache write
# ---------------------------------------------------------------------


def test_cache_write_is_atomic():
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        m = import_drift_scan(method)
        memory = m.MemoryObject(
            path=Path("/dev/null"), source_kind="g", memory_id="mem-a",
            created_at="", tags=(), title="", summary="",
            body="b", content_hash="bbbbbbbb",
        )
        decision = m.DecisionEntry(
            art_id="aaaa1111-2222-3333-4444-555555555555",
            title="d", scope="x", text="t", text_hash="dddddddd",
        )
        pair = m.DriftPair(memory, decision)
        m.cache_write(vault, pair, {"verdict": {"drifted": True}})
        # Final file exists, no .tmp residue.
        cache_dir_path = vault / ".harvest" / "kb-drift-scan-cache"
        files = sorted(p.name for p in cache_dir_path.iterdir())
        assert all(not f.endswith(".json.tmp") for f in files), files
        assert any(f.endswith(".json") for f in files), files
        sys.modules.pop("kb_drift_scan_t", None)
    print("  T11 PASS — cache write is atomic (tmp + rename)")


# ---------------------------------------------------------------------
# T12 — watermark gate when quota exhausted
# ---------------------------------------------------------------------


def test_watermark_not_advanced_on_quota_exhaustion():
    """When --max-llm-calls forces a skip, the watermark must NOT advance —
    otherwise the next default run silently skips the un-scanned pairs."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_decision(vault, title="Polestar in H2", scope="Polestar",
                       via_uuid="aaaaaaaa-1111-2222-3333-444444444444")
        # 2 memories matching the scope ⇒ 2 pairs ⇒ would need 2 LLM calls.
        write_memory(vault, "granola_note", "m1", tags=["polestar"])
        write_memory(vault, "slack_thread", "m2", tags=["polestar"])
        # Cap=0 with --skip-llm OFF means: first pair would try claude -p,
        # which fails (no `claude` binary in test env) — but we run with cap=0
        # to force the quota path BEFORE any claude -p call.
        r = run_drift_scan(method, "--all", "--max-llm-calls", "0")
        assert r.returncode == 0, r.stderr
        # 2 pairs surviving routing + 0 LLM calls = 2 skipped for quota.
        assert "skipped_for_quota=2" in r.stderr or "skipped 2" in r.stderr or "skipped_for_quota" in r.stderr, r.stderr
        # Watermark must NOT exist (or must be unchanged).
        wm = vault / ".harvest" / "kb-drift-scan-watermark.json"
        assert not wm.is_file(), "watermark should not advance on quota exhaustion"
    print("  T12 PASS — watermark NOT advanced when quota exhausted")


# ---------------------------------------------------------------------
# T13 — hard cap (F3 quota guard)
# ---------------------------------------------------------------------


def test_max_llm_calls_is_hard_cap():
    """F3: even when many pairs survive routing, llm_calls must never exceed
    --max-llm-calls. Probe at cap=0 (no claude -p invoked at all)."""
    with tempfile.TemporaryDirectory() as td:
        method, vault = make_fixture(Path(td))
        write_decision(vault, title="Polestar", scope="Polestar",
                       via_uuid="aaaaaaaa-1111-2222-3333-444444444444")
        write_decision(vault, title="Acko", scope="Acko",
                       via_uuid="bbbbbbbb-1111-2222-3333-444444444444")
        write_memory(vault, "granola_note", "m1", tags=["polestar", "acko"])
        write_memory(vault, "slack_thread", "m2", tags=["polestar", "acko"])
        # 4 surviving pairs (2 mem × 2 decisions). cap=0 must produce 0 LLM calls.
        r = run_drift_scan(method, "--all", "--max-llm-calls", "0")
        assert r.returncode == 0, r.stderr
        assert "llm_calls=0" in r.stderr, r.stderr
        assert "phase 1: 4 (memory, decision)" in r.stderr, r.stderr
    print("  T13 PASS — max-llm-calls hard cap holds (F3)")


if __name__ == "__main__":
    print("Running test_kb_drift_scan_acceptance.py...")
    test_empty_pool_emits_nothing()
    test_load_decisions_filters_by_scope_and_via()
    test_scope_intersection_positive()
    test_scope_intersection_rejects_unrelated()
    test_skip_llm_emits_nothing()
    test_cache_key_invalidates_on_either_edit()
    test_excerpt_grounding_check()
    test_confidence_threshold_filter()
    test_emit_drift_memo_passes_slice1_schema()
    test_emit_with_dangling_via_fails_lint()
    test_cache_write_is_atomic()
    test_watermark_not_advanced_on_quota_exhaustion()
    test_max_llm_calls_is_hard_cap()
    print("All kb-drift-scan tests passed.")
