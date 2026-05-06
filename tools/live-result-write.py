#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Write a live-fetched raw artifact to the vault (#39-B.1).

The skill orchestrates the live MCP call (Granola / Slack / Gmail) and
pipes the resulting body to this helper, which writes it to a stable
location alongside harvest-fetched artifacts and emits a `live_call_end`
metric event so the dashboard's `live_calls_per_query` stays accurate.

Filename convention:
    <content_root>/raw/<source_kind>/live-<utc-ts>-<query-hash>.md

Query hash: first 8 chars of sha256(query). Bounds the filename and lets
us correlate multiple calls about the same query without colliding.

Body format:
    <!-- live-fetched on <iso-utc> for query <repr> -->
    <body from MCP>

The leading HTML comment is invisible to markdown rendering, parseable
by compress.py for provenance, and survives content-only diffs.

Usage:
    cat granola_findings.md | tools/live-result-write.py \\
        --source granola_note --query "what's new with Acko Projects?"

Exit codes:
    0   wrote file successfully
    1   bad args (unknown --source, missing --query)
    2   could not write (permissions, missing content_root)
    3   stdin was empty (refuse to write zero-byte artifacts)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402
from _metrics import emit, inherit_or_start  # noqa: E402

VALID_SOURCES = {"granola_note", "slack_thread", "gmail_thread"}
QUERY_HASH_LEN = 8


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()[:QUERY_HASH_LEN]


def utc_ts() -> str:
    """UTC timestamp safe for filenames (no colons)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def write_live_artifact(
    *,
    source: str,
    query: str,
    body: str,
    content_root: Path,
    now_iso: str | None = None,
    now_filename: str | None = None,
) -> Path:
    """Pure write: no env touching, no metrics emit. Returns the path written."""
    if source not in VALID_SOURCES:
        raise ValueError(f"unknown source {source!r}; valid: {sorted(VALID_SOURCES)}")
    if not query.strip():
        raise ValueError("query must be non-empty")
    if not body.strip():
        raise ValueError("body must be non-empty (refusing to write zero-byte artifact)")

    h = query_hash(query)
    ts_file = now_filename or utc_ts()
    iso = now_iso or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

    target_dir = content_root / "raw" / source
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"live-{ts_file}-{h}.md"

    # Leading HTML comment carries provenance for compress.py + auditors.
    # Repr of the query escapes quotes/newlines safely.
    comment = f"<!-- live-fetched on {iso} for query {query!r} (#39-B) -->"
    target.write_text(f"{comment}\n{body}\n", encoding="utf-8")
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write a live-fetched artifact to <content_root>/raw/<source>/.")
    parser.add_argument("--source", required=True, choices=sorted(VALID_SOURCES),
                        help="source kind; matches harvest naming so compress.py + dedup behave consistently")
    parser.add_argument("--query", required=True, help="user query that drove the live call (used for hash + provenance)")
    args = parser.parse_args(argv[1:])

    body = sys.stdin.read()
    if not body.strip():
        print("[live-result-write] empty stdin; refusing to write zero-byte artifact", file=sys.stderr)
        return 3

    cfg = load_config()
    inherit_or_start()

    try:
        target = write_live_artifact(
            source=args.source,
            query=args.query,
            body=body,
            content_root=cfg.content_root,
        )
    except OSError as e:
        print(f"[live-result-write] write failed: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"[live-result-write] {e}", file=sys.stderr)
        return 1

    bytes_written = target.stat().st_size
    emit(
        "live_call_end",
        source=args.source,
        query_hash=query_hash(args.query),
        bytes_written=bytes_written,
        path_relative=str(target.relative_to(cfg.content_root)),
    )
    print(str(target))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
