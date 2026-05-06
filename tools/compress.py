#!/usr/bin/env -S uv run --quiet --with jsonschema --with pyyaml --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4", "pyyaml>=6"]
# ///
"""Compress a raw artifact (layer 1) into a memory object (layer 2).

Usage:
    tools/compress.py raw/examples/2026-q2-platform-strategy.md
    tools/compress.py raw/slack/T0123/abc.md --kind thread --source-kind slack_thread
    tools/compress.py raw/legal/contract-2026.md --kind legal  # expires_at => null

The compression backend invokes `claude -p` headlessly so the user's existing
Claude Code authentication is reused (no API key required). The editorial-
judgment prompt lives at `tools/prompts/compress.md`.

Outputs a memory object to `memory/<auto-derived-path>` and prints its path.
Validates the output against `docs/schemas/memory-object.schema.json` and
fails (exit 1) if the model produced an invalid file.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402
from _metrics import emit, time_event, inherit_or_start  # noqa: E402

_CFG = load_config()
METHOD_ROOT = _CFG.method_root
SCHEMA_PATH = METHOD_ROOT / "docs" / "schemas" / "memory-object.schema.json"
PROMPT_PATH = METHOD_ROOT / "tools" / "prompts" / "compress.md"
RAW_ROOT = _CFG.raw_root
MEMORY_ROOT = _CFG.memory_root
PROJECT_ROOT = METHOD_ROOT  # legacy alias for path-display helpers in this file

# Default expires_at windows by kind — operationalized in #8 but the
# compression pipeline is the natural write-time author of these dates.
EXPIRY_DAYS_BY_KIND: dict[str, int | None] = {
    "strategy": 180,
    "weekly": 90,
    "retrospective": 180,
    "thread": 90,
    "note": 90,
    "decision": None,        # never expires by default
    "legal": None,
    "glossary_term": None,
}


def call_claude(prompt: str, raw_text: str) -> str:
    """Invoke `claude -p` headlessly and return its stdout."""
    full_prompt = (
        f"{prompt}\n\n"
        "=== RAW DOCUMENT BEGIN ===\n"
        f"{raw_text}\n"
        "=== RAW DOCUMENT END ===\n"
    )
    result = subprocess.run(
        ["claude", "-p", full_prompt],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def parse_memo_output(text: str) -> tuple[dict, str]:
    """Strip optional code fences and parse frontmatter + body.

    Returns (frontmatter_dict, body_str). Raises ValueError on shape mismatch.
    """
    stripped = text.strip()

    # Strip ```markdown ... ``` or ``` ... ``` fences if the model wrapped it.
    fence_match = re.match(r"^```(?:\w+)?\s*\n(.*?)\n```\s*$", stripped, re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1).strip()

    if not stripped.startswith("---\n"):
        raise ValueError(
            "model output does not start with YAML frontmatter ('---\\n'). "
            f"First 200 chars: {stripped[:200]!r}"
        )
    parts = stripped.split("\n---\n", 1)
    if len(parts) != 2:
        raise ValueError("frontmatter not closed with '\\n---\\n' delimiter")

    front_text = parts[0][4:]  # strip leading '---\n'
    body = parts[1].lstrip("\n")
    front = yaml.safe_load(front_text)
    if not isinstance(front, dict):
        raise ValueError("frontmatter is not a YAML mapping")
    return front, body


def render_memo(front: dict, body: str) -> str:
    """Render frontmatter + body to a Markdown file string. Uses block-style
    YAML and quotes datetime-shaped strings to avoid PyYAML auto-conversion
    on round-trip."""
    front_yaml = yaml.safe_dump(
        front,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=120,
    )
    return f"---\n{front_yaml}---\n\n{body.rstrip()}\n"


def derive_memory_path(raw_path: Path, source_kind: str, *, provenance: str | None = None) -> Path:
    """Mirror the layer-1 path under memory/, preserving subdirectory structure.

    When `provenance == "live"`, the raw path is `raw/live/<source>/<file>` and
    we strip the `live/` segment so the memory object lands alongside
    harvest-fetched memory at `memory/<source>/<file>` (per #39-D). This lets
    the existing #10 event-id dedup catch the same meeting/thread across
    harvest and live pipelines without separate dedup state.
    """
    rel = raw_path.relative_to(RAW_ROOT)
    if provenance == "live" and rel.parts and rel.parts[0] == "live":
        rel = Path(*rel.parts[1:])
    return MEMORY_ROOT / rel


def count_tokens(text: str) -> int:
    """Character-based token estimate. See tools/_tokens.py for rationale."""
    from _tokens import estimate_tokens

    return estimate_tokens(text)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Compress a raw artifact into a memory object.")
    parser.add_argument("raw_path", help="Path to a layer-1 raw artifact (under raw/).")
    parser.add_argument(
        "--source-kind",
        default="doc",
        help="source_kind frontmatter value (default: doc).",
    )
    parser.add_argument(
        "--kind",
        default=None,
        help="Override kind frontmatter value (else: model picks from recommended set).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output path. Defaults to memory/<same-relative-path-as-raw>.",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=800,
        help="Soft body token budget; warn (not fail) if exceeded.",
    )
    parser.add_argument(
        "--provenance",
        default=None,
        choices=[None, "harvest", "live"],
        help="Provenance flag for the memory object's frontmatter (#39-D). "
             "When 'live', also strips the `live/` segment from the raw path "
             "when deriving the memory location, so #10 event-id dedup can "
             "catch the same event across harvest and live pipelines.",
    )
    args = parser.parse_args(argv[1:])

    raw_path = Path(args.raw_path).resolve()
    if not raw_path.exists():
        print(f"raw path does not exist: {raw_path}", file=sys.stderr)
        return 2
    try:
        rel_to_raw = raw_path.relative_to(RAW_ROOT)
    except ValueError:
        print(f"raw_path must live under {RAW_ROOT}", file=sys.stderr)
        return 2

    out_path = Path(args.out).resolve() if args.out else derive_memory_path(
        raw_path, args.source_kind, provenance=args.provenance,
    )

    raw_text = raw_path.read_text(encoding="utf-8")
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def _disp(p: Path) -> str:
        try:
            return str(p.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(p)
    print(f"compressing {_disp(raw_path)} -> {_disp(out_path)} ...", file=sys.stderr)

    # Inherit parent's session id (e.g., from harvest routine) so all compress
    # calls in one harvest run group together.
    inherit_or_start()

    # `compress` covers the LLM call (the slow part); the trailing
    # `compress_result` event carries post-dedup, post-validation outcome
    # data that isn't available until later in the function. Aggregator can
    # join the two events on session_id + relative timestamp.
    with time_event("compress", source_kind=args.source_kind, raw_chars=len(raw_text)) as ct:
        output = call_claude(prompt, raw_text)
        ct["output_chars"] = len(output)
    front, body = parse_memo_output(output)

    # Script-authored fields override whatever the model emitted.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    front["id"] = f"mem-{uuid.uuid4()}"
    front["source_uri"] = f"file:./raw/{rel_to_raw.as_posix()}"
    front["source_kind"] = args.source_kind
    front["created_at"] = now_iso
    # Provenance lets the dashboard / #10 dedup distinguish a memory object
    # that came from the live-call path (#39-B) from one that came from the
    # scheduled harvest. Absent when --provenance not passed (back-compat).
    if args.provenance:
        front["provenance"] = args.provenance

    if args.kind:
        front["kind"] = args.kind
    kind = front.get("kind")
    if not isinstance(kind, str) or not kind:
        print("model did not emit a non-empty 'kind' and no --kind override given", file=sys.stderr)
        return 1

    # Default expires_at if model didn't supply one (or supplied null).
    if "expires_at" not in front or front["expires_at"] in (None, "null", ""):
        days = EXPIRY_DAYS_BY_KIND.get(kind, 90)
        if days is None:
            front["expires_at"] = None
        else:
            from datetime import timedelta
            expiry = datetime.now(timezone.utc) + timedelta(days=days)
            front["expires_at"] = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")

    if "tags" not in front or front["tags"] is None:
        front["tags"] = []

    # Multi-fidelity event clustering (per #10). Runs before write so the
    # newly-landed memo carries event_id / is_canonical_for_event / superseded_by
    # at write time, and any displaced canonical gets demoted in the same pass.
    import dedup as _dedup
    dedup_cfg = _dedup.load_config()
    new_summary = _dedup.MemoSummary(
        id=front["id"],
        path=out_path,
        source_kind=front["source_kind"],
        created_at=datetime.fromisoformat(front["created_at"].replace("Z", "+00:00"))
            if isinstance(front["created_at"], str)
            else front["created_at"],
        body_tokens=_dedup.tokenize(body),
        event_id=None,
        is_canonical_for_event=False,
        superseded_by=None,
    )
    if new_summary.created_at.tzinfo is None:
        new_summary.created_at = new_summary.created_at.replace(tzinfo=timezone.utc)
    corpus = [m for m in _dedup.load_corpus(MEMORY_ROOT) if m.id != new_summary.id and m.path != out_path]
    cluster = _dedup.cluster_with_existing(new_summary, corpus, dedup_cfg)
    front["event_id"] = cluster.event_id
    front["is_canonical_for_event"] = cluster.role == "canonical"
    if cluster.role == "alternate":
        # Find current canonical to point at via superseded_by.
        members = _dedup.cluster_members(corpus, cluster.event_id)
        flagged = next((m for m in members if m.is_canonical_for_event), None)
        canonical = flagged or (
            _dedup.pick_canonical(members, dedup_cfg) if members else None
        )
        front["superseded_by"] = canonical.id if canonical else None
    else:
        front["superseded_by"] = None

    rendered = render_memo(front, body)

    # Validate before writing.
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    schema_errors = sorted(validator.iter_errors(front), key=lambda e: list(e.absolute_path))
    if schema_errors:
        for err in schema_errors:
            loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
            print(f"validation error at '{loc}': {err.message}", file=sys.stderr)
        print("---rendered output for debugging---", file=sys.stderr)
        print(rendered, file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")

    # If the new memo displaced an existing canonical, rewrite the demoted
    # memo's frontmatter so it reflects its alternate status.
    if cluster.demoted_id:
        for m in corpus:
            if m.id == cluster.demoted_id:
                d_front, d_body = _dedup.parse_memo_file(m.path)
                d_front["is_canonical_for_event"] = False
                d_front["superseded_by"] = front["id"]
                d_front["event_id"] = cluster.event_id  # ensure cluster membership
                m.path.write_text(render_memo(d_front, d_body), encoding="utf-8")
                print(f"[dedup] demoted previous canonical: {m.path}", file=sys.stderr)
                break

    # If the cluster was seeded by a legacy memo (one that had no prior
    # event_id), backfill the seed's event_id on disk so cluster membership
    # is queryable from frontmatter alone (per reviewer C1 on PR #20).
    # Skip this when the seed is already the demoted canonical we just rewrote.
    if cluster.seeded_id and cluster.seeded_id != cluster.demoted_id:
        for m in corpus:
            if m.id == cluster.seeded_id:
                s_front, s_body = _dedup.parse_memo_file(m.path)
                if not s_front.get("event_id"):
                    s_front["event_id"] = cluster.event_id
                    # The seed was the cluster's de-facto canonical until
                    # this new memo arrived; preserve that role unless the
                    # new memo took it.
                    if cluster.role == "alternate":
                        s_front.setdefault("is_canonical_for_event", True)
                        s_front.setdefault("superseded_by", None)
                    m.path.write_text(render_memo(s_front, s_body), encoding="utf-8")
                    print(f"[dedup] backfilled event_id on seed: {m.path}", file=sys.stderr)
                break

    print(
        f"[dedup] event_id={cluster.event_id[:12]}... role={cluster.role} score={cluster.score:.3f}",
        file=sys.stderr,
    )

    # Soft-warn on token budget (rough char-based estimate, see tools/_tokens.py).
    body_tokens = count_tokens(body)
    print(f"body tokens: {body_tokens} (budget {args.token_budget})", file=sys.stderr)
    over_budget = body_tokens > args.token_budget
    if over_budget:
        print(
            f"WARNING: body {body_tokens} tokens exceeds budget {args.token_budget}",
            file=sys.stderr,
        )

    # Compress_result carries post-LLM, post-validation outcome only. Fields
    # that overlap with `compress_end` (source_kind) are NOT repeated — the
    # aggregator joins on session_id + recency. Only emits NEW signal:
    # the resulting kind, body token count, budget violation, dedup role.
    emit(
        "compress_result",
        kind=kind,
        body_tokens=body_tokens,
        over_budget=over_budget,
        cluster_role=cluster.role,
    )

    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
