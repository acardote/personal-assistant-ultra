#!/usr/bin/env -S uv run --quiet --with pyyaml --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""Evaluation harness for parent #1's A1: is the memory architecture the
differentiating layer, or would vanilla long-context do as well?

Usage:
    tools/eval-harness.py --questions tests/eval-questions.json \\
        --out /tmp/claude/eval-report.json
    tools/eval-harness.py --questions tests/eval-questions.json --first 2 \\
        --out /tmp/claude/eval-smoke.json   # smoke test on first 2 questions

For each question, runs four configurations:
  - `no-memory`        : `claude -p "<question>"` bare, no skill, no context
  - `vanilla-long-context`: `claude -p "<KB+raw artifacts up to budget>\\n\\n<question>"`
                       — concatenated raw/* up to a token budget MATCHED to the
                         full-architecture's retrieved-context size (per F3)
  - `full-architecture`: `tools/route.py "<question>"` (advisor + critic + synthesizer
                       per #40). Measures the synthesizer in isolation. Does NOT
                       exercise the live-call path — that's skill-orchestrated.
  - `full-skill`       : `claude -p "<question>"` with skill auto-activation. Exercises
                       the user-facing entry that #39's live-call path runs through.
                       Each call gets an isolated PA_METRICS_DIR so events don't
                       pollute the operator dashboard, and the events are summarized
                       into the rater notes (skill_activated, gap reason, live calls).

Outputs a JSON report with:
  - blinded labels (A, B, C, D per question; the run's blinding key stored separately
    at <out>.key.json so the user can rate without seeing config identity, then
    map back) — F2 mitigation.
  - per-config token counts for each input
  - the responses

The user then opens the blinded report, supplies a 1-5 quality rating + free-text
comment per question/label, and the harness emits a delta artifact.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402
from _tokens import estimate_tokens, truncate_to_tokens  # noqa: E402

_CFG = load_config()
METHOD_ROOT = _CFG.method_root
RAW_ROOT = _CFG.raw_root
ROUTE_TOOL = METHOD_ROOT / "tools" / "route.py"
ASSEMBLE_KB_TOOL = METHOD_ROOT / "tools" / "assemble-kb.py"
PROJECT_ROOT = METHOD_ROOT  # legacy alias


@dataclass
class Run:
    config: str
    response: str
    input_tokens: int
    notes: str = ""


@dataclass
class QuestionEval:
    id: str
    text: str
    runs: dict[str, Run] = field(default_factory=dict)  # config -> Run
    blinding: dict[str, str] = field(default_factory=dict)  # blinded_label -> config


def call_claude(prompt: str, *, env: dict | None = None) -> str:
    # stdin=DEVNULL is load-bearing — claude -p will hang waiting for input
    # otherwise (per probe on 2026-05-06).
    result = subprocess.run(
        ["claude", "-p", prompt],
        check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    return result.stdout.strip()


def count_tokens(text: str) -> int:
    return estimate_tokens(text)


# ───────────────────────────────────────────────────────────────────────
# Config 1: no memory
# ───────────────────────────────────────────────────────────────────────

def run_no_memory(question: str) -> Run:
    prompt = question
    resp = call_claude(prompt)
    return Run(config="no-memory", response=resp, input_tokens=count_tokens(prompt))


# ───────────────────────────────────────────────────────────────────────
# Config 2: vanilla long-context (matched-size baseline per F3)
# ───────────────────────────────────────────────────────────────────────

def assemble_long_context(question: str, target_tokens: int) -> tuple[str, int]:
    """Concatenate KB + raw/* up to target_tokens. F3 requires matched ordering
    and budget; we use the same ordering the full-architecture retrieval would
    apply (relevance-by-keyword, recency tiebreak) but no compression and no
    pre-curation. The point of this baseline is: 'what if we just dumped raw
    context into a long-context model?'"""
    # Pull KB (always present in both configs).
    kb_resp = subprocess.run(
        [str(ASSEMBLE_KB_TOOL), "--json"],
        check=True, capture_output=True, text=True,
    )
    kb_payload = json.loads(kb_resp.stdout)
    kb_text = kb_payload["rendered"]

    # Pull all raw artifacts in mtime-desc order, dedup by path.
    raw_files = sorted(RAW_ROOT.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    raw_blocks: list[str] = []
    used = count_tokens(kb_text) + count_tokens(question)
    for path in raw_files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            disp = str(path.relative_to(METHOD_ROOT))
        except ValueError:
            try:
                disp = str(path.relative_to(_CFG.content_root))
            except ValueError:
                disp = str(path)
        block = f"<!-- raw: {disp} -->\n{text}\n"
        block_tokens = count_tokens(block)
        if used + block_tokens > target_tokens:
            # Truncate this block to fit if it's the first; else stop.
            remaining = target_tokens - used
            if remaining > 200 and not raw_blocks:
                # Account for the appended suffix (~7 tokens) so the final
                # block doesn't exceed the budget. Per round-1 reviewer
                # suggestion #1 on PR #35.
                suffix = "\n[...truncated to fit budget]"
                suffix_tokens = count_tokens(suffix)
                truncated_text = truncate_to_tokens(block, remaining - suffix_tokens)
                raw_blocks.append(truncated_text + suffix)
                used += remaining
            break
        raw_blocks.append(block)
        used += block_tokens
    rendered = (
        kb_text
        + "\n\n"
        + "\n\n".join(raw_blocks)
        + f"\n\n=== QUESTION ===\n{question}\n"
    )
    return rendered, count_tokens(rendered)


def run_vanilla_long_context(question: str, target_tokens: int) -> Run:
    prompt, tokens = assemble_long_context(question, target_tokens)
    resp = call_claude(prompt)
    return Run(
        config="vanilla-long-context",
        response=resp,
        input_tokens=tokens,
        notes=f"Budget matched to full-architecture retrieved context ({target_tokens} tokens).",
    )


# ───────────────────────────────────────────────────────────────────────
# Config 3: full architecture (route.py)
# ───────────────────────────────────────────────────────────────────────

def run_full_architecture(question: str) -> tuple[Run, int]:
    """Returns (Run, retrieved_context_token_count) so the long-context baseline
    can match its budget."""
    result = subprocess.run(
        [str(ROUTE_TOOL), question, "--json"],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout)
    # Synthesize a single response by concatenating advisor + critic (+ specialist).
    # This is the architecture's user-facing output; the harness scores the
    # composite, since that's what the user actually sees.
    parts: list[str] = []
    # Per #40: the user-facing output is the synthesized response when
    # synthesis ran (advisor + critic merged). Falls back to advisor's
    # draft when --no-critic is in play.
    if payload.get("synthesized_response"):
        parts.append(payload["synthesized_response"])
    else:
        parts.append(payload["advisor_response"])
        if payload.get("specialist_response"):
            parts.append(f"## Specialist ({payload['specialist']})\n\n{payload['specialist_response']}")
    response = "\n\n".join(parts)

    retrieved_tokens = payload["kb_tokens"] + payload["memory_tokens"]
    return Run(
        config="full-architecture",
        response=response,
        input_tokens=retrieved_tokens,
        notes=f"KB: {payload['kb_tokens']} t, Memory: {payload['memory_tokens']} t, Specialist: {payload.get('specialist') or 'none'}",
    ), retrieved_tokens


# ───────────────────────────────────────────────────────────────────────
# Config 4: full-skill (claude -p with skill auto-activation, exercises live path)
# ───────────────────────────────────────────────────────────────────────

def _summarize_events(events_dir: Path) -> str:
    """Walk this run's isolated metrics dir and summarize what fired.
    Surfaced in the rater notes so we can tell "skill activated AND live
    fired" apart from "skill activated but no gap" apart from "skill
    didn't activate." Per F2 of #19's eval: full-skill is only
    differentiating if the live path actually fires when memory misses."""
    events_seen: dict[str, int] = {}
    live_calls: list[str] = []
    gap_reason: str | None = None
    for f in events_dir.glob("events-*.jsonl"):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = e.get("event") or "?"
            events_seen[ev] = events_seen.get(ev, 0) + 1
            if ev == "live_call_end":
                d = e.get("data") or {}
                live_calls.append(f"{d.get('source','?')}={d.get('status','?')}")
            if ev == "gap_detected":
                d = e.get("data") or {}
                gap_reason = d.get("reason")
    skill_activated = "skill_start" in events_seen
    parts = [f"skill_activated={skill_activated}"]
    if gap_reason:
        parts.append(f"gap={gap_reason}")
    if live_calls:
        parts.append("live=[" + ",".join(live_calls) + "]")
    else:
        parts.append("live=none")
    return ", ".join(parts)


def run_full_skill(question: str, *, events_dir: Path, qid: str) -> Run:
    """Drive a `claude -p` session and let the personal-assistant skill
    auto-activate. This is the user-facing entry that exercises the
    live-call path (#52/#54/#56) — `tools/route.py` does NOT fire live
    calls; the skill orchestrates them per the #51 architecture decision.

    Each call writes to a per-question subdir of `events_dir` so events
    don't pollute the operator dashboard, AND so the raw events are
    preserved alongside the eval report for forensic re-derivation if
    the summary turns out to be wrong (per pr-challenger #2 on PR #60).
    The rater notes carry the summary; the raw events stay on disk."""
    import os as _os
    qdir = events_dir / qid
    qdir.mkdir(parents=True, exist_ok=True)
    env = _os.environ.copy()
    env["PA_METRICS_DIR"] = str(qdir)
    # Fresh session per call so events don't bleed across questions.
    env.pop("PA_SESSION_ID", None)
    try:
        resp = call_claude(question, env=env)
    except subprocess.CalledProcessError as e:
        resp = f"[full-skill call failed: exit {e.returncode}]\n{(e.stdout or '')[:500]}"
    notes = _summarize_events(qdir)
    return Run(
        config="full-skill",
        response=resp,
        input_tokens=count_tokens(question),
        notes=notes,
    )


# ───────────────────────────────────────────────────────────────────────
# Run + blinding + report
# ───────────────────────────────────────────────────────────────────────

def evaluate_question(qid: str, qtext: str, rng: random.Random, *, events_dir: Path) -> QuestionEval:
    qe = QuestionEval(id=qid, text=qtext)

    # Run full-architecture first to learn the retrieved-context size.
    print(f"  [{qid}] full-architecture...", file=sys.stderr)
    full_run, retrieved_tokens = run_full_architecture(qtext)
    qe.runs["full-architecture"] = full_run

    # Match long-context budget to retrieved-context size (F3).
    target_tokens = max(retrieved_tokens, 2000)
    print(f"  [{qid}] vanilla-long-context (budget {target_tokens} t)...", file=sys.stderr)
    qe.runs["vanilla-long-context"] = run_vanilla_long_context(qtext, target_tokens)

    print(f"  [{qid}] no-memory...", file=sys.stderr)
    qe.runs["no-memory"] = run_no_memory(qtext)

    # full-skill exercises the live-call path that route.py cannot reach.
    # Per #51 the skill orchestrates live calls; eval-harness needs to invoke
    # the skill (claude -p with auto-activation) to measure that path.
    print(f"  [{qid}] full-skill...", file=sys.stderr)
    qe.runs["full-skill"] = run_full_skill(qtext, events_dir=events_dir, qid=qid)

    # Blinding: shuffle config order under labels A, B, C, D (F2).
    configs = list(qe.runs.keys())
    rng.shuffle(configs)
    labels = ["A", "B", "C", "D"]
    qe.blinding = dict(zip(labels, configs))
    return qe


def render_report(evals: list[QuestionEval], blind_key_path: Path) -> tuple[dict, dict]:
    """Returns (blinded_report, key_payload). The user reads `blinded_report` to
    judge; only `key_payload` reveals which label was which config."""
    blinded = {
        "questions": [],
        "$instructions": "For each question, supply a 1-5 quality rating + free-text comment per label A/B/C/D. Do not look at the key file until your judgments are recorded.",
    }
    key = {"questions": []}
    for qe in evals:
        item = {
            "id": qe.id,
            "question": qe.text,
            "responses": {},
            "judgments_TODO": {label: {"rating_1_to_5": None, "comment": ""} for label in qe.blinding},
        }
        key_item = {"id": qe.id, "blinding": qe.blinding}
        for label, cfg in qe.blinding.items():
            run = qe.runs[cfg]
            item["responses"][label] = {
                "input_tokens": run.input_tokens,
                "response": run.response,
                "notes_HIDE_FROM_RATER": run.notes,  # name signals to user not to peek
            }
        blinded["questions"].append(item)
        key["questions"].append(key_item)
    return blinded, key


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Evaluation harness for A1.")
    parser.add_argument("--questions", required=True, help="JSON file with the frozen questions.")
    parser.add_argument("--out", required=True, help="Output path for the blinded report.")
    parser.add_argument("--first", type=int, default=None, help="Only run the first N questions (for smoke tests).")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for blinding shuffle.")
    args = parser.parse_args(argv[1:])

    questions_payload = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    questions = questions_payload["questions"]
    if args.first:
        questions = questions[:args.first]

    if questions_payload.get("_status", "").startswith("PLACEHOLDER"):
        print("[eval-harness] WARNING: questions file is marked PLACEHOLDER — F1 from challenger says these must be real recurring questions traceable to ≥2 prior occurrences before treating output as evidence.", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # full-skill events live alongside the report for forensic re-derivation
    # if a `Run.notes` summary turns out to be wrong (per pr-challenger #2 on
    # PR #60). One subdir per question id under <out-stem>.events/.
    events_dir = out_path.parent / f"{out_path.stem}.events"
    events_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    evals: list[QuestionEval] = []
    for q in questions:
        print(f"\n=== {q['id']}: {q['text']!r} ===", file=sys.stderr)
        qe = evaluate_question(q["id"], q["text"], rng, events_dir=events_dir)
        evals.append(qe)

    key_path = out_path.with_suffix(".key.json")
    blinded, key = render_report(evals, key_path)
    out_path.write_text(json.dumps(blinded, indent=2), encoding="utf-8")
    key_path.write_text(json.dumps(key, indent=2), encoding="utf-8")

    print(f"\n[eval-harness] blinded report: {out_path}", file=sys.stderr)
    print(f"[eval-harness] key (do not peek until judged): {key_path}", file=sys.stderr)
    print(f"[eval-harness] full-skill events: {events_dir}", file=sys.stderr)
    print(f"[eval-harness] questions evaluated: {len(evals)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
