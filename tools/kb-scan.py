#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""kb-scan.py — autonomous KB candidate detector (per parent #116, child #119).

Walks `<vault>/memory/<source>/*.md`, aggregates per-kind candidates against
the editorial-rules thresholds (per `docs/kb-editorial-rules.md`), and emits
each as a `kind=memo` artefact under `<vault>/artefacts/memo/.unprocessed/`.

Detection methods (v1):
  - person + org: tag-based aggregation. >=2 memory objects from >=2 distinct
    sources matching the same tag. One `claude -p` call per surviving
    candidate to synthesize a proposed kb entry. Self-exclusion list +
    alias-aware existing-heading filter (F4 closer).
  - decision: parse `## What was decided / what is true` section per memory.
    One `claude -p` call per memory body containing the section. LLM filters
    trivial decisions; emits one candidate per material decision.
  - glossary-term: frequency on capitalized noun phrases absent from
    `<method>/kb/glossary.md`. Threshold >=3 distinct memory objects. No LLM.

Watermark + cache:
  - `<vault>/.harvest/kb-scan-watermark.json` — last-scan ISO + per-source
    last-processed memory id list.
  - `<vault>/.harvest/kb-scan-cache/<memory-id>-<sha8>.json` — cached LLM
    output keyed by (memory-id, content-hash). Body edits invalidate the
    cache (F5 closer).

Per ADR-0003 F2 (autonomous-producer carve-out): this tool emits `kind=memo`
artefacts only. It MUST NOT write to `<vault>/kb/*` directly. The kb-process
slash command (slice 3 of #116) consumes the unprocessed memos via the
standard diff-and-approve flow.
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

PERSON_ORG_THRESHOLD_SOURCES = 2  # >= N distinct sources required
GLOSSARY_THRESHOLD_MEMORIES = 3   # >= N distinct memory objects required
DEFAULT_SOURCES = ("granola_note", "slack_thread", "slack_dm", "gmail_thread")
SYNTHESIS_BODIES_CAP = 5  # top-N memory bodies per candidate fed to LLM
DECISION_SECTION_HEADING = "## What was decided / what is true"

# Common-English words we'd never want emitted as glossary candidates.
# Conservative — bias toward emit-and-let-user-reject ONLY for project-shape
# multi-word terms; v1 disables glossary emission by default (--enable-glossary
# flag for opt-in) because pure-frequency on capitalized tokens produces too
# much noise (proper-noun-shaped narrative words, single-name mentions, etc.).
GLOSSARY_STOPWORDS = {
    # Days / months
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "apr", "feb", "jan", "mar", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    # Common pronouns / function words that get capitalized at sentence start
    "the", "and", "or", "but", "if", "then", "else", "when", "where", "why",
    "i", "you", "we", "they", "he", "she", "it", "this", "that", "what",
    "which", "whether", "who", "whom", "how",
    # Numbers / quantifiers
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "all", "many", "some", "few", "next", "new", "current", "open",
    # Common project nouns that ARE in glossary or shouldn't be
    "decision", "action", "scope", "risk", "team", "owner", "timeline",
    "weekly", "meeting", "channel", "data", "video", "platform", "growth",
    "product", "exact", "load", "bearing",
    # Non-noun affirmations
    "ok", "okay", "yes", "no", "todo", "done", "tbd", "fyi", "etc", "not",
}

# Capitalized noun-phrase regex — naive but catches the common shapes:
#   `Acko`, `Atlas`, `Polestar`, `WaymoRobotaxi`, `J. Miller`
# Multi-word capitalized phrases handled by allowing internal whitespace.
NOUN_PHRASE_RE = re.compile(r"\b[A-Z][A-Za-z0-9][A-Za-z0-9\-]{1,30}\b")


# ---------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MemoryObject:
    path: Path
    source_kind: str
    memory_id: str
    created_at: str
    tags: list[str]
    title: str
    summary: str
    body: str
    content_hash: str  # SHA-256 of body, first 8 hex


@dataclasses.dataclass
class Candidate:
    kind: str  # person | org | decision | glossary
    referent: str
    sources_cited: list[str]  # mem://<id>
    summary: str
    proposed_diff: str  # markdown rendering of the proposed kb entry


# ---------------------------------------------------------------------
# Config-paths helpers
# ---------------------------------------------------------------------


def watermark_path(content_root: Path) -> Path:
    return content_root / ".harvest" / "kb-scan-watermark.json"


def cache_dir(content_root: Path) -> Path:
    return content_root / ".harvest" / "kb-scan-cache"


def memo_unprocessed_dir(content_root: Path) -> Path:
    return content_root / "artefacts" / "memo" / ".unprocessed"


# ---------------------------------------------------------------------
# Memory loading + frontmatter parsing
# ---------------------------------------------------------------------


def parse_memory(path: Path) -> Optional[MemoryObject]:
    """Return MemoryObject or None on parse failure (skipped silently with
    stderr warning — kb-scan is best-effort over a possibly-large pool)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[kb-scan] WARN: cannot read {path}: {exc}", file=sys.stderr)
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
        print(f"[kb-scan] WARN: bad frontmatter in {path}: {exc}", file=sys.stderr)
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
        tags=[str(t) for t in tags],
        title=str(fm.get("title", "")),
        summary=str(fm.get("summary", "")),
        body=body,
        content_hash=content_hash,
    )


def load_memory(content_root: Path, since: Optional[str]) -> list[MemoryObject]:
    """Walk content_root/memory/<source>/*.md. Filter by since if provided
    (ISO8601 string compared lexically against created_at, which works for
    Z-suffixed UTC timestamps)."""
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
# KB heading extraction (for the existing-heading filter)
# ---------------------------------------------------------------------


# Headings that are template placeholders, NOT real entries.
TEMPLATE_HEADINGS = {
    "<name or handle>",
    "<org / unit / team name>",
    "<decision title>",
    "<term>",
}


def normalize(s: str) -> str:
    """Lowercase, strip, drop non-alphanumeric chars (except spaces collapsed
    to single space). Used for heading + tag matching."""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_h2_headings(path: Path) -> set[str]:
    """Return normalized set of `## <heading>` strings from a markdown file.
    Skips template placeholder headings. Empty if file missing."""
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if not m:
            continue
        norm = normalize(m.group(1))
        if norm in {normalize(t) for t in TEMPLATE_HEADINGS}:
            continue
        if not norm:
            continue
        out.add(norm)
    return out


def extract_kb_referents(content_root: Path, method_root: Path) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return (people, orgs, decisions, glossary) — each a set of normalized
    headings. people/org/decisions live in the vault; glossary in method."""
    people = extract_h2_headings(content_root / "kb" / "people.md")
    orgs = extract_h2_headings(content_root / "kb" / "org.md")
    decisions = extract_h2_headings(content_root / "kb" / "decisions.md")
    glossary = extract_h2_headings(method_root / "kb" / "glossary.md")
    return people, orgs, decisions, glossary


def kb_referent_matches(tag: str, kb_set: set[str]) -> bool:
    """Alias-aware match: tag normalized appears as substring of any KB
    heading, OR vice versa. Catches `leonor` ~ `leonor mendonca`. Lossy
    direction is intentional — we want to filter aggressively to avoid
    duplicate candidates."""
    norm_tag = normalize(tag)
    if not norm_tag:
        return True  # empty tag, treat as match (filter out)
    for heading in kb_set:
        if norm_tag == heading:
            return True
        # token-level substring: tag is a token of heading or vice versa
        if norm_tag in heading.split():
            return True
        if heading in norm_tag.split():
            return True
    return False


# ---------------------------------------------------------------------
# Self-exclusion (F4 closer)
# ---------------------------------------------------------------------


# Tags that are universally excluded — generic project terms that appear in
# tags but are not person/org candidates, plus the user's own first-name
# tag (kb-scan also reads people.md's existing headings to catch the
# canonical form, but tagging often uses bare first names).
SELF_EXCLUDE_TAGS_DEFAULT = {
    "andre",  # vault owner first-name (André Cardote)
    "nexar",  # operating org
    # Project-language / topic-shape tags — NOT person/org candidates.
    # Conservative list; the LLM synthesis prompt also filters via `skip`,
    # so missing entries here just cost extra LLM calls, not bad emits.
    "meeting", "scheduling", "onboarding", "evaluation",
    "sales", "partnership", "partnerships", "product", "delivery",
    "legal", "pilot", "edge-cases", "edge cases", "data-collection",
    "data collection", "enterprise-growth", "enterprise growth",
    "video-search", "signal-validation", "video-processing",
    "gm-delivery", "sow", "kickoff", "integration", "commercial",
    "co-marketing", "co marketing", "deck review", "staging", "revenue",
    "pricing", "infrastructure", "launch", "insurance", "annotation",
    "firmware", "mapping", "world model", "data platform", "off road data",
    "av data", "driver monitoring", "ar glasses", "3d reconstruction",
    "san francisco", "vru", "vsa", "bd", "dms", "oauth", "gcp", "gcs",
    "aws", "nda", "gtm", "c staff", "product strategy", "bruno method",
    "nap",  # internal acronym, not org
}


# ---------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------


def aggregate_tags(
    memories: list[MemoryObject],
    *,
    self_exclude: set[str],
    people: set[str],
    orgs: set[str],
) -> dict[str, list[MemoryObject]]:
    """Build {tag: [memory]} for tags meeting the >=2-distinct-sources rule
    AND not matching an existing KB heading AND not in the self-exclude list."""
    by_tag: dict[str, list[MemoryObject]] = defaultdict(list)
    for mo in memories:
        for tag in mo.tags:
            by_tag[normalize(tag)].append(mo)

    surviving: dict[str, list[MemoryObject]] = {}
    for norm_tag, mos in by_tag.items():
        if not norm_tag or norm_tag in self_exclude:
            continue
        if kb_referent_matches(norm_tag, people) or kb_referent_matches(norm_tag, orgs):
            continue
        sources = {m.source_kind for m in mos}
        if len(sources) < PERSON_ORG_THRESHOLD_SOURCES:
            continue
        # De-dup mos by memory_id (a memory might have the tag duplicated).
        seen = set()
        deduped = []
        for m in mos:
            if m.memory_id in seen:
                continue
            seen.add(m.memory_id)
            deduped.append(m)
        surviving[norm_tag] = deduped
    return surviving


def extract_decision_section(body: str) -> Optional[str]:
    """Return the body text of the `## What was decided` section, or None
    if the section is missing or empty."""
    idx = body.find(DECISION_SECTION_HEADING)
    if idx == -1:
        return None
    section_start = idx + len(DECISION_SECTION_HEADING)
    next_h2 = body.find("\n## ", section_start)
    if next_h2 == -1:
        section = body[section_start:]
    else:
        section = body[section_start:next_h2]
    section = section.strip()
    return section if section else None


def aggregate_glossary(
    memories: list[MemoryObject],
    *,
    glossary: set[str],
    person_tags: set[str],
    org_tags: set[str],
) -> dict[str, list[MemoryObject]]:
    """Frequency of capitalized noun phrases across distinct memory objects.
    Returns {phrase: [memory]} for phrases meeting threshold + filters."""
    by_phrase: dict[str, list[MemoryObject]] = defaultdict(list)
    seen_per_phrase: dict[str, set[str]] = defaultdict(set)
    for mo in memories:
        haystack = f"{mo.title}\n{mo.summary}\n{mo.body}"
        for m in NOUN_PHRASE_RE.finditer(haystack):
            phrase = m.group(0)
            norm = normalize(phrase)
            if norm in GLOSSARY_STOPWORDS or len(norm) < 3:
                continue
            if mo.memory_id in seen_per_phrase[norm]:
                continue
            seen_per_phrase[norm].add(mo.memory_id)
            by_phrase[norm].append(mo)

    out: dict[str, list[MemoryObject]] = {}
    for norm, mos in by_phrase.items():
        if len(mos) < GLOSSARY_THRESHOLD_MEMORIES:
            continue
        if kb_referent_matches(norm, glossary):
            continue
        # Avoid double-emit with person/org candidates.
        if norm in person_tags or norm in org_tags:
            continue
        out[norm] = mos
    return out


# ---------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------


def cache_read(content_root: Path, memory_id: str, content_hash: str) -> Optional[dict]:
    """Cache hit on (memory-id, content-hash). Mismatched hash → miss
    (F5 closer: body edits invalidate cache)."""
    p = cache_dir(content_root) / f"{memory_id}-{content_hash}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cache_write(content_root: Path, memory_id: str, content_hash: str, payload: dict) -> None:
    cache_dir(content_root).mkdir(parents=True, exist_ok=True)
    p = cache_dir(content_root) / f"{memory_id}-{content_hash}.json"
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------
# LLM synthesis (claude -p)
# ---------------------------------------------------------------------


PERSON_ORG_PROMPT = """You are proposing a knowledge-base entry candidate from harvested memory excerpts.

Given the memory excerpts below about a referent (the tag `{TAG}`), propose ONE of:
- A `person` entry (if the tag names a person — first name, last name, or handle).
- An `org` entry (if the tag names a team, company, or external organization).
- `skip` (if the signal is too weak, ambiguous, or the referent isn't actually a person/org — e.g., it's a project name, technical term, or status word).

Output ONLY YAML in this exact shape (no markdown fences, no commentary):

```
kind: person | org | skip
title: <proposed `## <heading>` exactly as it would appear>
role_or_relation: <one line, ≤80 chars>
summary: <one paragraph, ≤200 words. Captures what the memory excerpts say
  about this referent that would help an assistant answer questions about
  them. Cite specific facts. Avoid speculation.>
skip_reason: <only when kind=skip — one short sentence>
```

Be conservative. If the tag is ambiguous, skip. If the memory excerpts don't
mention the referent meaningfully, skip. False positives are worse than
false negatives — the user reviews every emitted candidate.
"""


DECISION_PROMPT = """You are extracting durable decisions from a memory's `## What was decided` section.

A "durable decision" is a commitment, choice, or policy the user (or their
team) made that should be remembered going forward. Examples:
- "We decided to drop Polestar from the H2 customer list."
- "Leonor will lead the Atlas team starting Q3."
- "We're going with Option 2 for the live-call architecture."

NOT durable decisions (skip these):
- Action items / to-dos that are just task assignments.
- Status updates ("video pipeline is at 80%").
- Single-meeting agreements that don't outlive the meeting.
- Small tactical choices that don't shape future work.

Output ONLY YAML — a list of decisions found, possibly empty. No markdown
fences, no commentary:

```
decisions:
  - title: <short ≤60-char title>
    body: <≤80 words: what was decided + why>
    referent: <person/org/team most directly affected, if any>
```

If no durable decisions are present, output:

```
decisions: []
```
"""


def call_claude(prompt: str) -> str:
    """Invoke `claude -p` headlessly and return stdout. Same pattern as
    compress.py."""
    result = subprocess.run(
        ["claude", "-p", prompt],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def strip_yaml_fences(text: str) -> str:
    """Strip ```yaml ... ``` fences if present."""
    text = text.strip()
    if text.startswith("```"):
        # Drop first line (fence) and last line (fence)
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def synthesize_person_org(tag: str, mos: list[MemoryObject]) -> Optional[dict]:
    """One claude -p call. Returns parsed YAML dict or None on skip/error."""
    excerpts = []
    for mo in mos[:SYNTHESIS_BODIES_CAP]:
        excerpt = (
            f"--- memory: {mo.memory_id} (source: {mo.source_kind}) ---\n"
            f"title: {mo.title}\n"
            f"summary: {mo.summary}\n\n"
            f"{mo.body[:1500]}"  # Cap body to keep prompt bounded.
        )
        excerpts.append(excerpt)
    full_prompt = (
        PERSON_ORG_PROMPT.replace("{TAG}", tag)
        + "\n\n=== MEMORY EXCERPTS ===\n\n"
        + "\n\n".join(excerpts)
    )
    try:
        raw = call_claude(full_prompt)
    except subprocess.CalledProcessError as exc:
        print(f"[kb-scan] WARN: claude -p failed for tag {tag!r}: {exc}", file=sys.stderr)
        return None
    try:
        parsed = yaml.safe_load(strip_yaml_fences(raw))
    except yaml.YAMLError as exc:
        print(f"[kb-scan] WARN: claude -p returned bad YAML for tag {tag!r}: {exc}", file=sys.stderr)
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("kind") not in {"person", "org"}:
        return None  # skip
    return parsed


def extract_decisions(mo: MemoryObject) -> list[dict]:
    """One claude -p per memory (with cached). Returns list of decision dicts."""
    section = extract_decision_section(mo.body)
    if not section:
        return []
    full_prompt = (
        DECISION_PROMPT
        + "\n\n=== DECISION SECTION ===\n\n"
        + f"From memory: {mo.memory_id} (source: {mo.source_kind})\n"
        + f"Title: {mo.title}\n\n"
        + section
    )
    try:
        raw = call_claude(full_prompt)
    except subprocess.CalledProcessError as exc:
        print(f"[kb-scan] WARN: claude -p failed for memory {mo.memory_id}: {exc}", file=sys.stderr)
        return []
    try:
        parsed = yaml.safe_load(strip_yaml_fences(raw))
    except yaml.YAMLError as exc:
        print(f"[kb-scan] WARN: claude -p returned bad YAML for memory {mo.memory_id}: {exc}", file=sys.stderr)
        return []
    if not isinstance(parsed, dict):
        return []
    decisions = parsed.get("decisions") or []
    return decisions if isinstance(decisions, list) else []


# ---------------------------------------------------------------------
# Memo emission
# ---------------------------------------------------------------------


def session_id_from_env() -> str:
    sid = os.environ.get("PA_SESSION_ID", "").strip()
    if re.match(r"^[0-9a-f]{8}$", sid):
        return sid
    # Mint a routine session.
    return uuid.uuid4().hex[:8]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_memo(content_root: Path, candidate: Candidate, session_id: str, query: str) -> Path:
    """Write a candidate as a kind=memo artefact. Returns path."""
    out_dir = memo_unprocessed_dir(content_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    art_uuid = str(uuid.uuid4())
    art_id = f"art-{art_uuid}"
    path = out_dir / f"{art_id}.md"

    fm: dict = {
        "id": art_id,
        "kind": "memo",
        "created_at": now_iso(),
        "title": f"Candidate {candidate.kind}: {candidate.referent}",
        "produced_by": {
            "session_id": session_id,
            "query": query,
            "model": "claude-opus-4-7",
            "sources_cited": candidate.sources_cited,
        },
    }
    fm_yaml = yaml.dump(fm, sort_keys=False, default_flow_style=False).strip()

    body_lines = [
        "---",
        fm_yaml,
        "---",
        "",
        f"## Candidate {candidate.kind}: {candidate.referent}",
        "",
        f"**Source memory objects**: {len(candidate.sources_cited)}",
        "",
        f"**Path**: scan-driven (per editorial-rules amendment in #117).",
        "",
        f"**Summary**:",
        "",
        candidate.summary,
        "",
        "## Proposed diff",
        "",
        candidate.proposed_diff,
        "",
        "## Sources",
        "",
    ]
    for src in candidate.sources_cited:
        body_lines.append(f"- {src}")
    path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    return path


def render_person_org_diff(syn: dict) -> str:
    """Render the LLM's synthesis output as the proposed-diff section."""
    title = syn.get("title", "(missing title)")
    role = syn.get("role_or_relation", "")
    summary = syn.get("summary", "")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return (
        f"```diff\n"
        f"+ ## {title}\n"
        f"+ - **Role / relation:** {role}\n"
        f"+ - **Last verified:** {today}\n"
        f"+ - **Expires:** never (refresh on role/org change)\n"
        f"+ - **Source:** scan-driven candidate from kb-scan\n"
        f"+ \n"
        f"+ {summary}\n"
        f"```"
    )


def render_decision_diff(dec: dict, mo: MemoryObject) -> str:
    title = dec.get("title", "(missing title)")
    body = dec.get("body", "")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return (
        f"```diff\n"
        f"+ ## {title}\n"
        f"+ - **Date:** {today}\n"
        f"+ - **Status:** decided\n"
        f"+ - **Last verified:** {today}\n"
        f"+ - **Expires:** never\n"
        f"+ - **Source:** mem://{mo.memory_id} ({mo.source_kind})\n"
        f"+ \n"
        f"+ {body}\n"
        f"```"
    )


def render_glossary_diff(phrase: str, mos: list[MemoryObject]) -> str:
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return (
        f"```diff\n"
        f"+ ## {phrase}\n"
        f"+ - **Last verified:** {today}\n"
        f"+ - **Source:** scan-driven candidate (≥{GLOSSARY_THRESHOLD_MEMORIES} memory mentions)\n"
        f"+ \n"
        f"+ <Definition needed: this term appears in {len(mos)} memory objects "
        f"but isn't yet defined in glossary.md. Define it or skip the candidate.>\n"
        f"```"
    )


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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--all", action="store_true", help="ignore watermark; scan full memory pool (bootstrap)")
    p.add_argument("--since", help="ISO timestamp override; mutually exclusive with --all")
    p.add_argument("--skip-llm", action="store_true", help="dry-run: aggregate + filter only, no claude -p calls (testing)")
    p.add_argument("--enable-glossary", action="store_true", help="emit glossary candidates (v1 default OFF — pure-frequency detection produces too much noise; needs follow-up tuning)")
    p.add_argument("--max-llm-calls", type=int, default=200, help="hard cap on claude -p invocations; aborts when exceeded (F3 quota guard, default 200)")
    p.add_argument("--out-dir", help="override unprocessed memo output dir (testing)")
    args = p.parse_args(argv)

    cfg = load_config(require_explicit_content_root=True)
    content_root = cfg.content_root
    method_root = cfg.method_root

    if args.all and args.since:
        print("--all and --since are mutually exclusive", file=sys.stderr)
        return 2

    since: Optional[str] = None
    if not args.all:
        since = args.since or read_watermark(content_root)

    print(f"[kb-scan] content_root={content_root}", file=sys.stderr)
    print(f"[kb-scan] since={since or '<all>'}", file=sys.stderr)

    memories = load_memory(content_root, since)
    print(f"[kb-scan] loaded {len(memories)} memory objects in scope", file=sys.stderr)

    if not memories:
        print("[kb-scan] no memory in scope; updating watermark and exiting clean", file=sys.stderr)
        if not args.skip_llm:
            write_watermark(content_root)
        return 0

    people, orgs, decisions_kb, glossary = extract_kb_referents(content_root, method_root)
    self_exclude = set(SELF_EXCLUDE_TAGS_DEFAULT)

    # Phase 1: tag aggregation (deterministic, no LLM).
    surviving_tags = aggregate_tags(
        memories, self_exclude=self_exclude, people=people, orgs=orgs,
    )
    print(f"[kb-scan] phase 1: {len(surviving_tags)} surviving person/org tag candidates", file=sys.stderr)

    # Phase 1b: glossary frequency (deterministic, no LLM).
    person_tag_set = set(surviving_tags.keys())
    org_tag_set = set(surviving_tags.keys())  # tag aggregation doesn't pre-classify; downstream LLM does
    surviving_glossary = aggregate_glossary(
        memories, glossary=glossary,
        person_tags=person_tag_set, org_tags=org_tag_set,
    )
    print(f"[kb-scan] phase 1b: {len(surviving_glossary)} surviving glossary candidates", file=sys.stderr)

    if args.skip_llm:
        print("[kb-scan] --skip-llm: stopping after phase 1 (no candidates emitted)", file=sys.stderr)
        return 0

    session_id = session_id_from_env()
    out_dir = Path(args.out_dir) if args.out_dir else memo_unprocessed_dir(content_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    emit_count = 0
    llm_calls = 0
    skipped_for_quota = 0

    # Phase 2: per-tag synthesis.
    for tag, mos in surviving_tags.items():
        if llm_calls >= args.max_llm_calls:
            skipped_for_quota += len(surviving_tags)  # rough; logged below
            break
        sources_cited = [f"mem://{m.memory_id}" for m in mos]
        syn = synthesize_person_org(tag, mos)
        llm_calls += 1
        if syn is None:
            continue
        kind = syn.get("kind")  # person | org
        candidate = Candidate(
            kind=kind,
            referent=syn.get("title") or tag,
            sources_cited=sources_cited,
            summary=syn.get("summary", ""),
            proposed_diff=render_person_org_diff(syn),
        )
        path = emit_memo(
            content_root, candidate, session_id,
            f"kb-scan {('--all' if args.all else f'--since={since}')}",
        )
        # When out_dir is overridden (tests), move the emitted file there.
        if args.out_dir:
            new_path = out_dir / path.name
            path.replace(new_path)
        emit_count += 1

    # Phase 3: per-memory decision extraction (cached).
    for mo in memories:
        if extract_decision_section(mo.body) is None:
            continue
        cached = cache_read(content_root, mo.memory_id, mo.content_hash)
        if cached is not None:
            decisions_extracted = cached.get("decisions", [])
        else:
            if llm_calls >= args.max_llm_calls:
                skipped_for_quota += 1
                continue
            decisions_extracted = extract_decisions(mo)
            llm_calls += 1
            cache_write(content_root, mo.memory_id, mo.content_hash, {
                "memory_id": mo.memory_id,
                "content_hash": mo.content_hash,
                "decisions": decisions_extracted,
                "scanned_at": now_iso(),
            })
        for dec in decisions_extracted:
            if not isinstance(dec, dict):
                continue
            title = dec.get("title")
            if not title:
                continue
            # Filter against existing decisions.md
            if kb_referent_matches(title, decisions_kb):
                continue
            candidate = Candidate(
                kind="decision",
                referent=title,
                sources_cited=[f"mem://{mo.memory_id}"],
                summary=dec.get("body", ""),
                proposed_diff=render_decision_diff(dec, mo),
            )
            path = emit_memo(
                content_root, candidate, session_id,
                f"kb-scan decision-extract from {mo.memory_id}",
            )
            if args.out_dir:
                new_path = out_dir / path.name
                path.replace(new_path)
            emit_count += 1

    # Phase 4: glossary candidates (no LLM). Disabled by default — emit-on-flag.
    if not args.enable_glossary:
        if surviving_glossary:
            print(
                f"[kb-scan] glossary detection produced {len(surviving_glossary)} "
                f"candidates but emission is OFF (run with --enable-glossary to "
                f"opt in; v1 detection is too noisy without follow-up tuning).",
                file=sys.stderr,
            )
        surviving_glossary = {}
    for phrase, mos in surviving_glossary.items():
        sources_cited = [f"mem://{m.memory_id}" for m in mos]
        candidate = Candidate(
            kind="glossary",
            referent=phrase,
            sources_cited=sources_cited,
            summary=f"Term `{phrase}` appears in {len(mos)} memory objects without an existing definition.",
            proposed_diff=render_glossary_diff(phrase, mos),
        )
        path = emit_memo(
            content_root, candidate, session_id,
            f"kb-scan glossary-frequency",
        )
        if args.out_dir:
            new_path = out_dir / path.name
            path.replace(new_path)
        emit_count += 1

    write_watermark(content_root)

    msg = f"[kb-scan] emitted {emit_count} candidate memo(s) to {out_dir} (llm_calls={llm_calls})"
    if skipped_for_quota:
        msg += f", skipped {skipped_for_quota} due to --max-llm-calls={args.max_llm_calls}"
    print(msg, file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"[kb-scan] ERROR: {e}", file=sys.stderr)
        sys.exit(2)
