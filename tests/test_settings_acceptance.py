#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for .claude/settings.json (C2 of #216).

The settings.json is a static config file. Tests validate its STRUCTURE invariants
(JSON parseable, allow-only, no narrowing rules) rather than execute it against
Claude Code — that's C4 (#227)'s empirical-validation surface.

Tests:
  T1 — file exists, is valid JSON, has a top-level `permissions` key.
  T2 — `permissions.allow` exists, is a non-empty list of strings.
  T3 — no `permissions.deny` key (A3 non-regression guard).
  T4 — no `permissions.ask` key (would prompt instead of allow — defeats the purpose).
  T5 — no `defaultMode` key at any nesting (`dontAsk` / `bypassPermissions` etc. would change session semantics in ways the parent's scope explicitly excludes).
  T6 — every entry in `allow` matches one of: `mcp__<server>__*`, `mcp__<server>__<tool>`, `Bash(<pattern>)`, or bare `Write` / `Edit`. No bare MCP tool names (would silently not match — see C1 A2 finding).
  T7 — required MCP server prefixes are present (Slack, Gmail, Granola, GitHub) — covers the four connectors the harvest routine depends on.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
SETTINGS = PROJ / ".claude" / "settings.json"

RULE_MCP_WILDCARD = re.compile(r"^mcp__[A-Za-z0-9_]+__\*$")
RULE_MCP_TOOL = re.compile(r"^mcp__[A-Za-z0-9_]+__[A-Za-z0-9_]+$")
RULE_BASH = re.compile(r"^Bash\(.+\)$")
RULE_BARE = {"Write", "Edit"}


def _load():
    assert SETTINGS.is_file(), f"{SETTINGS} does not exist"
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def test_file_exists_and_parses():
    """T1 — .claude/settings.json exists, parses as JSON, has top-level `permissions`."""
    cfg = _load()
    assert isinstance(cfg, dict), "root must be a JSON object"
    assert "permissions" in cfg, "top-level `permissions` key required"


def test_allow_is_non_empty_list():
    """T2 — `permissions.allow` exists and is a non-empty list of strings."""
    cfg = _load()
    allow = cfg["permissions"].get("allow")
    assert isinstance(allow, list), "permissions.allow must be a list"
    assert allow, "permissions.allow must not be empty"
    for rule in allow:
        assert isinstance(rule, str), f"rule must be string, got {type(rule).__name__}: {rule!r}"


def test_no_deny_key():
    """T3 — no `permissions.deny` (A3 non-regression guard: any deny narrows scheduled-fire effective grants)."""
    cfg = _load()
    assert "deny" not in cfg["permissions"], (
        "permissions.deny must not be present — A3 non-regression guard. "
        "If a deny is genuinely needed, file a follow-up that re-validates A3 first."
    )


def test_no_ask_key():
    """T4 — no `permissions.ask` (ask rules prompt instead of allow; defeats the fix)."""
    cfg = _load()
    assert "ask" not in cfg["permissions"], (
        "permissions.ask must not be present — would prompt for approval, defeating the fix."
    )


def test_no_defaultmode_override():
    """T5 — no `defaultMode` key (changing default mode is out of scope per parent #216)."""
    cfg = _load()
    # Per-doc: defaultMode lives at the top-level of settings.json, not under permissions.
    assert "defaultMode" not in cfg, (
        "top-level `defaultMode` override is out of scope. Allow-only model relies on default mode."
    )
    assert "defaultMode" not in cfg.get("permissions", {}), (
        "permissions.defaultMode override is out of scope."
    )


def test_every_rule_matches_expected_shape():
    """T6 — every rule is `mcp__<server>__*|<tool>`, `Bash(...)`, or bare `Write`/`Edit`."""
    cfg = _load()
    for rule in cfg["permissions"]["allow"]:
        ok = (
            rule in RULE_BARE
            or RULE_MCP_WILDCARD.match(rule)
            or RULE_MCP_TOOL.match(rule)
            or RULE_BASH.match(rule)
        )
        assert ok, (
            f"rule {rule!r} does not match any expected shape "
            "(mcp__<server>__* / mcp__<server>__<tool> / Bash(...) / Write / Edit). "
            "Bare MCP tool names like `slack_search_users` silently never match — see C1 A2 finding on #224."
        )


def test_required_mcp_servers_covered():
    """T7 — Slack / Gmail / Granola / GitHub prefixes are all present in some allow rule.

    Each MCP server name appears in at least one rule, either as a wildcard
    (`mcp__claude_ai_Slack__*`) or as a specific tool (`mcp__claude_ai_Slack__slack_search_users`).
    """
    cfg = _load()
    allow_blob = "\n".join(cfg["permissions"]["allow"])
    for server_name in ("Slack", "Gmail", "Granola", "GitHub"):
        # Server name appears inside a mcp__<...>_<server>_<...> rule.
        # Match anywhere in the rule string with the underscore-delimited boundary.
        assert re.search(rf"mcp__[A-Za-z0-9_]*{server_name}[A-Za-z0-9_]*__", allow_blob), (
            f"no allow rule references MCP server containing {server_name!r}. "
            f"The harvest routine depends on Slack / Gmail / Granola / GitHub MCPs "
            f"per templates/routines/harvest-routine.md:45-53."
        )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}", file=sys.stderr)
    if failed:
        print(f"\n{failed}/{len(tests)} failed", file=sys.stderr)
        sys.exit(1)
    print(f"\n{len(tests)} tests ok")
