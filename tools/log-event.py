#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Thin CLI wrapper around `tools/_metrics.py:emit()`.

For use from shell scripts, routine prompts, and other places where importing
the Python module isn't convenient. Each invocation emits one event.

Usage:
    tools/log-event.py <event> [--duration-ms N] [--data key=value ...] [--json-data key=value ...]
    tools/log-event.py harvest_start --data scheduler=routine --json-data cold_start=true
    tools/log-event.py memory_retrieve_end --duration-ms 1234 --json-data memory_hits=3

By default, `--data key=value` keeps the value as a string. To pass typed
values (numbers, booleans, lists), use `--json-data key=<json-literal>`. This
removes the type-coercion footgun in the original API where bare integers
were silently parsed as JSON.

Special-case: `topic_keywords` accepts a comma-separated string when passed
via `--data` (e.g., `--data topic_keywords=acko,pico,badas`).

Optional: `--inherit-session` makes the emit participate in the parent
process's session (via `PA_SESSION_ID` env var). Default is to start a fresh
session per invocation, avoiding cross-invocation env bleed.

Exits 0 on success, 1 on emission failure (e.g., metrics dir not writable).
The host caller should not block on this exit code — instrumentation is
best-effort by design.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

# Avoid sys.path mutation; load _metrics via spec.
_SPEC = importlib.util.spec_from_file_location(
    "_pa_metrics", str(Path(__file__).resolve().parent / "_metrics.py")
)
assert _SPEC is not None and _SPEC.loader is not None
_metrics = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_metrics)


def parse_string_value(raw: str, key: str):
    """Default value parser: keep as string, with comma-split for topic_keywords."""
    if key == "topic_keywords":
        return [t.strip() for t in raw.split(",") if t.strip()]
    return raw


def parse_json_value(raw: str, key: str):
    """JSON-typed value parser: parse as JSON literal, fall back to string."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Common case: bare token like `true` works, but `acko` is not JSON.
        return raw


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Emit one personal-assistant metrics event.")
    parser.add_argument("event", help="Event type (e.g. query_start, harvest_end).")
    parser.add_argument("--duration-ms", type=int, default=None,
                        help="Duration in milliseconds (only meaningful on _end events).")
    parser.add_argument("--data", action="append", default=[],
                        help="key=value data field (string-typed); can repeat. "
                             "Special: topic_keywords value is comma-split.")
    parser.add_argument("--json-data", action="append", default=[],
                        help="key=value data field (JSON-typed: numbers, bools, lists). "
                             "Can repeat. Falls back to string on JSON parse failure.")
    parser.add_argument("--inherit-session", action="store_true",
                        help="Inherit PA_SESSION_ID from env (default: fresh session per invocation).")
    args = parser.parse_args(argv[1:])

    # Session policy: default fresh, opt-in inherit.
    if args.inherit_session:
        _metrics.inherit_or_start()
    else:
        _metrics.start_session()

    data: dict = {}
    for kv in args.data:
        if "=" not in kv:
            print(f"[log-event] skipping malformed --data {kv!r} (expected key=value)", file=sys.stderr)
            continue
        k, _, v = kv.partition("=")
        k = k.strip()
        data[k] = parse_string_value(v, k)
    for kv in args.json_data:
        if "=" not in kv:
            print(f"[log-event] skipping malformed --json-data {kv!r} (expected key=value)", file=sys.stderr)
            continue
        k, _, v = kv.partition("=")
        k = k.strip()
        data[k] = parse_json_value(v, k)

    ok = _metrics.emit(args.event, duration_ms=args.duration_ms, **data)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
