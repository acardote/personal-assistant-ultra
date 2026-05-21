#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for tools/_config.py — PA_CONTENT_ROOT env-var support (C2 of #214).

Tests:
  T1  — env-set + valid path returns Config(config_source="env", content_root=<env>).
  T2  — env-set takes precedence over .assistant.local.json when both are present.
  T3  — env-set + relative path falls back via _fallback() with warning naming PA_CONTENT_ROOT.
  T4  — env-set + nonexistent path falls back via _fallback() with warning naming PA_CONTENT_ROOT.
  T5  — env-set + path inside method root falls back via _fallback() with warning naming PA_CONTENT_ROOT.
  T6  — env-unset preserves today's file-based behaviour exactly.
  T7  — use_env=False ignores PA_CONTENT_ROOT even when set+valid; falls through to file path.
  T8  — require_explicit_content_root=True + env-set-invalid raises with a message naming PA_CONTENT_ROOT.
  T9  — PA_QUIET=1 suppresses the [pa] breadcrumb on successful env routing.
  T10 — successful env routing emits the [pa] breadcrumb to stderr.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def load_config_module(method_root: Path):
    """Load _config.py from a fixture method-root so METHOD_ROOT matches the fixture.

    Must register in sys.modules BEFORE exec_module: the @dataclass decorator
    calls sys.modules.get(cls.__module__) during class creation; if the module
    isn't there yet, dataclasses fails with the cryptic 'NoneType has no __dict__'.
    """
    name = f"_config_{id(method_root)}"
    spec = importlib.util.spec_from_file_location(name, method_root / "tools" / "_config.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_fixture(tmpdir: Path, *, with_file_config: bool = True) -> tuple[Path, Path]:
    """Create method + vault skeletons. Returns (method_root, vault)."""
    method = tmpdir / "method"
    vault = tmpdir / "vault"
    method.mkdir()
    vault.mkdir()
    (method / "tools").mkdir()
    shutil.copy(PROJ / "tools" / "_config.py", method / "tools" / "_config.py")
    if with_file_config:
        (method / ".assistant.local.json").write_text(
            json.dumps({
                "$schema_version": 1,
                "paths": {"content_root": str(vault.resolve())},
            }),
            encoding="utf-8",
        )
    return method, vault


def clear_pa_env() -> dict:
    """Snapshot + clear PA_CONTENT_ROOT / PA_QUIET. Returns the snapshot for restore."""
    snap = {
        "PA_CONTENT_ROOT": os.environ.pop("PA_CONTENT_ROOT", None),
        "PA_QUIET": os.environ.pop("PA_QUIET", None),
    }
    return snap


def restore_pa_env(snap: dict) -> None:
    for k, v in snap.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_env_set_valid_returns_env_source():
    """T1 — env-set + valid path returns Config(config_source='env', content_root=<env>)."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            method, vault = make_fixture(Path(td), with_file_config=False)
            cfg_mod = load_config_module(method)
            other_vault = Path(td) / "other-vault"
            other_vault.mkdir()
            os.environ["PA_CONTENT_ROOT"] = str(other_vault.resolve())
            os.environ["PA_QUIET"] = "1"
            cfg = cfg_mod.load_config()
            assert cfg.config_source == "env", f"got {cfg.config_source!r}"
            assert cfg.content_root == other_vault.resolve(), (
                f"got {cfg.content_root}, want {other_vault.resolve()}"
            )
    finally:
        restore_pa_env(snap)


def test_env_overrides_file_config():
    """T2 — env-set takes precedence over .assistant.local.json when both present."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            # File config points at `vault`; env points at `other-vault`. Env wins.
            method, vault = make_fixture(Path(td), with_file_config=True)
            cfg_mod = load_config_module(method)
            other_vault = Path(td) / "other-vault"
            other_vault.mkdir()
            os.environ["PA_CONTENT_ROOT"] = str(other_vault.resolve())
            os.environ["PA_QUIET"] = "1"
            cfg = cfg_mod.load_config()
            assert cfg.config_source == "env"
            assert cfg.content_root == other_vault.resolve()
            assert cfg.content_root != vault.resolve()
    finally:
        restore_pa_env(snap)


def test_env_set_relative_falls_back():
    """T3 — env-set + relative path falls back with PA_CONTENT_ROOT-named warning."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            method, vault = make_fixture(Path(td), with_file_config=True)
            cfg_mod = load_config_module(method)
            os.environ["PA_CONTENT_ROOT"] = "relative/path"
            os.environ["PA_QUIET"] = "1"
            buf = io.StringIO()
            with redirect_stderr(buf):
                cfg = cfg_mod.load_config()
            assert cfg.config_source == "fallback", f"got {cfg.config_source!r}"
            assert "PA_CONTENT_ROOT" in buf.getvalue(), (
                f"fallback warning didn't name PA_CONTENT_ROOT; got:\n{buf.getvalue()}"
            )
            assert "absolute" in buf.getvalue()
    finally:
        restore_pa_env(snap)


def test_env_set_missing_dir_falls_back():
    """T4 — env-set + nonexistent path falls back with PA_CONTENT_ROOT-named warning."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            method, vault = make_fixture(Path(td), with_file_config=True)
            cfg_mod = load_config_module(method)
            missing = Path(td) / "this-does-not-exist"
            os.environ["PA_CONTENT_ROOT"] = str(missing)
            os.environ["PA_QUIET"] = "1"
            buf = io.StringIO()
            with redirect_stderr(buf):
                cfg = cfg_mod.load_config()
            assert cfg.config_source == "fallback"
            assert "PA_CONTENT_ROOT" in buf.getvalue()
            assert "existing directory" in buf.getvalue()
    finally:
        restore_pa_env(snap)


def test_env_set_inside_method_root_falls_back():
    """T5 — env-set + path inside method root falls back with PA_CONTENT_ROOT-named warning."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            method, _vault = make_fixture(Path(td), with_file_config=True)
            cfg_mod = load_config_module(method)
            inside = method / "vault"
            inside.mkdir()
            os.environ["PA_CONTENT_ROOT"] = str(inside.resolve())
            os.environ["PA_QUIET"] = "1"
            buf = io.StringIO()
            with redirect_stderr(buf):
                cfg = cfg_mod.load_config()
            assert cfg.config_source == "fallback"
            assert "PA_CONTENT_ROOT" in buf.getvalue()
            assert "method root" in buf.getvalue()
    finally:
        restore_pa_env(snap)


def test_env_unset_preserves_file_behaviour():
    """T6 — env-unset: today's file-based behaviour is exactly preserved."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            method, vault = make_fixture(Path(td), with_file_config=True)
            cfg_mod = load_config_module(method)
            # PA_CONTENT_ROOT explicitly unset (clear_pa_env() handled it).
            os.environ["PA_QUIET"] = "1"
            cfg = cfg_mod.load_config()
            assert cfg.config_source == "file"
            assert cfg.content_root == vault.resolve()
    finally:
        restore_pa_env(snap)


def test_use_env_false_ignores_env():
    """T7 — use_env=False ignores PA_CONTENT_ROOT and reads file/fallback."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            method, vault = make_fixture(Path(td), with_file_config=True)
            cfg_mod = load_config_module(method)
            other_vault = Path(td) / "other-vault"
            other_vault.mkdir()
            os.environ["PA_CONTENT_ROOT"] = str(other_vault.resolve())
            os.environ["PA_QUIET"] = "1"
            cfg = cfg_mod.load_config(use_env=False)
            # File-config path was taken even though env was set.
            assert cfg.config_source == "file", f"got {cfg.config_source!r}"
            assert cfg.content_root == vault.resolve()
    finally:
        restore_pa_env(snap)


def test_require_explicit_with_bad_env_raises_naming_env():
    """T8 — require_explicit_content_root=True + bad env raises with PA_CONTENT_ROOT in msg."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            method, _vault = make_fixture(Path(td), with_file_config=False)
            cfg_mod = load_config_module(method)
            os.environ["PA_CONTENT_ROOT"] = "relative/no-good"
            os.environ["PA_QUIET"] = "1"
            try:
                cfg_mod.load_config(require_explicit_content_root=True)
            except RuntimeError as e:
                assert "PA_CONTENT_ROOT" in str(e), f"raised msg lacks PA_CONTENT_ROOT: {e}"
                return
            raise AssertionError("expected RuntimeError but none was raised")
    finally:
        restore_pa_env(snap)


def test_pa_quiet_suppresses_breadcrumb():
    """T9 — PA_QUIET=1 suppresses the [pa] breadcrumb on successful env routing."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            method, _vault = make_fixture(Path(td), with_file_config=False)
            cfg_mod = load_config_module(method)
            other_vault = Path(td) / "other-vault"
            other_vault.mkdir()
            os.environ["PA_CONTENT_ROOT"] = str(other_vault.resolve())
            os.environ["PA_QUIET"] = "1"
            buf = io.StringIO()
            with redirect_stderr(buf):
                cfg = cfg_mod.load_config()
            assert cfg.config_source == "env"
            assert "[pa] content_root via PA_CONTENT_ROOT" not in buf.getvalue(), (
                f"breadcrumb leaked under PA_QUIET=1:\n{buf.getvalue()}"
            )
    finally:
        restore_pa_env(snap)


def test_breadcrumb_emitted_when_not_quiet():
    """T10 — successful env routing emits the [pa] breadcrumb to stderr."""
    snap = clear_pa_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            method, _vault = make_fixture(Path(td), with_file_config=False)
            cfg_mod = load_config_module(method)
            other_vault = Path(td) / "other-vault"
            other_vault.mkdir()
            os.environ["PA_CONTENT_ROOT"] = str(other_vault.resolve())
            # Explicitly NOT setting PA_QUIET.
            buf = io.StringIO()
            with redirect_stderr(buf):
                cfg = cfg_mod.load_config()
            assert cfg.config_source == "env"
            assert "[pa] content_root via PA_CONTENT_ROOT" in buf.getvalue(), (
                f"breadcrumb missing:\n{buf.getvalue()}"
            )
            assert str(other_vault.resolve()) in buf.getvalue()
    finally:
        restore_pa_env(snap)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}", file=sys.stderr)
    if failed:
        print(f"\n{failed}/{len(tests)} failed", file=sys.stderr)
        sys.exit(1)
    print(f"\n{len(tests)} tests ok")
