#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Thin CLI wrapper around `tools/_metrics.py:emit()`.

For use from shell scripts, routine prompts, and other places where importing
the Python module isn't convenient. Each invocation emits one event.

Usage:
    tools/log-event.py <event> [--duration-ms N] [--data key=value ...]
    tools/log-event.py harvest_start --data scheduler=routine cold_start=true
    tools/log-event.py memory_retrieve_end --duration-ms 1234 --data memory_hits=3

Values in `--data key=value` are parsed as JSON if possible (so you can pass
booleans, numbers, lists, etc. quoted as JSON), else as strings. Lists for
`topic_keywords` are auto-split on comma if not JSON-shaped.

Exits 0 on success, 1 on emission failure (e.g., metrics dir not writable).
The host caller should not block on this exit code — instrumentation is
best-effort by design.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _metrics import emit  # noqa: E402


def parse_value(raw: str):
    """Parse a value as JSON if possible, else return as-is string."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Emit one personal-assistant metrics event.")
    parser.add_argument("event", help="Event type (e.g. query_start, harvest_end).")
    parser.add_argument("--duration-ms", type=int, default=None,
                        help="Duration in milliseconds (only meaningful on _end events).")
    parser.add_argument("--data", action="append", default=[],
                        help="key=value data field; can repeat. Values parse as JSON if possible.")
    args = parser.parse_args(argv[1:])

    data: dict = {}
    for kv in args.data:
        if "=" not in kv:
            print(f"[log-event] skipping malformed --data {kv!r} (expected key=value)", file=sys.stderr)
            continue
        k, _, v = kv.partition("=")
        k = k.strip()
        if k == "topic_keywords":
            # Comma-split if it's not already JSON-shaped
            if v.strip().startswith("["):
                try:
                    data[k] = json.loads(v)
                    continue
                except json.JSONDecodeError:
                    pass
            data[k] = [t.strip() for t in v.split(",") if t.strip()]
        else:
            data[k] = parse_value(v)

    ok = emit(args.event, duration_ms=args.duration_ms, **data)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
