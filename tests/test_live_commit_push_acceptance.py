#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/live-commit-push.sh — vault commit+push helper.

Tests focus on #87's refusal-on-mismatch contract:
  T1 — content_root arg matching .assistant.local.json proceeds.
  T2 — content_root arg differing from .assistant.local.json refuses with rc=5.
  T3 — symlinked path that resolves to the configured vault still proceeds
       (realpath comparison is the contract).
  T4 — missing .assistant.local.json: helper proceeds (lint will fall back
       to method-only or warn — that's the existing behavior).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
HELPER = PROJ / "tools" / "live-commit-push.sh"


def make_method_with_config(tmpdir: Path, vault_path: Path | None) -> Path:
    """Build an isolated method-root with tools/ and (optionally) a config
    pointing at vault_path. Returns the method path."""
    method = tmpdir / "method"
    method.mkdir()
    (method / "tools").mkdir()
    # Copy the helper + its dependency
    shutil.copy(PROJ / "tools" / "live-commit-push.sh", method / "tools" / "live-commit-push.sh")
    shutil.copy(PROJ / "tools" / "lint-provenance.py", method / "tools" / "lint-provenance.py")
    shutil.copy(PROJ / "tools" / "_config.py", method / "tools" / "_config.py")
    if vault_path is not None:
        (method / ".assistant.local.json").write_text(json.dumps({
            "$schema_version": 1,
            "paths": {"content_root": str(vault_path.resolve())},
        }), encoding="utf-8")
    return method


def make_vault(tmpdir: Path, name: str) -> Path:
    """Create a fresh git working tree with empty initial commit."""
    vault = tmpdir / name
    vault.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.email", "t@test"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=vault, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=vault, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=vault, check=True)
    return vault


def run_helper(method: Path, content_root: Path, msg: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(method / "tools" / "live-commit-push.sh"), str(content_root), msg],
        capture_output=True, text=True,
    )


def test_matching_content_root_proceeds():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        vault = make_vault(td_path, "vault")
        method = make_method_with_config(td_path, vault)
        r = run_helper(method, vault, "test-noop")
        # Empty vault → "no changes staged" → exit 0. Lint may warn but exit 0.
        assert r.returncode == 0, f"matching path should proceed; got rc={r.returncode}\nstderr:\n{r.stderr}"
    print("  T1 PASS — matching content_root proceeds")


def test_mismatched_content_root_refuses():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        configured_vault = make_vault(td_path, "configured")
        other_vault = make_vault(td_path, "other")
        method = make_method_with_config(td_path, configured_vault)
        # Pass the OTHER vault as the arg. Helper must refuse with rc=5.
        r = run_helper(method, other_vault, "test")
        assert r.returncode == 5, f"mismatched path should rc=5; got {r.returncode}\nstderr:\n{r.stderr}"
        assert "disagrees" in r.stderr.lower() or "configured" in r.stderr.lower()
    print("  T2 PASS — mismatched content_root refuses with rc=5")


def test_symlink_to_configured_vault_proceeds():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        vault = make_vault(td_path, "real-vault")
        # Create a symlink that points at the real vault.
        link = td_path / "link-to-vault"
        link.symlink_to(vault)
        method = make_method_with_config(td_path, vault)
        # Pass the symlink as the arg. Realpath should resolve identically.
        r = run_helper(method, link, "test-noop")
        assert r.returncode == 0, (
            f"symlink resolving to configured vault should proceed; got rc={r.returncode}\n{r.stderr}"
        )
    print("  T3 PASS — symlink resolving to configured vault proceeds")


def test_missing_config_proceeds():
    """No .assistant.local.json → no enforcement. Helper falls through to
    its existing behavior (lint-provenance falls back, helper proceeds)."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        vault = make_vault(td_path, "vault")
        method = make_method_with_config(td_path, None)
        r = run_helper(method, vault, "test-noop")
        # The helper should NOT exit 5 (no config to compare against).
        # It may exit 4 if the lint refuses against fallback content_root,
        # but exit 5 (mismatch) must NOT fire.
        assert r.returncode != 5, (
            f"missing config must not trigger mismatch refusal; got rc={r.returncode}\n{r.stderr}"
        )
    print("  T4 PASS — missing config doesn't trigger mismatch refusal")


def test_quoted_path_does_not_crash():
    """Per pr-challenger #104: paths with single quotes shouldn't crash via
    string interpolation. The env-var transport handles this safely."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # macOS / Linux both allow ' in directory names. Use a folder name
        # that would have crashed the inlined-string version.
        odd = td_path / "it's-a-vault"
        odd.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=odd, check=True)
        subprocess.run(["git", "config", "user.email", "t@test"], cwd=odd, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=odd, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=odd, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=odd, check=True)
        method = make_method_with_config(td_path, odd)
        r = run_helper(method, odd, "test-noop")
        assert r.returncode == 0, (
            f"quoted path should NOT crash; got rc={r.returncode}\nstderr:\n{r.stderr}"
        )
    print("  T5 PASS — quoted path doesn't crash python comparison")


def test_corrupt_config_warns_then_proceeds():
    """Per pr-challenger #104: corrupt .assistant.local.json should warn
    visibly and proceed without enforcement (same shape as missing config)."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        vault = make_vault(td_path, "vault")
        method = make_method_with_config(td_path, vault)
        # Clobber the config with garbage.
        (method / ".assistant.local.json").write_text("{ this is not valid json", encoding="utf-8")
        r = run_helper(method, vault, "test-noop")
        # Must not exit 5 (no scope enforcement when config is unparseable).
        assert r.returncode != 5
        # Must surface the warn — visibility per pr-challenger.
        assert "warn:" in r.stderr or "corrupt" in r.stderr.lower()
    print("  T6 PASS — corrupt config warns + proceeds")


if __name__ == "__main__":
    print("Running test_live_commit_push_acceptance.py...")
    test_matching_content_root_proceeds()
    test_mismatched_content_root_refuses()
    test_symlink_to_configured_vault_proceeds()
    test_missing_config_proceeds()
    test_quoted_path_does_not_crash()
    test_corrupt_config_warns_then_proceeds()
    print("All live-commit-push tests passed.")
