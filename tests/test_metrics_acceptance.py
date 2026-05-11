#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/_metrics.py — event emission library (#41 PR-A).

Tests:
  T1 — emit() writes a JSON line to today's events file.
  T2 — emit() returns False on unwritable metrics dir, doesn't raise.
  T3 — start_session() returns a stable id; subsequent emits include it.
  T4 — inherit_or_start() honors PA_SESSION_ID env var.
  T5 — topic_keywords sanitization (bounded count + length, lowercased).
  T6 — time_event context manager emits _start and _end with duration.
  T7 — time_event tracker mutation lands on _end event.
  T8 — emission overhead is <50ms per event (privacy contract).
  T9 — env-var resolution priority (PA_METRICS_DIR > PA_CONTENT_ROOT).
  T10 — non-string topic_keywords are dropped (not crashed on).
  T11 — _today_path uses UTC date (not local).
  T12 — log-event.py CLI emits an event end-to-end (string-typed --data).
  T13 — emit() handles non-serializable **data without crashing (round-1 fix).
  T14 — concurrent emit from two processes produces valid jsonl (round-1).
  T15 — invalid session id (NUL byte / whitespace) doesn't crash env-write.
  T16 — PII denylist drops sensitive field names from **data.
  T17 — line size cap drops oversized events (>4KB).
  T18 — start_session() always mints fresh, even with PA_SESSION_ID set.
  T19 — time_event still emits _end when body raises.
  T20 — log-event.py --json-data parses typed values; --data keeps strings.
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fresh_metrics_module(tmpdir: Path):
    """Reset module-level state with PA_METRICS_DIR pointed at tmpdir."""
    os.environ["PA_METRICS_DIR"] = str(tmpdir)
    os.environ.pop("PA_SESSION_ID", None)
    os.environ.pop("PA_CONTENT_ROOT", None)
    sys.modules.pop("metrics_test", None)
    return load_module("metrics_test", PROJ / "tools" / "_metrics.py")


def test_emit_writes_jsonl():
    """T1: emit() writes a JSON line."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        ok = m.emit("test_event", foo="bar", count=3)
        assert ok is True
        files = list(Path(td).glob("events-*.jsonl"))
        assert len(files) == 1
        parsed = json.loads(files[0].read_text().splitlines()[0])
        assert parsed["event"] == "test_event"
        assert parsed["data"]["foo"] == "bar"
        assert parsed["data"]["count"] == 3
        assert "ts" in parsed and "session_id" in parsed
    print("  T1 PASS — emit() writes well-formed JSON line.")


def test_emit_failure_returns_false():
    """T2: emit() returns False (deterministically) on unwritable metrics dir."""
    with tempfile.TemporaryDirectory() as td:
        unwritable = Path(td) / "noperm"
        unwritable.mkdir()
        unwritable.chmod(0o400)
        try:
            os.environ["PA_METRICS_DIR"] = str(unwritable / "subdir")
            os.environ.pop("PA_SESSION_ID", None)
            os.environ.pop("PA_CONTENT_ROOT", None)
            sys.modules.pop("metrics_test", None)
            m = load_module("metrics_test", PROJ / "tools" / "_metrics.py")
            ok = m.emit("test_event")
            assert ok is False, f"unwritable parent should yield False; got {ok}"
        finally:
            unwritable.chmod(0o700)
    print("  T2 PASS — emit() deterministically returns False on unwritable dir.")


def test_start_session_stable():
    """T3: start_session() returns same id; subsequent emits include it."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        sid = m.start_session()
        assert isinstance(sid, str) and len(sid) == 8
        m.emit("a")
        m.emit("b")
        events = [json.loads(line) for line in next(Path(td).glob("events-*.jsonl")).read_text().splitlines()]
        assert all(e["session_id"] == sid for e in events)
    print("  T3 PASS — session_id stable across emits.")


def test_inherit_or_start_honors_env():
    """T4: inherit_or_start() honors PA_SESSION_ID."""
    with tempfile.TemporaryDirectory() as td:
        os.environ["PA_METRICS_DIR"] = str(td)
        os.environ["PA_SESSION_ID"] = "abc12345"
        sys.modules.pop("metrics_test", None)
        m = load_module("metrics_test", PROJ / "tools" / "_metrics.py")
        sid = m.inherit_or_start()
        assert sid == "abc12345", f"expected env session id, got {sid}"
        m.emit("ping")
        events = [json.loads(line) for line in next(Path(td).glob("events-*.jsonl")).read_text().splitlines()]
        assert events[0]["session_id"] == "abc12345"
    print("  T4 PASS — inherit_or_start() honors PA_SESSION_ID.")


def test_topic_keywords_sanitized():
    """T5: topic_keywords are bounded + lowercased."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        long_kw = "a" * 100
        m.emit("test", topic_keywords=["BADAS", "Atlas", long_kw, "x", "Y", "Z", "W", "EXTRA"])
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        kws = e["data"]["topic_keywords"]
        assert len(kws) <= 5
        assert all(k == k.lower() for k in kws)
        truncated = next((k for k in kws if k.startswith("a")), None)
        assert truncated is not None and len(truncated) <= 32
    print("  T5 PASS — topic_keywords bounded + lowercased.")


def test_time_event_emits_pair():
    """T6: time_event emits _start and _end."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        with m.time_event("retrieval", topic_keywords=["badas"]) as t:
            time.sleep(0.05)
            t["memory_hits"] = 7
        events = [json.loads(line) for line in next(Path(td).glob("events-*.jsonl")).read_text().splitlines()]
        assert len(events) == 2
        assert events[0]["event"] == "retrieval_start"
        assert events[1]["event"] == "retrieval_end"
        assert events[1]["duration_ms"] >= 50
        assert events[1]["data"]["memory_hits"] == 7
    print("  T6 PASS — time_event emits _start + _end with duration.")


def test_time_event_tracker_carries_to_end():
    """T7: tracker mutations propagate to end event but NOT start event."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        with m.time_event("op") as t:
            t["foo"] = "bar"
        events = [json.loads(line) for line in next(Path(td).glob("events-*.jsonl")).read_text().splitlines()]
        start_data = events[0].get("data", {})
        end_data = events[1].get("data", {})
        assert "foo" not in start_data
        assert end_data.get("foo") == "bar"
    print("  T7 PASS — tracker mutations land on _end, not _start.")


def test_emit_under_50ms():
    """T8: single emit takes <50ms per event."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        m.emit("warmup")
        start = time.monotonic()
        for _ in range(100):
            m.emit("perf_test", foo="bar")
        per_event_ms = ((time.monotonic() - start) / 100) * 1000
        assert per_event_ms < 50, f"emit overhead too high: {per_event_ms:.2f}ms"
    print(f"  T8 PASS — emit overhead {per_event_ms:.2f}ms per event (<50ms).")


def test_env_resolution_priority():
    """T9: PA_METRICS_DIR takes priority over PA_CONTENT_ROOT."""
    with tempfile.TemporaryDirectory() as td:
        explicit = Path(td) / "explicit"
        content_root = Path(td) / "content"
        os.environ["PA_METRICS_DIR"] = str(explicit)
        os.environ["PA_CONTENT_ROOT"] = str(content_root)
        os.environ.pop("PA_SESSION_ID", None)
        sys.modules.pop("metrics_test", None)
        m = load_module("metrics_test", PROJ / "tools" / "_metrics.py")
        m.emit("ping")
        explicit_files = list(explicit.glob("events-*.jsonl")) if explicit.exists() else []
        content_files = list((content_root / ".metrics").glob("events-*.jsonl")) if (content_root / ".metrics").exists() else []
        assert len(explicit_files) == 1
        assert len(content_files) == 0
    print("  T9 PASS — PA_METRICS_DIR > PA_CONTENT_ROOT priority.")


def test_non_string_keywords_dropped():
    """T10: non-string keywords filtered, not crashed on."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        m.emit("test", topic_keywords=["valid", 42, None, {"nested": "obj"}, "also_valid"])
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        kws = e["data"]["topic_keywords"]
        assert "valid" in kws and "also_valid" in kws
        assert all(isinstance(k, str) for k in kws)
    print("  T10 PASS — non-string keywords filtered cleanly.")


def test_today_path_uses_utc():
    """T11: today's filename uses UTC date."""
    import datetime as _dt
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        m.emit("ping")
        files = list(Path(td).glob("events-*.jsonl"))
        expected = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        assert any(expected in f.name for f in files)
    print("  T11 PASS — today's events file uses UTC date.")


def test_log_event_cli():
    """T12: tools/log-event.py emits an event end-to-end (default string-typed)."""
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "PA_METRICS_DIR": str(td)}
        env.pop("PA_SESSION_ID", None)
        result = subprocess.run(
            [str(PROJ / "tools" / "log-event.py"), "harvest_start",
             "--data", "scheduler=routine",
             "--data", "topic_keywords=acko,pico,badas"],
            env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, f"log-event.py failed: stderr={result.stderr}"
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        assert e["event"] == "harvest_start"
        assert e["data"]["scheduler"] == "routine"  # string, not int
        assert e["data"]["topic_keywords"] == ["acko", "pico", "badas"]
    print("  T12 PASS — log-event.py CLI emits event with string-typed values.")


def test_non_serializable_data():
    """T13: emit() handles non-serializable values via default=str (round-1 fix)."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        # Path, datetime, set, custom object — all non-JSON-native
        import datetime as _dt
        ok = m.emit("test",
                    a_path=Path("/tmp/foo"),
                    a_dt=_dt.datetime.now(),
                    a_set={"x", "y"},
                    a_obj=object())
        assert ok is True, "emit should succeed (with default=str fallback)"
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        # Values were string-coerced
        assert isinstance(e["data"]["a_path"], str)
        assert isinstance(e["data"]["a_dt"], str)
    print("  T13 PASS — non-serializable data coerced via default=str (no crash).")


def _emit_n(args):
    """Worker for T14 multi-process emit test."""
    metrics_dir, n, label = args
    os.environ["PA_METRICS_DIR"] = str(metrics_dir)
    os.environ["PA_SESSION_ID"] = label
    sys.modules.pop("metrics_test", None)
    spec = importlib.util.spec_from_file_location(
        "metrics_test", str(PROJ / "tools" / "_metrics.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for i in range(n):
        m.emit("concurrent", worker=label, idx=i)


def test_concurrent_emit():
    """T14: multi-process concurrent emit produces valid jsonl, no corruption."""
    with tempfile.TemporaryDirectory() as td:
        n_per_worker = 50
        workers = [(Path(td), n_per_worker, label) for label in ("aabbccdd", "11223344")]
        with multiprocessing.Pool(2) as pool:
            pool.map(_emit_n, workers)
        files = list(Path(td).glob("events-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().splitlines()
        # Could be ≤ 100 (some events may have been dropped if oversized, but
        # these are tiny). Each line must parse as valid JSON.
        assert len(lines) == 2 * n_per_worker, (
            f"expected {2 * n_per_worker} lines, got {len(lines)} (concurrency corruption?)"
        )
        for line in lines:
            json.loads(line)  # raises if any line is corrupt
    print(f"  T14 PASS — {2 * n_per_worker} concurrent emits produced valid jsonl.")


def test_invalid_session_id_safe():
    """T15: invalid session ids (NUL, newlines) don't crash env-write."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        # Try to set an invalid session id — should fall back to fresh
        sid = m.start_session(session_id="bad\x00id")
        assert "\x00" not in sid, "should not accept NUL bytes"
        # And a freshly generated id should now be in use
        assert len(sid) == 8 and sid.isalnum()
    print("  T15 PASS — invalid session ids (NUL bytes) handled safely.")


def test_pii_denylist():
    """T16: PII denylist field names are dropped from **data."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        m.emit("kb_load",
               raw_query="What's my SSN?",  # denylisted
               api_key="sk-abc123",          # denylisted
               EMAIL="user@example.com",      # denylisted (case-insensitive)
               kb_chars=12345)                # allowed
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        data = e["data"]
        assert "raw_query" not in data
        assert "api_key" not in data
        assert "EMAIL" not in data and "email" not in data
        assert data["kb_chars"] == 12345
    print("  T16 PASS — PII denylist drops sensitive field names.")


def test_oversized_event_dropped():
    """T17: events exceeding MAX_LINE_BYTES are dropped to avoid concurrency corruption."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        huge = "x" * 5000  # > MAX_LINE_BYTES
        ok = m.emit("test", payload=huge)
        assert ok is False, "oversized event should be dropped"
        files = list(Path(td).glob("events-*.jsonl"))
        # File may not exist or may be empty — neither is a regression
        if files:
            assert files[0].read_text().strip() == ""
    print("  T17 PASS — oversized events dropped to protect line atomicity.")


def test_start_session_always_fresh():
    """T18: start_session() mints fresh even when PA_SESSION_ID is set (no env bleed)."""
    with tempfile.TemporaryDirectory() as td:
        os.environ["PA_METRICS_DIR"] = str(td)
        os.environ["PA_SESSION_ID"] = "stale123"
        sys.modules.pop("metrics_test", None)
        m = load_module("metrics_test", PROJ / "tools" / "_metrics.py")
        sid = m.start_session()
        assert sid != "stale123", "start_session should NOT reuse stale env"
        assert os.environ["PA_SESSION_ID"] == sid, "env should be overwritten with fresh id"
    print("  T18 PASS — start_session mints fresh, no env bleed.")


def test_time_event_end_on_exception():
    """T19: time_event emits _end even when body raises, including error_type."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        try:
            with m.time_event("op") as t:
                t["pre"] = "set"
                raise ValueError("intentional")
        except ValueError:
            pass
        events = [json.loads(line) for line in next(Path(td).glob("events-*.jsonl")).read_text().splitlines()]
        assert len(events) == 2
        assert events[0]["event"] == "op_start"
        assert events[1]["event"] == "op_end"
        assert events[1]["data"]["error_type"] == "ValueError"
        assert events[1]["data"]["pre"] == "set"
    print("  T19 PASS — time_event emits _end with error_type when body raises.")


def test_pii_denylist_extended():
    """T21: extended PII denylist catches phone, ssn, name, username, ip, etc. (round-2)."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        m.emit("kb_load",
               phone="+1-555-1234",      # denylisted
               ssn="123-45-6789",        # denylisted
               name="Jane Doe",          # denylisted
               username="jdoe",          # denylisted
               user_id="u_12345",        # denylisted
               ip_address="1.2.3.4",     # denylisted
               authorization="Bearer x", # denylisted
               kb_chars=12345)            # allowed
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        data = e["data"]
        for forbidden in ("phone", "ssn", "name", "username", "user_id", "ip_address", "authorization"):
            assert forbidden not in data, f"{forbidden} should be denylisted"
        assert data["kb_chars"] == 12345
    print("  T21 PASS — extended PII denylist (phone/ssn/name/username/ip/auth) enforced.")


def test_exception_objects_redacted():
    """T22: Exception objects in **data are surfaced as <TypeName> only — no PII leak from str(exc) (round-2)."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        try:
            raise ValueError("user@example.com had a bad password: hunter2")
        except ValueError as exc:
            m.emit("test", error=exc)
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        err_str = e["data"]["error"]
        # Should be "<ValueError>" not the leaky message.
        assert err_str == "<ValueError>", f"exception leaked PII: {err_str!r}"
        assert "@example.com" not in err_str
        assert "hunter2" not in err_str
    print("  T22 PASS — exception objects in **data are redacted to <TypeName>.")


def test_data_json_data_precedence():
    """T23: --json-data overrides --data when same key is given (round-2)."""
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "PA_METRICS_DIR": str(td)}
        env.pop("PA_SESSION_ID", None)
        result = subprocess.run(
            [str(PROJ / "tools" / "log-event.py"), "test_event",
             "--data", "foo=string_value",
             "--json-data", "foo=42"],  # should win
            env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, f"log-event failed: {result.stderr}"
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        # --json-data parses last → wins
        assert e["data"]["foo"] == 42, f"--json-data should win, got {e['data']['foo']!r}"
    print("  T23 PASS — --json-data wins over --data on same key.")


def test_default_str_bounded():
    """T24: _safe_default bounds string-coerced values to 512 chars (round-2)."""
    with tempfile.TemporaryDirectory() as td:
        m = fresh_metrics_module(Path(td))
        # Use a custom non-JSON-native object whose str() is huge
        class HugeRepr:
            def __str__(self): return "x" * 10000
        m.emit("test", giant=HugeRepr())
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        # Either dropped entirely (too big), or bounded
        if "data" in e and "giant" in e["data"]:
            assert len(e["data"]["giant"]) <= 512, (
                f"_safe_default should bound to 512 chars; got {len(e['data']['giant'])}"
            )
    print("  T24 PASS — _safe_default bounds string-coerced values to 512 chars.")


def test_log_event_json_data_typing():
    """T20: --json-data parses typed values; --data keeps strings."""
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "PA_METRICS_DIR": str(td)}
        env.pop("PA_SESSION_ID", None)
        result = subprocess.run(
            [str(PROJ / "tools" / "log-event.py"), "test_event",
             "--data", "string_field=42",         # stays string
             "--json-data", "int_field=42",       # parses to int
             "--json-data", "bool_field=true",    # parses to bool
             "--json-data", "list_field=[1,2,3]"], # parses to list
            env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, f"log-event failed: {result.stderr}"
        e = json.loads(next(Path(td).glob("events-*.jsonl")).read_text().splitlines()[0])
        d = e["data"]
        assert d["string_field"] == "42"  # string!
        assert d["int_field"] == 42       # int
        assert d["bool_field"] is True    # bool
        assert d["list_field"] == [1, 2, 3]
    print("  T20 PASS — --data keeps strings; --json-data parses typed values.")


def test_inherit_from_state_file_when_env_empty():
    """T25 (#155): inherit_or_start() falls back to the on-disk session-state
    file when PA_SESSION_ID env is unset.

    This is the load-bearing case for cross-Bash-tool-call inheritance: each
    Claude Code Bash invocation is a fresh `bash -c`, so the env-var export
    from skill bootstrap doesn't survive into call N+1. The fix persists the
    session id to `<metrics_dir>/.current_session.json` so subsequent tools
    in the same turn can recover it."""
    import datetime as _dt
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        # Simulate "skill bootstrap" — process A mints + writes state file.
        m1 = fresh_metrics_module(td_p)
        bootstrap_sid = m1.start_session()
        state_file = td_p / m1.SESSION_STATE_FILENAME
        assert state_file.exists(), "start_session must write state file (#155 fix)"
        persisted = json.loads(state_file.read_text())
        assert persisted["session_id"] == bootstrap_sid

        # Simulate "second Bash tool invocation in same turn" — fresh process,
        # no env (Claude Code's bash -c didn't inherit), state file present.
        os.environ.pop("PA_SESSION_ID", None)
        sys.modules.pop("metrics_test", None)
        m2 = load_module("metrics_test", PROJ / "tools" / "_metrics.py")
        recovered = m2.inherit_or_start()
        assert recovered == bootstrap_sid, (
            f"second process should inherit bootstrap session from file, "
            f"got {recovered!r} != {bootstrap_sid!r}"
        )
        # And env should now be set (so any same-process chain continues to work).
        assert os.environ.get("PA_SESSION_ID") == bootstrap_sid
    print("  T25 PASS — inherit_or_start() recovers session from state file when env empty (#155).")


def test_state_file_ttl_expires():
    """T26 (#155): a state file older than SESSION_STATE_TTL_SECONDS is
    treated as stale and inherit_or_start() mints fresh.

    Prevents two distinct user turns (>30 min apart) from silently sharing
    a session when no explicit bootstrap re-mints."""
    import datetime as _dt
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        m = fresh_metrics_module(td_p)
        # Stage a state file with last_access well past TTL.
        stale_sid = "deadbeef"
        stale_ts = (_dt.datetime.now(_dt.timezone.utc)
                    - _dt.timedelta(seconds=m.SESSION_STATE_TTL_SECONDS + 60))
        state_file = td_p / m.SESSION_STATE_FILENAME
        state_file.write_text(json.dumps({
            "session_id": stale_sid,
            "last_access": stale_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }))
        # No env set; inherit_or_start should mint fresh, NOT use stale file.
        os.environ.pop("PA_SESSION_ID", None)
        sys.modules.pop("metrics_test", None)
        m2 = load_module("metrics_test", PROJ / "tools" / "_metrics.py")
        sid = m2.inherit_or_start()
        assert sid != stale_sid, (
            f"stale state file (>TTL) must not be inherited, got {sid!r}"
        )
    print("  T26 PASS — state file older than TTL is ignored, fresh session minted.")


def test_state_file_touched_on_inherit():
    """T27 (#155): every successful inheritance refreshes last_access so a
    long-running but active session doesn't expire mid-turn."""
    import datetime as _dt
    import time as _time
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        m = fresh_metrics_module(td_p)
        original_sid = m.start_session()
        state_file = td_p / m.SESSION_STATE_FILENAME
        original_ts = json.loads(state_file.read_text())["last_access"]
        # Sleep just enough that the second-resolution timestamp can advance.
        _time.sleep(1.1)
        # Second invocation — fresh module, empty env, fresh file → inherit.
        os.environ.pop("PA_SESSION_ID", None)
        sys.modules.pop("metrics_test", None)
        m2 = load_module("metrics_test", PROJ / "tools" / "_metrics.py")
        recovered = m2.inherit_or_start()
        assert recovered == original_sid
        new_ts = json.loads(state_file.read_text())["last_access"]
        assert new_ts > original_ts, (
            f"inherit_or_start should refresh last_access; "
            f"old={original_ts}, new={new_ts}"
        )
    print("  T27 PASS — inherit_or_start refreshes last_access (no premature expiry).")


def test_env_takes_priority_over_state_file():
    """T28 (#155): env-set session_id wins over a different file-stored id.

    Preserves the existing inherit_or_start() semantic — when the caller
    explicitly set PA_SESSION_ID (e.g., harvest routine within one bash
    session), that's authoritative."""
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        m = fresh_metrics_module(td_p)
        # File says one session.
        m.start_session(session_id="aaaaaaaa")
        # Env says a different one.
        os.environ["PA_SESSION_ID"] = "bbbbbbbb"
        sys.modules.pop("metrics_test", None)
        m2 = load_module("metrics_test", PROJ / "tools" / "_metrics.py")
        sid = m2.inherit_or_start()
        assert sid == "bbbbbbbb", f"env must win over file, got {sid!r}"
        # And file should now be updated to env's value.
        state_file = td_p / m.SESSION_STATE_FILENAME
        assert json.loads(state_file.read_text())["session_id"] == "bbbbbbbb"
    print("  T28 PASS — env-set session_id wins over state file (semantics preserved).")


if __name__ == "__main__":
    print("Running test_metrics_acceptance.py...")
    test_emit_writes_jsonl()
    test_emit_failure_returns_false()
    test_start_session_stable()
    test_inherit_or_start_honors_env()
    test_topic_keywords_sanitized()
    test_time_event_emits_pair()
    test_time_event_tracker_carries_to_end()
    test_emit_under_50ms()
    test_env_resolution_priority()
    test_non_string_keywords_dropped()
    test_today_path_uses_utc()
    test_log_event_cli()
    test_non_serializable_data()
    test_concurrent_emit()
    test_invalid_session_id_safe()
    test_pii_denylist()
    test_oversized_event_dropped()
    test_start_session_always_fresh()
    test_time_event_end_on_exception()
    test_pii_denylist_extended()
    test_exception_objects_redacted()
    test_data_json_data_precedence()
    test_default_str_bounded()
    test_log_event_json_data_typing()
    test_inherit_from_state_file_when_env_empty()
    test_state_file_ttl_expires()
    test_state_file_touched_on_inherit()
    test_env_takes_priority_over_state_file()
    print("All metrics tests passed.")
