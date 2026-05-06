#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Write a live-fetched raw artifact to the vault (#39-B.1).

The skill orchestrates the live MCP call (Granola / Slack / Gmail) and
pipes the resulting body to this helper, which writes it to a stable
location and emits a `live_call_end` metric event.

## Path scheme

    <content_root>/raw/live/<source_kind>/<utc-ts>-<query-hash>.md

The **separate `raw/live/` subtree** is load-bearing (per pr-challenger
C1/C2 on PR #53): it keeps live artifacts away from harvest's per-source
dirs so harvest's compress + dedup paths don't accidentally pick them
up. #39-D's write-back pipeline will walk `raw/live/` explicitly when
folding live findings into memory with provenance preserved.

Until #39-D lands, files in `raw/live/<source>/` accumulate without
flowing to memory — that's the documented trade-off, not a bug.

## Filename uniqueness

Timestamp uses millisecond precision (`%Y-%m-%dT%H-%M-%S-%fmsZ`) so
same-second retries don't collide. Query hash is the first 8 chars of
sha256(query) — a 32-bit space, so birthday-collision becomes likely
around ~65k distinct queries (years of headroom at typical rates, but
documented).

## Body format

    <!-- live-fetched on <iso> for query <repr> (#39-B) -->
    <body from MCP>

The leading HTML comment carries provenance for compress.py + auditors.
**Note**: this comment quotes the user query verbatim, which means
`raw/live/` files inherit the same privacy posture as harvest's `raw/`
artifacts (they contain user content). The metrics events file remains
PII-filtered per `_metrics.py`'s denylist.

## Event semantics (per pr-challenger C3 on PR #53)

A single `live_call_end` event with a `status` field, not a sibling
`live_call_error`. Status ∈ {success, empty, error, timeout}. The
helper emits status=success on a successful write, status=empty when
stdin was empty (still emits — was a silent skip before). The skill
emits status=error / status=timeout when the MCP call fails, so every
`live_call_start` always pairs with exactly one `live_call_end`.

## --start-iso

When the skill emits `live_call_start` it captures the start ISO ts and
passes it back via `--start-iso <ts>`. The helper computes duration_ms
and includes it on `live_call_end`. This pairs the start/end at the
helper-emit site so latency aggregation is robust to the skill
emitting the start but not the end on transient failures.

Usage:
    cat granola_findings.md | tools/live-result-write.py \\
        --source granola_note --query "what's new with Acko Projects?" \\
        --start-iso 2026-05-06T13:14:00.123Z

Exit codes:
    0   wrote file successfully
    1   bad args (unknown --source, missing --query)
    2   could not write (permissions, missing content_root)
    3   stdin was empty (refuse to write zero-byte artifact, but emit
        live_call_end with status=empty so the call is still observable)
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
    """UTC timestamp with millisecond precision, safe for filenames (no colons)."""
    now = _dt.datetime.now(_dt.timezone.utc)
    # Truncate microseconds to milliseconds (3 digits) for readability.
    return now.strftime("%Y-%m-%dT%H-%M-%S-") + f"{now.microsecond // 1000:03d}msZ"


def _parse_iso_to_ms(iso: str) -> _dt.datetime:
    """Parse the start ISO ts. Accepts 'Z' suffix or explicit offset."""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(iso)


def compute_duration_ms(start_iso: str, end_dt: _dt.datetime) -> int | None:
    """Return ms elapsed between start_iso and end_dt, or None on parse failure."""
    try:
        start = _parse_iso_to_ms(start_iso)
    except ValueError:
        return None
    delta = end_dt - start
    return max(0, int(delta.total_seconds() * 1000))


def write_live_artifact(
    *,
    source: str,
    query: str,
    body: str,
    content_root: Path,
    now_iso: str | None = None,
    now_filename: str | None = None,
) -> Path:
    """Pure write: no env touching, no metrics emit. Returns the path written.

    Path scheme: `<content_root>/raw/live/<source>/<ts>-<hash>.md`. The
    `raw/live/` separation is intentional — keeps live artifacts away
    from harvest's per-source dirs.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"unknown source {source!r}; valid: {sorted(VALID_SOURCES)}")
    if not query.strip():
        raise ValueError("query must be non-empty")
    if not body.strip():
        raise ValueError("body must be non-empty (refusing to write zero-byte artifact)")

    h = query_hash(query)
    ts_file = now_filename or utc_ts()
    iso = now_iso or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")

    target_dir = content_root / "raw" / "live" / source
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{ts_file}-{h}.md"

    # Leading HTML comment carries provenance for compress.py + auditors.
    comment = f"<!-- live-fetched on {iso} for query {query!r} (#39-B) -->"
    target.write_text(f"{comment}\n{body}\n", encoding="utf-8")
    return target


def _emit_live_call_end(
    *,
    source: str,
    q_hash: str,
    status: str,
    bytes_written: int = 0,
    path_relative: str | None = None,
    duration_ms: int | None = None,
) -> None:
    payload: dict = {
        "source": source,
        "query_hash": q_hash,
        "status": status,
        "bytes_written": bytes_written,
    }
    if path_relative is not None:
        payload["path_relative"] = path_relative
    emit("live_call_end", duration_ms=duration_ms, **payload)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write a live-fetched artifact to <content_root>/raw/live/<source>/.")
    parser.add_argument("--source", required=True, choices=sorted(VALID_SOURCES),
                        help="source kind; matches harvest naming so #39-D dedup behaves consistently")
    parser.add_argument("--query", required=True,
                        help="user query that drove the live call (used for hash + provenance)")
    parser.add_argument("--start-iso", default=None,
                        help="ISO timestamp of when the skill emitted live_call_start; "
                             "if provided, helper computes duration_ms on live_call_end")
    args = parser.parse_args(argv[1:])

    body = sys.stdin.read()
    cfg = load_config()
    inherit_or_start()
    h = query_hash(args.query)
    now_dt = _dt.datetime.now(_dt.timezone.utc)
    duration_ms = compute_duration_ms(args.start_iso, now_dt) if args.start_iso else None

    if not body.strip():
        # Emit live_call_end with status=empty so the call is observable.
        # Pre-#53-fixup, this exit-3'd silently — biasing dashboards.
        _emit_live_call_end(source=args.source, q_hash=h, status="empty",
                            duration_ms=duration_ms)
        print("[live-result-write] empty stdin; refusing to write zero-byte artifact", file=sys.stderr)
        return 3

    try:
        target = write_live_artifact(
            source=args.source,
            query=args.query,
            body=body,
            content_root=cfg.content_root,
        )
    except OSError as e:
        _emit_live_call_end(source=args.source, q_hash=h, status="error",
                            duration_ms=duration_ms)
        print(f"[live-result-write] write failed: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        _emit_live_call_end(source=args.source, q_hash=h, status="error",
                            duration_ms=duration_ms)
        print(f"[live-result-write] {e}", file=sys.stderr)
        return 1

    bytes_written = target.stat().st_size
    _emit_live_call_end(
        source=args.source, q_hash=h, status="success",
        bytes_written=bytes_written,
        path_relative=str(target.relative_to(cfg.content_root)),
        duration_ms=duration_ms,
    )
    print(str(target))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
