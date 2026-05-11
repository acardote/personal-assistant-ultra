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

- `start_session()` always mints a fresh 8-hex-char id, overwrites
  `PA_SESSION_ID` env var, AND persists the id to `<metrics_dir>/.current_session.json`.
  Top-level entry points (skill startup, harvest routine kickoff) should
  call this.
- `inherit_or_start()` honors an inherited `PA_SESSION_ID` if set; otherwise
  falls back to the on-disk session-state file (if fresh, per
  `SESSION_STATE_TTL_SECONDS`); otherwise starts fresh. Child tool
  subprocesses use this to participate in the parent's session.
- `get_session_id()` is lazy: if no session has been started, it calls
  `start_session()` (i.e., fresh, NOT env-inherited). This avoids the
  env-bleed bug where two top-level invocations from the same shell
  silently share session ids.

### Cross-Bash-tool-call inheritance (#155)

Claude Code spawns each tool invocation in its own `bash -c` child of the
harness, so `export PA_SESSION_ID=...` in tool call N does NOT carry to
tool call N+1 within the same user turn. To aggregate `query_start →
kb_load_end → memory_retrieve_end → query_end` under one `session_id`,
`start_session()` and `inherit_or_start()` persist the id to an on-disk
state file alongside the env var. Subsequent tools in the same turn read
the file as a fallback when env is empty. A TTL keeps two distinct user
turns from sharing a session when no explicit bootstrap re-mints.

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

# Cross-Bash-tool-call session inheritance (#155). Claude Code spawns each
# tool in its own `bash -c`, so the env-var export from the skill's bootstrap
# doesn't reach subsequent tool calls. We persist the session id to a state
# file in the metrics dir; subsequent tools fall back to it when env is empty.
# The TTL bounds session bleed across distinct user turns when no explicit
# bootstrap re-mints — 30 min comfortably covers a single user turn while
# still separating most cross-turn activity.
SESSION_STATE_FILENAME = ".current_session.json"
SESSION_STATE_TTL_SECONDS = 30 * 60

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
    """Import _config without sys.path mutation (per PR-A challenger-finding #5).

    Note: Python 3.14's dataclass implementation reads cls.__module__ from
    sys.modules during decoration, so the module MUST be registered before
    exec_module, or the frozen-dataclass at _config.py:41 crashes with
    AttributeError: 'NoneType' object has no attribute '__dict__'.
    """
    import sys as _sys
    config_path = Path(__file__).resolve().parent / "_config.py"
    if not config_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_pa_metrics_config", str(config_path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        _sys.modules["_pa_metrics_config"] = module
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


def _session_state_path() -> Path | None:
    """Locate the on-disk session-state file. None if metrics dir is unresolvable."""
    md = _get_metrics_dir()
    if md is None:
        return None
    return md / SESSION_STATE_FILENAME


def _read_session_state() -> dict | None:
    """Load the session-state JSON. Returns None on any read/parse error."""
    p = _session_state_path()
    if p is None or not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_session_state(sid: str) -> None:
    """Persist the current session_id + last-access timestamp. Atomic via rename;
    silent on filesystem failure (instrumentation MUST NEVER crash the host)."""
    p = _session_state_path()
    if p is None:
        return
    payload = {"session_id": sid, "last_access": _utcnow_iso()}
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _session_state_is_fresh(state: dict) -> bool:
    """True if state's last_access is within SESSION_STATE_TTL_SECONDS."""
    last_access = state.get("last_access")
    if not isinstance(last_access, str):
        return False
    try:
        last_dt = _dt.datetime.fromisoformat(last_access.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_seconds = (_dt.datetime.now(_dt.timezone.utc) - last_dt).total_seconds()
    return 0 <= age_seconds <= SESSION_STATE_TTL_SECONDS


def start_session(*, session_id: str | None = None) -> str:
    """Begin a new session, always fresh. Overwrites PA_SESSION_ID env var
    AND the on-disk session-state file.

    Top-level entry points (skill startup, harvest routine kickoff) should
    call this to avoid the env-bleed bug where two top-level invocations
    from the same shell silently share session ids. The on-disk write makes
    the fresh session id available to subsequent Bash tool invocations
    within the same user turn (#155 closer).

    If `session_id` is provided, validates it; falls back to fresh on invalid.
    """
    global _SESSION_ID
    if session_id is not None and _is_valid_session_id(session_id):
        _SESSION_ID = session_id
    else:
        _SESSION_ID = secrets.token_hex(4)
    _set_env_session(_SESSION_ID)
    _write_session_state(_SESSION_ID)
    return _SESSION_ID


def inherit_or_start() -> str:
    """For child processes: inherit PA_SESSION_ID if valid, else fall back to
    the on-disk session-state file (if fresh per SESSION_STATE_TTL_SECONDS),
    else start fresh.

    The on-disk fallback closes the cross-Bash-tool-call inheritance gap
    (#155): Claude Code spawns each tool in its own `bash -c`, so the
    env-var export from the skill's bootstrap doesn't reach subsequent tool
    calls. The state file persists across calls; the TTL keeps two distinct
    turns from sharing a session when no explicit bootstrap re-mints.

    Every successful inheritance touches the state file's `last_access`
    timestamp so a long-running but active session doesn't expire mid-turn.
    """
    global _SESSION_ID
    # 1. Env inheritance — fast path, preserves existing semantics.
    env_sid = os.environ.get("PA_SESSION_ID", "")
    if _is_valid_session_id(env_sid):
        _SESSION_ID = env_sid
        _write_session_state(_SESSION_ID)  # keep file in sync + extend TTL
        return _SESSION_ID
    # 2. File inheritance — closes the Bash-tool-call gap.
    state = _read_session_state()
    if state is not None and _session_state_is_fresh(state):
        file_sid = state.get("session_id", "")
        if _is_valid_session_id(file_sid):
            _SESSION_ID = file_sid
            _set_env_session(_SESSION_ID)
            _write_session_state(_SESSION_ID)  # extend TTL
            return _SESSION_ID
    # 3. Mint fresh.
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
