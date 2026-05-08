#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""kb-drift-scan.py — drift detector against kb/decisions.md (slice 2 of #135).

Walks `<vault>/memory/<source>/*.md` since the last drift-scan watermark.
For each memory, intersects against `<vault>/kb/decisions.md` entries on
their `**Scope:**` field; for each surviving (memory, decision) pair runs
`claude -p` to judge drift; emits a `kind=memo` artefact with the slice-1
drift-candidate schema if the LLM judges drift at-or-above the confidence
threshold.

Routing (F3 cap-leak guard): Scope intersection is the primary gate.
Without it, a 56-decision × 129-memory vault would brute-force to 7,224
LLM calls. Intersection keeps real-world runs in the dozens.

Cache (F5 cache-invalidation guard): per-pair cache keyed on
sha8(memory_body || decision_text) so EITHER edit invalidates. Body edits
re-LLM (matches kb-scan precedent); decision edits also re-LLM.

Hallucination floor (F4 grounding guard): the LLM is required to return
a verbatim excerpt from the memory body; emission verifies it appears as
a substring before writing the memo. A drift_claim that can't be audited
back to the source is rejected.

Per ADR-0003 F2 (autonomous-producer carve-out): emits `kind=memo`
artefacts only. Does NOT write to `<vault>/kb/*` directly. The kb-process
drift-walk (slice 3 of #135) is the human-review consumer.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

DEFAULT_MAX_LLM_CALLS = 100
DEFAULT_CONFIDENCE_THRESHOLD = "medium"
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
MEMORY_BODY_CAP = 2500  # chars passed to LLM (matches kb-scan's prompt-bound)
EXCERPT_MIN_LEN = 10    # F4: grounding excerpt must be at least this many chars

# Decisions.md parsing — recognises kb-scan-landed entries which carry both a
# **Scope:** field (post-#136) AND a `via=art-<uuid>` tag in the produced_by
# inline comment. Hand-written seed entries without `via=` are silently
# skipped because they have no canonical artefact to anchor `affects_decision`
# against; that's an intentional v1 limitation (drift detection only fires
# against scan-landed decisions).
DECISION_SCOPE_RE = re.compile(
    r"^\s*-\s*\*\*Scope:\*\*\s*(?P<scope>[^\n]+?)\s*$",
    re.MULTILINE,
)
DECISION_VIA_RE = re.compile(r"via=art-(?P<via>[a-f0-9-]+)")
DECISION_TITLE_RE = re.compile(r"^##\s+(?P<title>\S.*?)\s*$", re.MULTILINE)


# ---------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MemoryObject:
    path: Path
    source_kind: str
    memory_id: str
    created_at: str
    tags: tuple[str, ...]
    title: str
    summary: str
    body: str
    content_hash: str  # sha8(body)


@dataclasses.dataclass(frozen=True)
class DecisionEntry:
    art_id: str          # via-uuid (full form, e.g. "0168866f-95a6-..."); the
                         # `affects_decision: art://<art_id>` reference uses this verbatim.
    title: str
    scope: str
    text: str            # full ## section body, used for prompt + cache key
    text_hash: str       # sha8(text)


@dataclasses.dataclass(frozen=True)
class DriftPair:
    memory: MemoryObject
    decision: DecisionEntry


@dataclasses.dataclass
class DriftVerdict:
    drifted: bool
    drift_claim: str
    drift_confidence: str       # high | medium | low
    verbatim_excerpt: str       # F4: must be substring of memory body
    reasoning: str = ""         # LLM-stated rationale; surfaced in the emitted body


# ---------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------


def watermark_path(content_root: Path) -> Path:
    return content_root / ".harvest" / "kb-drift-scan-watermark.json"


def cache_dir(content_root: Path) -> Path:
    return content_root / ".harvest" / "kb-drift-scan-cache"


def memo_unprocessed_dir(content_root: Path) -> Path:
    return content_root / "artefacts" / "memo" / ".unprocessed"


def decisions_path(content_root: Path) -> Path:
    return content_root / "kb" / "decisions.md"


# ---------------------------------------------------------------------
# Normalization (shared with kb-scan's NFKD strategy for unicode names)
# ---------------------------------------------------------------------


def normalize(s: str) -> str:
    """Lowercase, NFKD-fold, collapse to alphanumerics + spaces. Same shape
    as kb-scan.normalize so scope tokens match memory tags by the same rules
    even when accents or punctuation differ between sources."""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", errors="ignore").decode("ascii")
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------------
# Memory loading — frontmatter + content_hash
# ---------------------------------------------------------------------


def parse_memory(path: Path) -> Optional[MemoryObject]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[kb-drift-scan] WARN: cannot read {path}: {exc}", file=sys.stderr)
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    fm_block = text[4:end]
    body = text[end + 4:].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_block)
    except yaml.YAMLError as exc:
        print(f"[kb-drift-scan] WARN: bad frontmatter in {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(fm, dict):
        return None
    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    return MemoryObject(
        path=path,
        source_kind=str(fm.get("source_kind", path.parent.name)),
        memory_id=str(fm.get("id", path.stem)),
        created_at=str(fm.get("created_at", "")),
        tags=tuple(str(t) for t in tags),
        title=str(fm.get("title", "")),
        summary=str(fm.get("summary", "")),
        body=body,
        content_hash=content_hash,
    )


def load_memory(content_root: Path, since: Optional[str]) -> list[MemoryObject]:
    memory_root = content_root / "memory"
    out: list[MemoryObject] = []
    if not memory_root.is_dir():
        return out
    for source_dir in sorted(memory_root.iterdir()):
        if not source_dir.is_dir():
            continue
        for path in sorted(source_dir.glob("*.md")):
            mo = parse_memory(path)
            if mo is None:
                continue
            if since and mo.created_at and mo.created_at < since:
                continue
            out.append(mo)
    return out


# ---------------------------------------------------------------------
# Decision loading — parse ## sections from kb/decisions.md
# ---------------------------------------------------------------------


def load_decisions(content_root: Path) -> list[DecisionEntry]:
    """Parse `## <title>` sections from kb/decisions.md. Skips:
      - Sections whose title contains <...> placeholders (template/schema docs).
      - Sections without a `**Scope:**` line (drift can't route — pre-#133 seed
        entries fall here; v1 limits drift detection to scoped decisions).
      - Sections without a `via=art-<uuid>` tag in produced_by (no canonical
        artefact to anchor `affects_decision: art://<...>`).
    """
    path = decisions_path(content_root)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    sections = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    out: list[DecisionEntry] = []
    for sec in sections:
        if not sec.startswith("## "):
            continue
        first_nl = sec.find("\n")
        if first_nl < 0:
            continue
        title_line = sec[3:first_nl]
        title = title_line.strip()
        if "<" in title and ">" in title:
            # Schema/template heading like `## <Decision title>`.
            continue
        scope_match = DECISION_SCOPE_RE.search(sec)
        if not scope_match:
            continue
        scope = scope_match.group("scope").strip()
        if not scope:
            continue
        via_match = DECISION_VIA_RE.search(sec)
        if not via_match:
            continue
        via_uuid = via_match.group("via")
        # Normalize the section before hashing so trailing whitespace at the
        # file boundary doesn't make the LAST section's hash dependent on
        # adjacent sections. Without this, appending a new decision at the
        # end flips the previously-last section's hash and invalidates its
        # cache spuriously (per pr-challenger MEDIUM finding on PR #144).
        normalized = sec.rstrip()
        text_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
        out.append(DecisionEntry(
            art_id=via_uuid,
            title=title,
            scope=scope,
            text=normalized,
            text_hash=text_hash,
        ))
    return out


# ---------------------------------------------------------------------
# Routing — Scope intersection
# ---------------------------------------------------------------------


# Tokens too generic to gate on alone. `team` matches half the memory pool;
# `nyc` is short but locality-signaling. Threshold: token len ≥ 4 OR token
# is one of the curated short-but-distinctive keywords.
SHORT_DISTINCTIVE_TOKENS = {"hr", "ip", "qa", "ml", "ai", "ux"}


def memory_mentions_scope(memory: MemoryObject, scope: str) -> bool:
    """True when the memory plausibly relates to the decision's Scope.

    Tag match takes precedence (semantically tagged → high signal). Body
    keyword match falls back to word-boundary search on each scope token of
    length ≥4 (short tokens are noise unless on the distinctive list)."""
    norm_scope = normalize(scope)
    if not norm_scope:
        return False
    norm_tags = {normalize(t) for t in memory.tags if t}
    norm_tags.discard("")
    # Direct or token-overlap tag match.
    scope_tokens = set(norm_scope.split())
    for tag in norm_tags:
        if tag == norm_scope:
            return True
        tag_tokens = set(tag.split())
        if scope_tokens & tag_tokens:
            return True
    # Body fallback — word-boundary on each ≥4-char scope token.
    norm_haystack = normalize(memory.title + " " + memory.summary + " " + memory.body)
    for tok in scope_tokens:
        if len(tok) >= 4 or tok in SHORT_DISTINCTIVE_TOKENS:
            if re.search(rf"\b{re.escape(tok)}\b", norm_haystack):
                return True
    return False


def build_pairs(
    memories: list[MemoryObject],
    decisions: list[DecisionEntry],
) -> list[DriftPair]:
    """Cartesian intersection: only (memory, decision) pairs where Scope
    appears in the memory survive. This is the F3 quota guard — a leaky
    intersection means routing is broken and the cap becomes the only thing
    standing between us and a 7k-pair brute-force."""
    pairs: list[DriftPair] = []
    for mem in memories:
        for dec in decisions:
            if memory_mentions_scope(mem, dec.scope):
                pairs.append(DriftPair(mem, dec))
    return pairs


# ---------------------------------------------------------------------
# Cache (per-pair, two-hash invalidation)
# ---------------------------------------------------------------------


def cache_filename(pair: DriftPair) -> str:
    """Cache key: <memory-id>-<decision-art-id>-<sha8(body||text)>.json.
    Both hashes go into one composite hash so EITHER edit triggers a miss
    (F5: decision-edit invalidation, plus the standard body-edit case)."""
    composite = pair.memory.content_hash + "|" + pair.decision.text_hash
    composite_hash = hashlib.sha256(composite.encode("utf-8")).hexdigest()[:8]
    return f"{pair.memory.memory_id}-art-{pair.decision.art_id}-{composite_hash}.json"


def cache_read(content_root: Path, pair: DriftPair) -> Optional[dict]:
    p = cache_dir(content_root) / cache_filename(pair)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cache_write(content_root: Path, pair: DriftPair, payload: dict) -> None:
    """Atomic write: tmp + rename so a crash mid-write doesn't leave a partial
    cache file (matches kb-scan precedent — pr-challenger #120 finding)."""
    cache_dir(content_root).mkdir(parents=True, exist_ok=True)
    final = cache_dir(content_root) / cache_filename(pair)
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(final)


# ---------------------------------------------------------------------
# LLM (claude -p)
# ---------------------------------------------------------------------


DRIFT_PROMPT = """You are checking whether a recent memory excerpt indicates DRIFT from a previously-recorded user decision.

DRIFT means the memory contains evidence the decision is no longer accurate, has been superseded, or was never as committed as the kb entry implies. Examples of drift:
  - Decision: "We're going with Polestar in H2." Memory: "Drop Polestar from H2; Acko replaces them."
  - Decision: "Leonor leads Atlas in Q3." Memory: "Leonor moved to Compass; Tomás takes Atlas."
  - Decision: "Fraud-prevention starts as messaging only." Memory: "Decided to add transaction caps after reviewing the data."

NOT drift (skip these):
  - The memory simply mentions the decision's referent without contradicting it.
  - The memory is consistent with the decision (reinforces, executes, or refines without changing direction).
  - The memory describes a tangentially-related event in the same scope but doesn't speak to the decision.

Output ONLY YAML in this exact shape (no markdown fences, no commentary):

```
drifted: true | false
drift_confidence: high | medium | low
drift_claim: <one short sentence stating what changed and why it constitutes drift; empty string when drifted=false>
verbatim_excerpt: <a verbatim substring of the memory body, ≥10 chars and ≤300 chars, that grounds the drift_claim. MUST be copy-paste from the memory body. Empty string when drifted=false.>
reasoning: <one sentence explaining the verdict; brief.>
```

Be conservative. False positives waste the user's review budget. If you cannot quote a verbatim excerpt grounding the drift, set drifted=false.

=== DECISION ===
Title: {DECISION_TITLE}
Scope: {DECISION_SCOPE}

{DECISION_TEXT}

=== MEMORY ===
ID: {MEMORY_ID} (source: {MEMORY_SOURCE})
Title: {MEMORY_TITLE}

{MEMORY_BODY}
"""


def call_claude(prompt: str) -> str:
    """Same shape as kb-scan.call_claude. Headless `claude -p`, returns stdout."""
    result = subprocess.run(
        ["claude", "-p", prompt],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def strip_yaml_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def judge_drift(pair: DriftPair) -> Optional[DriftVerdict]:
    """One claude -p call. Returns parsed DriftVerdict or None on parse error."""
    body_excerpt = pair.memory.body[:MEMORY_BODY_CAP]
    prompt = (
        DRIFT_PROMPT
        .replace("{DECISION_TITLE}", pair.decision.title)
        .replace("{DECISION_SCOPE}", pair.decision.scope)
        .replace("{DECISION_TEXT}", pair.decision.text)
        .replace("{MEMORY_ID}", pair.memory.memory_id)
        .replace("{MEMORY_SOURCE}", pair.memory.source_kind)
        .replace("{MEMORY_TITLE}", pair.memory.title)
        .replace("{MEMORY_BODY}", body_excerpt)
    )
    try:
        raw = call_claude(prompt)
    except subprocess.CalledProcessError as exc:
        print(
            f"[kb-drift-scan] WARN: claude -p failed for ({pair.memory.memory_id}, "
            f"art-{pair.decision.art_id}): {exc}",
            file=sys.stderr,
        )
        return None
    try:
        parsed = yaml.safe_load(strip_yaml_fences(raw))
    except yaml.YAMLError as exc:
        print(
            f"[kb-drift-scan] WARN: claude -p returned bad YAML for "
            f"({pair.memory.memory_id}, art-{pair.decision.art_id}): {exc}",
            file=sys.stderr,
        )
        return None
    if not isinstance(parsed, dict):
        return None
    return DriftVerdict(
        drifted=bool(parsed.get("drifted", False)),
        drift_claim=str(parsed.get("drift_claim", "")).strip(),
        drift_confidence=str(parsed.get("drift_confidence", "low")).strip().lower(),
        verbatim_excerpt=str(parsed.get("verbatim_excerpt", "")).strip(),
        reasoning=str(parsed.get("reasoning", "")).strip(),
    )


# ---------------------------------------------------------------------
# F4 enforcement: verbatim excerpt is a substring of the memory body
# ---------------------------------------------------------------------


def excerpt_grounded(verdict: DriftVerdict, memory: MemoryObject) -> bool:
    """Return True iff verdict.verbatim_excerpt appears as a substring of the
    memory body, after collapsing whitespace and lowercasing both sides.

    F4 closer: drift_claim cites a phrase that does NOT appear in the source
    memory ⇒ provenance contract violated; reviewer can't audit. We can't
    catch every paraphrase, but a verbatim-excerpt check rejects the cleanest
    failure mode (LLM fabricates a quote)."""
    excerpt_norm = re.sub(r"\s+", " ", verdict.verbatim_excerpt).strip().lower()
    if len(excerpt_norm) < EXCERPT_MIN_LEN:
        return False
    body_norm = re.sub(r"\s+", " ", memory.body).lower()
    return excerpt_norm in body_norm


# ---------------------------------------------------------------------
# Memo emission — slice-1 schema
# ---------------------------------------------------------------------


def session_id_from_env() -> str:
    sid = os.environ.get("PA_SESSION_ID", "").strip()
    if re.match(r"^[0-9a-f]{8}$", sid):
        return sid
    return uuid.uuid4().hex[:8]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_drift_memo(
    content_root: Path,
    pair: DriftPair,
    verdict: DriftVerdict,
    *,
    session_id: str,
    query: str,
    out_dir: Optional[Path] = None,
) -> Path:
    """Write a drift candidate as a kind=memo artefact carrying the slice-1
    drift schema. Returns the written path."""
    target_dir = out_dir if out_dir is not None else memo_unprocessed_dir(content_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    art_uuid = str(uuid.uuid4())
    art_id = f"art-{art_uuid}"
    path = target_dir / f"{art_id}.md"

    fm: dict = {
        "id": art_id,
        "kind": "memo",
        "created_at": now_iso(),
        "title": f"Drift: {pair.decision.title[:80]}",
        "drift_candidate": True,
        "affects_decision": f"art://{pair.decision.art_id}",
        "drift_claim": verdict.drift_claim,
        "drift_confidence": verdict.drift_confidence,
        "produced_by": {
            "session_id": session_id,
            "query": query,
            "model": "claude-opus-4-7",
            "sources_cited": [
                f"mem://{pair.memory.memory_id}",
                f"art://{pair.decision.art_id}",
            ],
        },
    }
    fm_yaml = yaml.dump(fm, sort_keys=False, default_flow_style=False).strip()

    body_lines = [
        "---", fm_yaml, "---", "",
        f"## Drift candidate: {pair.decision.title}",
        "",
        f"**Affected decision**: art://{pair.decision.art_id} (scope: {pair.decision.scope})",
        "",
        "### Decision (current kb entry)",
        "",
        "> " + pair.decision.text.strip().replace("\n", "\n> "),
        "",
        f"### Memory (source: {pair.memory.source_kind})",
        "",
        f"- **id**: mem://{pair.memory.memory_id}",
        f"- **title**: {pair.memory.title}",
        f"- **created_at**: {pair.memory.created_at}",
        "",
        f"### Drift claim ({verdict.drift_confidence} confidence)",
        "",
        verdict.drift_claim,
        "",
        "### Grounding excerpt (verbatim from memory body)",
        "",
        "> " + verdict.verbatim_excerpt.replace("\n", "\n> "),
        "",
    ]
    if verdict.reasoning:
        body_lines.extend([
            "### LLM reasoning",
            "",
            verdict.reasoning,
            "",
        ])
    path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------


def read_watermark(content_root: Path) -> Optional[str]:
    p = watermark_path(content_root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    last = data.get("last_scan_at")
    return str(last) if isinstance(last, str) else None


def write_watermark(content_root: Path) -> None:
    p = watermark_path(content_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"last_scan_at": now_iso()}, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


def confidence_meets_threshold(conf: str, threshold: str) -> bool:
    return CONFIDENCE_RANK.get(conf, 0) >= CONFIDENCE_RANK.get(threshold, 0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--all", action="store_true", help="ignore watermark; scan full memory pool (bootstrap)")
    p.add_argument("--since", help="ISO timestamp override; mutually exclusive with --all")
    p.add_argument("--skip-llm", action="store_true", help="dry-run: routing only, no claude -p (testing)")
    p.add_argument("--max-llm-calls", type=int, default=DEFAULT_MAX_LLM_CALLS,
                   help=f"hard cap on claude -p invocations (F3 quota guard, default {DEFAULT_MAX_LLM_CALLS})")
    p.add_argument("--threshold", choices=("high", "medium", "low"),
                   default=DEFAULT_CONFIDENCE_THRESHOLD,
                   help="minimum drift_confidence to emit (default medium)")
    p.add_argument("--out-dir", help="override unprocessed memo output dir (testing)")
    args = p.parse_args(argv)

    if args.all and args.since:
        print("--all and --since are mutually exclusive", file=sys.stderr)
        return 2

    cfg = load_config(require_explicit_content_root=True)
    content_root = cfg.content_root

    since: Optional[str] = None
    if not args.all:
        since = args.since or read_watermark(content_root)

    print(f"[kb-drift-scan] content_root={content_root}", file=sys.stderr)
    print(f"[kb-drift-scan] since={since or '<all>'}", file=sys.stderr)
    print(f"[kb-drift-scan] threshold={args.threshold}", file=sys.stderr)

    decisions = load_decisions(content_root)
    print(f"[kb-drift-scan] loaded {len(decisions)} scoped+anchored decision(s)", file=sys.stderr)
    if not decisions:
        print("[kb-drift-scan] no decisions in scope; updating watermark and exiting clean", file=sys.stderr)
        if not args.skip_llm:
            write_watermark(content_root)
        return 0

    memories = load_memory(content_root, since)
    print(f"[kb-drift-scan] loaded {len(memories)} memory object(s) in scope", file=sys.stderr)
    if not memories:
        print("[kb-drift-scan] no memory in scope; updating watermark and exiting clean", file=sys.stderr)
        if not args.skip_llm:
            write_watermark(content_root)
        return 0

    pairs = build_pairs(memories, decisions)
    print(f"[kb-drift-scan] phase 1: {len(pairs)} (memory, decision) pairs after Scope intersection", file=sys.stderr)
    # Fan-out diagnostic: top scopes by pair count. Helps operators see when
    # an over-broad Scope value is producing a thousand-pair pile-up before
    # the cap fires.
    if pairs:
        by_scope: dict[str, int] = defaultdict(int)
        for p in pairs:
            by_scope[p.decision.scope] += 1
        top = sorted(by_scope.items(), key=lambda kv: kv[1], reverse=True)[:5]
        if top:
            top_str = ", ".join(f"{s}={n}" for s, n in top)
            print(f"[kb-drift-scan] top scopes by pair fan-out: {top_str}", file=sys.stderr)

    if args.skip_llm:
        print("[kb-drift-scan] --skip-llm: stopping after routing", file=sys.stderr)
        return 0

    session_id = session_id_from_env()
    out_dir = Path(args.out_dir) if args.out_dir else memo_unprocessed_dir(content_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    query = f"kb-drift-scan {('--all' if args.all else f'--since={since}')}"

    emit_count = 0
    llm_calls = 0
    skipped_for_quota = 0
    cached_pairs = 0
    rejected_for_grounding = 0
    below_threshold = 0
    not_drifted = 0

    for pair in pairs:
        cached = cache_read(content_root, pair)
        if cached is not None:
            verdict_data = cached.get("verdict")
            cached_pairs += 1
        else:
            if llm_calls >= args.max_llm_calls:
                skipped_for_quota += 1
                continue
            verdict = judge_drift(pair)
            llm_calls += 1
            if verdict is None:
                # Cache the failure shape so we don't retry on the same
                # (memory, decision) tuple until either side changes.
                cache_write(content_root, pair, {
                    "memory_id": pair.memory.memory_id,
                    "decision_art_id": pair.decision.art_id,
                    "scanned_at": now_iso(),
                    "verdict": None,
                })
                continue
            verdict_data = dataclasses.asdict(verdict)
            cache_write(content_root, pair, {
                "memory_id": pair.memory.memory_id,
                "decision_art_id": pair.decision.art_id,
                "scanned_at": now_iso(),
                "verdict": verdict_data,
            })

        if not verdict_data:
            continue
        verdict = DriftVerdict(**verdict_data)
        if not verdict.drifted:
            not_drifted += 1
            continue
        if not confidence_meets_threshold(verdict.drift_confidence, args.threshold):
            below_threshold += 1
            continue
        if not excerpt_grounded(verdict, pair.memory):
            rejected_for_grounding += 1
            print(
                f"[kb-drift-scan] WARN: rejected drift verdict for "
                f"({pair.memory.memory_id}, art-{pair.decision.art_id}) — "
                f"verbatim_excerpt not found in memory body (F4 grounding floor)",
                file=sys.stderr,
            )
            continue

        path = emit_drift_memo(
            content_root, pair, verdict,
            session_id=session_id, query=query,
            out_dir=out_dir if args.out_dir else None,
        )
        # When out_dir is overridden (tests), emit_drift_memo writes there directly.
        emit_count += 1

    # Watermark gate (matches kb-scan): don't advance if quota was hit, so the
    # next default run doesn't silently skip the un-scanned pairs.
    if skipped_for_quota == 0:
        write_watermark(content_root)
    else:
        print(
            f"[kb-drift-scan] WARN: quota exhausted with {skipped_for_quota} pairs skipped; "
            f"watermark NOT advanced. Re-run with --max-llm-calls > {args.max_llm_calls} "
            f"or split with --since to clear the backlog.",
            file=sys.stderr,
        )

    summary = (
        f"[kb-drift-scan] emitted {emit_count} drift candidate(s); "
        f"llm_calls={llm_calls}, cached={cached_pairs}, "
        f"not_drifted={not_drifted}, below_threshold={below_threshold}, "
        f"rejected_for_grounding={rejected_for_grounding}"
    )
    if skipped_for_quota:
        summary += f", skipped_for_quota={skipped_for_quota}"
    print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"[kb-drift-scan] ERROR: {e}", file=sys.stderr)
        sys.exit(1)
