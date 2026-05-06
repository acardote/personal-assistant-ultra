"""Event-emission library for personal-assistant instrumentation (#41).

Append-only JSON-lines log of structured events at
`<content_root>/.metrics/events-YYYY-MM-DD.jsonl`. Every other tool calls into
this module to emit query/harvest/retrieval events; aggregation
(`tools/metrics-aggregate.py`) and dashboard (`tools/metrics-dashboard.py`)
read the same files.

Design constraints (from #41):

- **<5% query-latency overhead**. Best effort: stdlib only, no network, no
  dependency on `_config.py` for hot-path emit (which would parse JSON and
  do filesystem checks). The library uses an env-var-resolved metrics dir
  with a fallback search to be cheap on the common path.
- **<50ms per event**. The append-and-fsync pattern is sub-millisecond
  on local disk; we don't fsync.
- **Privacy: no raw query text** stored. Topic keywords are bounded
  (max 5 per event, max 32 chars each, lowercased). All other fields are
  numeric / categorical / structured metadata.
- **Crash-safe**. Every event flushes its line; partial writes are bounded
  to the current event (the next emit appends a new complete line).
- **Append-only**. The aggregator reads files in append-only fashion;
  no event is ever rewritten.

Schema (one event per line):
    {
        "ts": "2026-05-06T14:23:00Z",
        "session_id": "<8-char-hex>",
        "event": "query_start" | "query_end" | "kb_load" | "memory_retrieve"
                  | "live_call" | "writeback" | "harvest_start" | "harvest_end"
                  | "harvest_source" | "skill_emit",
        "duration_ms": <int> (optional, only on _end events),
        "data": { ... event-specific structured fields ... }
    }

Sessions: `start_session()` returns an 8-char-hex session id. Subsequent
`emit()` calls with `session_id=None` use the current session. The session
id can also be propagated across processes via the `PA_SESSION_ID` env var
(set by the calling shell / parent tool).

Locating the metrics dir:
    1. `$PA_METRICS_DIR` env var (explicit override)
    2. `$PA_CONTENT_ROOT/.metrics/` env var
    3. `_config.load_config().harvest_state_root.parent / ".metrics"` (slow path)
    4. Fallback: `~/.personal-assistant/metrics/` (so the tools never crash)

The slow path is only taken when neither env var is set; the calling tools
should set `PA_METRICS_DIR` once at module-import time to avoid repeated
filesystem walks.

If the metrics dir cannot be created or written to, `emit()` silently
discards events. Instrumentation must NEVER crash the calling tool.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Bounded keyword count + length, per #41 privacy contract.
MAX_KEYWORDS = 5
MAX_KEYWORD_LEN = 32

# Cache the resolved metrics dir at first use; if None, the resolver is run.
_METRICS_DIR: Path | None = None
_SESSION_ID: str | None = None


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_metrics_dir() -> Path | None:
    """Locate or create `.metrics/`. Returns None if unable (caller silently
    drops events)."""
    # 1. Explicit override
    env_dir = os.environ.get("PA_METRICS_DIR")
    if env_dir:
        p = Path(env_dir).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            return None

    # 2. Content root from env
    env_root = os.environ.get("PA_CONTENT_ROOT")
    if env_root:
        p = Path(env_root).expanduser() / ".metrics"
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            return None

    # 3. Slow path: ask _config (lazy import to avoid circular deps + cost)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _config import load_config  # type: ignore
        cfg = load_config(require_explicit_content_root=False)
        p = cfg.harvest_state_root.parent / ".metrics"
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            return None
    except Exception:
        # Final fallback; never let instrumentation crash the host tool.
        p = Path.home() / ".personal-assistant" / "metrics"
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            return None


def _get_metrics_dir() -> Path | None:
    global _METRICS_DIR
    if _METRICS_DIR is None:
        _METRICS_DIR = _resolve_metrics_dir()
    return _METRICS_DIR


def _today_path() -> Path | None:
    md = _get_metrics_dir()
    if md is None:
        return None
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    return md / f"events-{today}.jsonl"


def start_session(session_id: str | None = None) -> str:
    """Begin a new session. Returns the session id (8 hex chars). If a
    session id is provided (e.g. from a parent process via env), reuses it."""
    global _SESSION_ID
    if session_id:
        _SESSION_ID = session_id
    elif os.environ.get("PA_SESSION_ID"):
        _SESSION_ID = os.environ["PA_SESSION_ID"]
    else:
        _SESSION_ID = secrets.token_hex(4)
    # Propagate to child processes
    os.environ["PA_SESSION_ID"] = _SESSION_ID
    return _SESSION_ID


def get_session_id() -> str:
    """Return current session id; lazily start one if unset."""
    global _SESSION_ID
    if _SESSION_ID is None:
        # Honor inherited env first
        if os.environ.get("PA_SESSION_ID"):
            _SESSION_ID = os.environ["PA_SESSION_ID"]
        else:
            start_session()
    return _SESSION_ID  # type: ignore[return-value]


def _sanitize_keywords(kws: Any) -> list[str]:
    """Bound + lowercase + trim keyword list per privacy contract."""
    if not isinstance(kws, (list, tuple)):
        return []
    out: list[str] = []
    for k in kws:
        if not isinstance(k, str):
            continue
        k = k.strip().lower()[:MAX_KEYWORD_LEN]
        if k:
            out.append(k)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def emit(event: str, *, duration_ms: int | None = None, **data: Any) -> bool:
    """Append one event to today's events file. Returns True on success.

    Instrumentation must never crash the host tool — any error is swallowed
    and the function returns False. Use the return value only for testing.

    `event`: short event-type identifier (see schema in module docstring).
    `duration_ms`: optional integer, only meaningful on `*_end` events.
    `**data`: structured event-specific fields. `topic_keywords` is sanitized
    if present (bounded + lowercased).
    """
    path = _today_path()
    if path is None:
        return False

    if "topic_keywords" in data:
        data["topic_keywords"] = _sanitize_keywords(data["topic_keywords"])

    payload: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "session_id": get_session_id(),
        "event": event,
    }
    if duration_ms is not None:
        payload["duration_ms"] = int(duration_ms)
    if data:
        payload["data"] = data

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except OSError:
        # Disk full, perm error, dir gone — silently drop.
        return False


@contextmanager
def time_event(event: str, **data: Any):
    """Context manager that emits two events: `<event>_start` and `<event>_end`,
    with `duration_ms` on the end event.

    Inside the context, the caller can set/extend keys on the yielded dict
    (`tracker`); those keys land on the `_end` event's data. The `_start`
    event is emitted first with whatever data was passed in.

    Example:
        with time_event("memory_retrieve", topic_keywords=["acko"]) as t:
            results = retrieve(...)
            t["memory_hits"] = len(results)
        # emits memory_retrieve_start (with topic_keywords)
        # emits memory_retrieve_end (with topic_keywords, memory_hits, duration_ms)
    """
    start = time.monotonic()
    emit(f"{event}_start", **data)
    tracker: dict[str, Any] = dict(data)
    try:
        yield tracker
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        emit(f"{event}_end", duration_ms=duration_ms, **tracker)
