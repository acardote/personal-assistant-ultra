#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Multi-agent router: turn one user query into advisor + adversarial critic
(+ optional specialist) perspectives, all grounded in the layer-3 KB and
relevant layer-2 memory objects.

Usage:
    tools/route.py "your question"
    tools/route.py --batch tests/router-cases.json --report-out /tmp/report.json
    tools/route.py "query" --no-critic --no-specialist  # advisor-only, for ablation

Architecture per parent #1's A3 ("three perspectives is the sweet spot"):
    1. Advisor — primary helpful response. Sharp persona at tools/prompts/advisor.md.
    2. Adversarial critic — hard-instructed "you are not allowed to agree."
       At tools/prompts/critic.md.
    3. Optional specialist — invoked only when a routing rule fires. The set of
       specialists lives at tools/prompts/specialists/<name>.md. Currently:
         - incident-response: triggers on keywords {incident, outage, postmortem,
           remediation, root cause}.

The router calls Claude Code (`claude -p`) for each persona. Critic's call
includes the advisor's response in context — by construction the critic can
see exactly what it must disagree with.

Synthesis: the router does NOT collapse the perspectives. It outputs both,
delineated, so downstream consumers (or the user) preserve both. F2 on issue
#7 directly warns against synthesis-by-collapse — we don't do it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402
from _metrics import emit, time_event, inherit_or_start  # noqa: E402

_CFG = load_config()
METHOD_ROOT = _CFG.method_root
PROMPTS_DIR = METHOD_ROOT / "tools" / "prompts"
SPECIALISTS_DIR = PROMPTS_DIR / "specialists"
ASSEMBLE_KB = METHOD_ROOT / "tools" / "assemble-kb.py"
MEMORY_ROOT = _CFG.memory_root
PROJECT_ROOT = METHOD_ROOT  # legacy alias for path-display


SPECIALIST_TRIGGERS: dict[str, tuple[str, ...]] = {
    "incident-response": ("incident", "outage", "postmortem", "post-mortem", "remediation", "root cause"),
}

# Stopwords for topic-keyword extraction (privacy-preserving signal extraction).
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "what", "where", "when",
    "how", "why", "on", "in", "of", "to", "for", "with", "and", "or", "but",
    "we", "i", "me", "my", "this", "that", "these", "those", "it", "its",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "should", "could", "can", "may", "might", "must",
    "from", "by", "at", "as", "if", "then", "than", "so", "not", "no",
    "yes", "any", "all", "some", "you", "your", "they", "them", "their",
    "he", "she", "him", "her", "his", "hers", "us", "our", "ours",
    "latest", "current", "now", "today", "yesterday", "tomorrow", "week",
    "tell", "show", "give", "summarize", "explain", "describe",
})


def extract_topic_keywords(query: str, *, limit: int = 5) -> list[str]:
    """Privacy-preserving topic-keyword extraction from a query string.

    Returns up to `limit` distinctive lowercase tokens (≥4 chars, not stopwords).
    Used to tag metrics events with what topic the query was about, without
    logging the raw query text.
    """
    tokens = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]+\b", query.lower())
    seen = set()
    out: list[str] = []
    for t in tokens:
        if t in _STOPWORDS or len(t) < 4 or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


@dataclass
class RouteResult:
    query: str
    kb_tokens: int
    memory_tokens: int
    memory_files: list[str]
    specialist: str | None
    advisor_response: str = ""
    critic_response: str = ""
    specialist_response: str = ""


def assemble_kb_text() -> tuple[str, int]:
    out = subprocess.run(
        [str(ASSEMBLE_KB), "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(out.stdout)
    return payload["rendered"], payload["token_count"]


def load_memory_objects(query: str, *, max_items: int = 12) -> tuple[str, int, list[str]]:
    """Naive layer-2 retrieval with recency decay (per #8 acceptance criterion 3).

    Scoring: relevance × recency_weight, where:
      - relevance = count of query keywords (≥4 chars) appearing in body, +1 fallback
        so every item has a non-zero base score when no keywords match.
      - recency_weight = exp(-age_days × ln 2 / 90) — half-life 90 days.

    Items in `memory/.archive/` are excluded (pruned per #8). Top `max_items`
    selected; ties broken by recency. Real semantic retrieval is future work —
    see ADR-0001 re-opening criteria.
    """
    import math as _math
    from datetime import datetime as _dt, timezone as _tz
    archive_root = MEMORY_ROOT / ".archive"
    files = [p for p in sorted(MEMORY_ROOT.rglob("*.md")) if archive_root not in p.parents]
    if not files:
        return "", 0, []

    needles = {w.lower() for w in re.findall(r"\b[\w'-]{4,}\b", query)}
    now = _dt.now(_tz.utc)
    half_life_days = 90.0

    scored: list[tuple[float, Path, _dt]] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        # Parse created_at from frontmatter for decay weighting.
        created_at = now  # fallback if frontmatter absent
        if text.startswith("---\n"):
            parts = text.split("\n---\n", 1)
            if len(parts) == 2:
                front_text = parts[0][4:]
                m = re.search(r"^created_at:\s*['\"]?([^\s'\"]+)['\"]?", front_text, re.MULTILINE)
                if m:
                    try:
                        created_at = _dt.fromisoformat(m.group(1).replace("Z", "+00:00"))
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=_tz.utc)
                    except ValueError:
                        pass

        relevance = 1
        if needles:
            lower = text.lower()
            relevance += sum(1 for n in needles if n in lower)
        age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
        recency = _math.exp(-age_days * _math.log(2) / half_life_days)

        # Per #10's multi-fidelity dedup: canonical (or unclustered) memos get
        # a small bonus so they surface ahead of alternates at equal relevance.
        # Alternates remain reachable on alternate-only-content queries because
        # the bonus is multiplicative and modest, not a filter (per F3 from #10).
        canonical_bonus = 1.0
        if text.startswith("---\n"):
            parts = text.split("\n---\n", 1)
            if len(parts) == 2:
                front_text = parts[0][4:]
                m_canon = re.search(r"^is_canonical_for_event:\s*(true|false|True|False)", front_text, re.MULTILINE)
                if m_canon:
                    is_canonical = m_canon.group(1).lower() == "true"
                    if not is_canonical:
                        canonical_bonus = 0.85  # alternate
                # If the field is absent, treat as unclustered → keep bonus 1.0

        score = float(relevance) * recency * canonical_bonus
        scored.append((score, path, created_at))

    scored.sort(key=lambda t: (-t[0], -t[2].timestamp()))
    selected = [p for _, p, _ in scored[:max_items]]

    def _disp(p: Path) -> str:
        try:
            return str(p.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(p)

    blocks: list[str] = []
    for path in selected:
        rel = _disp(path)
        body = path.read_text(encoding="utf-8")
        blocks.append(f"<!-- BEGIN memory: {rel} -->\n{body}\n<!-- END memory: {rel} -->")
    rendered = "\n\n".join(blocks)

    from _tokens import estimate_tokens
    return rendered, estimate_tokens(rendered), [_disp(p) for p in selected]


def detect_specialist(query: str) -> str | None:
    q = query.lower()
    for name, triggers in SPECIALIST_TRIGGERS.items():
        if any(t in q for t in triggers):
            return name
    return None


def call_claude(prompt: str) -> str:
    result = subprocess.run(
        ["claude", "-p", prompt],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_context_block(kb_text: str, memory_text: str, query: str) -> str:
    return (
        "<KB>\n"
        f"{kb_text}\n"
        "</KB>\n\n"
        "<MEMORY>\n"
        f"{memory_text if memory_text else '(no memory objects retrieved)'}\n"
        "</MEMORY>\n\n"
        "<QUESTION>\n"
        f"{query}\n"
        "</QUESTION>\n"
    )


def run_advisor(query: str, context: str) -> str:
    prompt = (
        (PROMPTS_DIR / "advisor.md").read_text(encoding="utf-8")
        + "\n\n---\n\n"
        + context
    )
    return call_claude(prompt)


def run_critic(query: str, context: str, advisor_response: str) -> str:
    prompt = (
        (PROMPTS_DIR / "critic.md").read_text(encoding="utf-8")
        + "\n\n---\n\n"
        + context
        + f"\n<ADVISOR_RESPONSE>\n{advisor_response}\n</ADVISOR_RESPONSE>\n"
    )
    return call_claude(prompt)


def run_specialist(name: str, query: str, context: str) -> str:
    path = SPECIALISTS_DIR / f"{name}.md"
    if not path.exists():
        return f"(specialist '{name}' has no prompt file at {path})"
    prompt = path.read_text(encoding="utf-8") + "\n\n---\n\n" + context
    return call_claude(prompt)


def route(query: str, *, no_critic: bool = False, no_specialist: bool = False) -> RouteResult:
    # Per-query session: inherit if parent set PA_SESSION_ID, else fresh.
    inherit_or_start()
    topic_kws = extract_topic_keywords(query)

    with time_event("query", topic_keywords=topic_kws,
                    no_critic=no_critic, no_specialist=no_specialist) as q_tracker:

        with time_event("kb_load") as kb_tracker:
            kb_text, kb_tokens = assemble_kb_text()
            kb_tracker["kb_tokens"] = kb_tokens
            kb_tracker["kb_chars"] = len(kb_text)

        with time_event("memory_retrieve", topic_keywords=topic_kws) as mem_tracker:
            memory_text, memory_tokens, memory_files = load_memory_objects(query)
            mem_tracker["memory_hits"] = len(memory_files)
            mem_tracker["memory_tokens"] = memory_tokens

        specialist = None if no_specialist else detect_specialist(query)
        context = build_context_block(kb_text, memory_text, query)

        result = RouteResult(
            query=query,
            kb_tokens=kb_tokens,
            memory_tokens=memory_tokens,
            memory_files=memory_files,
            specialist=specialist,
        )

        print(f"[route] kb={kb_tokens}t memory={memory_tokens}t files={len(memory_files)} specialist={specialist or 'none'}", file=sys.stderr)
        print(f"[route] invoking advisor...", file=sys.stderr)
        with time_event("advisor_call", topic_keywords=topic_kws) as adv_tracker:
            result.advisor_response = run_advisor(query, context)
            adv_tracker["response_chars"] = len(result.advisor_response)

        if not no_critic:
            print(f"[route] invoking adversarial critic...", file=sys.stderr)
            with time_event("critic_call", topic_keywords=topic_kws) as crit_tracker:
                result.critic_response = run_critic(query, context, result.advisor_response)
                crit_tracker["response_chars"] = len(result.critic_response)

        if specialist:
            print(f"[route] invoking specialist: {specialist}...", file=sys.stderr)
            with time_event("specialist_call", specialist=specialist, topic_keywords=topic_kws) as sp_tracker:
                result.specialist_response = run_specialist(specialist, query, context)
                sp_tracker["response_chars"] = len(result.specialist_response)

        # Top-level query tracker mutations (land on query_end)
        q_tracker["kb_tokens"] = kb_tokens
        q_tracker["memory_tokens"] = memory_tokens
        q_tracker["memory_hits"] = len(memory_files)
        q_tracker["specialist"] = specialist
        q_tracker["empty_handed"] = (memory_tokens == 0 and kb_tokens > 0)

    return result


def render_human_output(r: RouteResult) -> str:
    sections = [
        f"# Query\n\n{r.query}\n",
        f"# Context\n- KB tokens: {r.kb_tokens}\n- Memory tokens: {r.memory_tokens}\n- Memory files: {len(r.memory_files)}\n- Specialist invoked: {r.specialist or 'none'}\n",
        f"# Advisor\n\n{r.advisor_response}\n",
    ]
    if r.critic_response:
        sections.append(f"# Adversarial critic\n\n{r.critic_response}\n")
    if r.specialist_response:
        sections.append(f"# Specialist ({r.specialist})\n\n{r.specialist_response}\n")
    return "\n".join(sections)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Multi-agent router for personal-assistant queries.")
    parser.add_argument("query", nargs="?", help="The user query (omit if --batch).")
    parser.add_argument("--batch", help="Path to a JSON file with a list of {query, ...} cases.")
    parser.add_argument("--report-out", help="Write batch JSON report to this path.")
    parser.add_argument("--no-critic", action="store_true", help="Skip the adversarial critic call (ablation).")
    parser.add_argument("--no-specialist", action="store_true", help="Skip the specialist call (ablation).")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human text.")
    args = parser.parse_args(argv[1:])

    if args.batch:
        cases = json.loads(Path(args.batch).read_text(encoding="utf-8"))
        results = []
        for i, case in enumerate(cases, start=1):
            print(f"\n=== case {i}/{len(cases)}: {case.get('id', '<unnamed>')} ===", file=sys.stderr)
            r = route(case["query"], no_critic=args.no_critic, no_specialist=args.no_specialist)
            results.append({**case, **asdict(r)})
        report = {"cases": results}
        if args.report_out:
            Path(args.report_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"\n[batch] report written to {args.report_out}", file=sys.stderr)
        else:
            print(json.dumps(report, indent=2))
        return 0

    if not args.query:
        parser.error("query is required (or use --batch)")

    r = route(args.query, no_critic=args.no_critic, no_specialist=args.no_specialist)
    if args.json:
        print(json.dumps(asdict(r), indent=2))
    else:
        print(render_human_output(r))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
