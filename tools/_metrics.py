"""Event-emission library for personal-assistant instrumentation (#41).

Append-only JSON-lines log of structured events at
`<content_root>/.metrics/events-YYYY-MM-DD.jsonl`. Every other tool calls into
this module to emit query/harvest/retrieval events; aggregation
(`tools/metrics-aggregate.py`) and dashboard (`tools/metrics-dashboard.py`)
read the same files.

## Caller responsibility (privacy)

`emit(event, **data)` accepts arbitrary kwargs. The library bounds and
sanitizes `topic_keywords` and applies a per-line size cap, but **every
other field is the caller's responsibility**. The privacy contract:

- **Do not log raw query text.** Extract topic keywords (max 5, max 32
  chars each, lowercased) via simple keyword extraction; pass via
  `topic_keywords=`.
- **Do not log full file paths.** Use relative paths or basenames.
- **Do not log error messages or exception reprs.** Use error type
  names + structured failure codes.
- **Do not log user-identifying strings.** Email addresses, names, etc.

The library enforces:
- Bounded `topic_keywords` (max 5, max 32 chars each, lowercased).
- Hard line cap (`MAX_LINE_BYTES=4000`, drops events that exceed).
- Best-effort denylist on common PII field names (`raw_query`,
  `query_text`, `email`, `password`, `api_key`, `token`, `secret`).
  These keys are dropped from `**data` before serialization.
- Crash-safety: any unexpected exception in emit() is swallowed and
  returns False. Instrumentation MUST NEVER crash the host tool.

## Schema (one event per line)
    {
        "ts": "2026-05-06T14:23:00Z",
        "session_id": "<8-char-hex>",
        "event": "query_start" | "query_end" | "kb_load" | "memory_retrieve"
                  | "live_call" | "writeback" | "harvest_start" | "harvest_end"
                  | "harvest_source" | "skill_emit",
        "duration_ms": <int> (optional, only on _end events),
        "data": { ... event-specific structured fields ... }
    }

## Sessions

- `start_session()` always mints a fresh 8-hex-char id and overwrites
  `PA_SESSION_ID` env var. Top-level entry points (skill startup, harvest
  routine kickoff) should call this.
- `inherit_or_start()` honors an inherited `PA_SESSION_ID` if set; otherwise
  starts fresh. Child tool subprocesses use this to participate in the
  parent's session.
- `get_session_id()` is lazy: if no session has been started, it calls
  `start_session()` (i.e., fresh, NOT env-inherited). This avoids the
  env-bleed bug where two top-level invocations from the same shell
  silently share session ids.

## Locating the metrics dir
1. `$PA_METRICS_DIR` env var (explicit override)
2. `$PA_CONTENT_ROOT/.metrics/` env var
3. `_config.load_config().harvest_state_root.parent / ".metrics"` (slow path)
4. Fallback: `~/.personal-assistant/metrics/` (so the tools never crash)

The slow path uses `importlib.util.spec_from_file_location` for `_config`
loading instead of `sys.path.insert` to avoid global state mutation.

If the metrics dir cannot be created or written to, `emit()` silently
discards events and returns False.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Privacy / safety bounds.
MAX_KEYWORDS = 5
MAX_KEYWORD_LEN = 32
MAX_LINE_BYTES = 4000  # PIPE_BUF on most POSIX = 4096; stay under for atomic appends.

# Field names that almost certainly carry PII; dropped from **data unconditionally.
# Best-effort, not exhaustive — the privacy contract docstring puts the bar on
# callers, but this catches the common slip-ups.
PII_DENYLIST = frozenset({
    # Raw text fields
    "raw_query", "query_text", "query", "raw_text", "body", "content",
    "message", "prompt",
    # Identifiers
    "email", "email_address", "phone", "phone_number", "ssn", "name",
    "full_name", "username", "user_id", "user", "address", "ip",
    "ip_address", "dob", "birthdate",
    # Secrets
    "password", "api_key", "apikey", "token", "access_token",
    "refresh_token", "secret", "credential", "authorization", "auth",
    "cookie", "session_token",
})


def _safe_default(obj: Any) -> str:
    """json.dumps default= hook. Refuses to stringify exception objects (which
    can leak PII via stack traces / message content). Other unserializable
    types fall back to str(obj) bounded to a sane length.
    """
    if isinstance(obj, BaseException):
        # Surface only the type, never the message / args / __cause__.
        return f"<{type(obj).__name__}>"
    s = str(obj)
    return s[:512]  # bound any default-stringified value

# Cache the resolved metrics dir at first use; if None, the resolver is run.
_METRICS_DIR: Path | None = None
_SESSION_ID: str | None = None


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_valid_session_id(s: str) -> bool:
    """Reject session ids with whitespace, NUL, or non-ASCII (env-write safety)."""
    if not isinstance(s, str) or not s:
        return False
    return s.isascii() and s.isalnum() and len(s) <= 64


def _load_config_via_spec():
    """Import _config without sys.path mutation (per challenger-finding #5)."""
    config_path = Path(__file__).resolve().parent / "_config.py"
    if not config_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_pa_metrics_config", str(config_path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


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

    # 3. Slow path: ask _config (no sys.path mutation)
    try:
        cfg_module = _load_config_via_spec()
        if cfg_module is not None:
            cfg = cfg_module.load_config(require_explicit_content_root=False)
            p = cfg.harvest_state_root.parent / ".metrics"
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except OSError:
                return None
    except Exception:
        pass

    # 4. Final fallback; never let instrumentation crash the host tool.
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


def _set_env_session(sid: str) -> None:
    """Best-effort env write; tolerate failure silently (e.g., NUL bytes)."""
    try:
        os.environ["PA_SESSION_ID"] = sid
    except (ValueError, OSError):
        pass


def start_session(*, session_id: str | None = None) -> str:
    """Begin a new session, always fresh. Overwrites PA_SESSION_ID env var.

    Top-level entry points (skill startup, harvest routine kickoff) should
    call this to avoid the env-bleed bug where two top-level invocations
    from the same shell silently share session ids.

    If `session_id` is provided, validates it; falls back to fresh on invalid.
    """
    global _SESSION_ID
    if session_id is not None and _is_valid_session_id(session_id):
        _SESSION_ID = session_id
    else:
        _SESSION_ID = secrets.token_hex(4)
    _set_env_session(_SESSION_ID)
    return _SESSION_ID


def inherit_or_start() -> str:
    """For child processes: inherit PA_SESSION_ID if valid, else start fresh.

    This is the right call for tools spawned as subprocesses of the skill
    or harvest routine. It honors the parent's session id when set.
    """
    global _SESSION_ID
    env_sid = os.environ.get("PA_SESSION_ID", "")
    if _is_valid_session_id(env_sid):
        _SESSION_ID = env_sid
        return _SESSION_ID
    return start_session()


def get_session_id() -> str:
    """Return current session id; lazily starts a fresh one if unset.

    Does NOT inherit from env in the lazy path — that's the bleed source.
    Use `inherit_or_start()` explicitly for env inheritance.
    """
    global _SESSION_ID
    if _SESSION_ID is None:
        return start_session()
    return _SESSION_ID


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


def _sanitize_data(data: dict) -> dict:
    """Drop PII-denylist keys; sanitize topic_keywords."""
    out: dict = {}
    for k, v in data.items():
        if not isinstance(k, str) or k.lower() in PII_DENYLIST:
            continue
        if k == "topic_keywords":
            out[k] = _sanitize_keywords(v)
        else:
            out[k] = v
    return out


def emit(event: str, *, duration_ms: int | None = None, **data: Any) -> bool:
    """Append one event to today's events file. Returns True on success.

    Instrumentation must never crash the host tool — any error is swallowed
    and the function returns False. Use the return value only for testing.

    Bounds enforced (silently):
    - Sanitized `topic_keywords` (count + length + lowercase)
    - PII denylist on `**data` keys
    - Hard line cap (MAX_LINE_BYTES); over-budget events are dropped
    - Crash-safety: any exception in serialization or write returns False
    """
    try:
        path = _today_path()
        if path is None:
            return False

        sanitized = _sanitize_data(data)

        payload: dict[str, Any] = {
            "ts": _utcnow_iso(),
            "session_id": get_session_id(),
            "event": str(event)[:64],  # bound event name length too
        }
        if duration_ms is not None:
            try:
                payload["duration_ms"] = int(duration_ms)
            except (TypeError, ValueError):
                pass  # silently drop bad duration
        if sanitized:
            payload["data"] = sanitized

        # Serialize. default=_safe_default converts non-JSON-native types
        # (Path, datetime, set) to bounded strings rather than crashing.
        # Exceptions surface as "<TypeName>" only — never their .args/.__str__
        # which can leak PII via tracebacks. Bounded to MAX_LINE_BYTES.
        try:
            line = json.dumps(payload, ensure_ascii=False, default=_safe_default)
        except (TypeError, ValueError):
            return False  # truly unserializable

        line_bytes = len(line.encode("utf-8"))
        if line_bytes > MAX_LINE_BYTES:
            # Too big — likely concurrency-corrupting. Drop.
            return False

        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True
        except OSError:
            return False

    except Exception:
        # Catch-all: instrumentation must never crash the host tool.
        return False


@contextmanager
def time_event(event: str, **data: Any):
    """Context manager that emits two events: `<event>_start` and `<event>_end`,
    with `duration_ms` on the end event.

    Inside the context, the caller can set/extend keys on the yielded dict
    (`tracker`); those keys land on the `_end` event's data. The `_start`
    event is emitted with whatever data was passed in (excluding tracker
    mutations).

    Crash-safety: if the body raises, the `_end` event is still emitted
    (with `error_type` capturing the exception class name) before the
    exception propagates. The library never swallows the user's exception.
    """
    start = time.monotonic()
    emit(f"{event}_start", **data)
    tracker: dict[str, Any] = dict(data)
    try:
        yield tracker
    except Exception as exc:
        # Capture failure metadata onto the _end event without leaking exception details.
        tracker["error_type"] = type(exc).__name__
        raise
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        emit(f"{event}_end", duration_ms=duration_ms, **tracker)
