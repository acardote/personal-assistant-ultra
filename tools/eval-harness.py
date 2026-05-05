#!/usr/bin/env -S uv run --quiet --with tiktoken --with pyyaml --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["tiktoken>=0.7", "pyyaml>=6"]
# ///
"""Evaluation harness for parent #1's A1: is the memory architecture the
differentiating layer, or would vanilla long-context do as well?

Usage:
    tools/eval-harness.py --questions tests/eval-questions.json \\
        --out /tmp/claude/eval-report.json
    tools/eval-harness.py --questions tests/eval-questions.json --first 2 \\
        --out /tmp/claude/eval-smoke.json   # smoke test on first 2 questions

For each question, runs three configurations:
  - `no-memory`        : `claude -p "<question>"` with no system prompt context
  - `vanilla-long-context`: `claude -p "<KB+raw artifacts up to budget>\\n\\n<question>"`
                       — concatenated raw/* up to a token budget MATCHED to the
                         full-architecture's retrieved-context size (per F3)
  - `full-architecture`: `tools/route.py "<question>"` (advisor + critic + optional specialist)

Outputs a JSON report with:
  - blinded labels (A, B, C per question; the run's blinding key stored separately
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

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402

_CFG = load_config()
METHOD_ROOT = _CFG.method_root
RAW_ROOT = _CFG.raw_root
ROUTE_TOOL = METHOD_ROOT / "tools" / "route.py"
ASSEMBLE_KB_TOOL = METHOD_ROOT / "tools" / "assemble-kb.py"
PROJECT_ROOT = METHOD_ROOT  # legacy alias
ENCODER = tiktoken.get_encoding("cl100k_base")


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


def call_claude(prompt: str) -> str:
    result = subprocess.run(["claude", "-p", prompt], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def count_tokens(text: str) -> int:
    return len(ENCODER.encode(text))


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
                # Take the first `remaining` tokens of this block.
                truncated_text = ENCODER.decode(ENCODER.encode(block)[:remaining])
                raw_blocks.append(truncated_text + "\n[...truncated to fit budget]")
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
    parts.append(f"## Advisor\n\n{payload['advisor_response']}")
    if payload.get("critic_response"):
        parts.append(f"## Adversarial critic\n\n{payload['critic_response']}")
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
# Run + blinding + report
# ───────────────────────────────────────────────────────────────────────

def evaluate_question(qid: str, qtext: str, rng: random.Random) -> QuestionEval:
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

    # Blinding: shuffle config order under labels A, B, C (F2).
    configs = list(qe.runs.keys())
    rng.shuffle(configs)
    labels = ["A", "B", "C"]
    qe.blinding = dict(zip(labels, configs))
    return qe


def render_report(evals: list[QuestionEval], blind_key_path: Path) -> tuple[dict, dict]:
    """Returns (blinded_report, key_payload). The user reads `blinded_report` to
    judge; only `key_payload` reveals which label was which config."""
    blinded = {
        "questions": [],
        "$instructions": "For each question, supply a 1-5 quality rating + free-text comment per label A/B/C. Do not look at the key file until your judgments are recorded.",
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

    rng = random.Random(args.seed)
    evals: list[QuestionEval] = []
    for q in questions:
        print(f"\n=== {q['id']}: {q['text']!r} ===", file=sys.stderr)
        qe = evaluate_question(q["id"], q["text"], rng)
        evals.append(qe)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    key_path = out_path.with_suffix(".key.json")
    blinded, key = render_report(evals, key_path)
    out_path.write_text(json.dumps(blinded, indent=2), encoding="utf-8")
    key_path.write_text(json.dumps(key, indent=2), encoding="utf-8")

    print(f"\n[eval-harness] blinded report: {out_path}", file=sys.stderr)
    print(f"[eval-harness] key (do not peek until judged): {key_path}", file=sys.stderr)
    print(f"[eval-harness] questions evaluated: {len(evals)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
