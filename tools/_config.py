"""Shared configuration loader for the personal-assistant tools.

Reads `<method-root>/.assistant.local.json` (gitignored, per-checkout) and exposes:
  - `method_root`: this repo's checkout (where tools live)
  - `content_root`: where vault content (memory/, kb/, .harvest/, raw/) lives

Per #12's load-bearing falsifier (F1 from challenger): when `.assistant.local.json`
is missing or malformed, the loader emits a LOUD warning to stderr AND falls back
to `method_root` as `content_root`. This keeps fixtures + test runs working without
a vault, while making "I forgot to set this up" impossible to miss in real use.

If a tool needs to be strict (refuse to run without an explicit config), pass
`require_explicit_content_root=True` to `load_config()`.

Schema for `.assistant.local.json` (current shape — additive evolution only):

    {
      "paths": {
        "content_root": "<absolute or ~-prefixed path>"
      }
    }

`content_root` is `~`-expanded and resolved to an absolute path. If the resolved
path does not exist, the loader treats it as a malformed config and follows the
fallback-with-warning rule above.

Per #214's A+D strategy: when the `PA_CONTENT_ROOT` env var is set,
`load_config()` honors it FIRST and `.assistant.local.json` is ignored. The env
path reuses every validation the file path does (absoluteness, exists, not at-
or-inside method root). On successful env routing, a single stderr breadcrumb
of the form `[pa] content_root via PA_CONTENT_ROOT = <path>` is emitted
(suppress via `PA_QUIET=1`) so the user sees which content_root the session
is using. Callers that need the canonical vault root REGARDLESS of env state
(e.g. the `pa-session` launch helper) should pass `use_env=False`.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# A method-root relative to this file. tools/_config.py is at <method_root>/tools/_config.py.
METHOD_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILENAME = ".assistant.local.json"


@dataclass(frozen=True)
class Config:
    method_root: Path
    content_root: Path
    config_source: str  # "file" if loaded from .assistant.local.json, "fallback" if defaulted
    config_path: Path

    @property
    def memory_root(self) -> Path:
        return self.content_root / "memory"

    @property
    def raw_root(self) -> Path:
        return self.content_root / "raw"

    @property
    def harvest_state_root(self) -> Path:
        return self.content_root / ".harvest"

    @property
    def kb_content_root(self) -> Path:
        """Layer-3 KB entries that are *user content* (people, org, decisions).
        See `kb_method_glossary` for the method-canonical glossary."""
        return self.content_root / "kb"

    @property
    def kb_method_glossary(self) -> Path:
        """The canonical project glossary (raw archive, memory object, etc.).
        Stays in method repo so all checkouts share one definition of project terms."""
        return self.method_root / "kb" / "glossary.md"


def _emit_fallback_warning(reason: str, expected_path: Path, *, config_existed: bool) -> None:
    """Loud, hard-to-miss stderr warning. Per F1: silent fallback is the failure mode."""
    bar = "=" * 78
    print(f"\n{bar}", file=sys.stderr)
    print("WARNING: personal-assistant config fallback in effect", file=sys.stderr)
    print(f"  reason: {reason}", file=sys.stderr)
    print(f"  expected: {expected_path}", file=sys.stderr)
    print(f"  falling back to: content_root = method_root = {METHOD_ROOT}", file=sys.stderr)
    print("  This is OK for fixtures / tests; NOT OK for real harvest.", file=sys.stderr)
    if config_existed:
        print("  Your .assistant.local.json was found but is invalid — fix it (do not", file=sys.stderr)
        print("  re-copy from the example, that would clobber any other settings).", file=sys.stderr)
    else:
        print("  To set up: copy .assistant.local.json.example to .assistant.local.json", file=sys.stderr)
        print("  and edit `paths.content_root` to point at your vault checkout.", file=sys.stderr)
    print(f"{bar}\n", file=sys.stderr)


def load_config(
    *,
    require_explicit_content_root: bool = False,
    use_env: bool = True,
) -> Config:
    """Load the per-checkout config. Returns a Config with both roots resolved.

    Behaviour matrix:
      - PA_CONTENT_ROOT set + valid (use_env=True)  -> return Config(config_source="env")
      - PA_CONTENT_ROOT set + invalid (use_env=True) -> fallback (warn) OR raise if strict
      - PA_CONTENT_ROOT unset, or use_env=False     -> fall through to file path below
      - .assistant.local.json missing               -> fallback (warn) OR raise if strict
      - .assistant.local.json malformed             -> fallback (warn) OR raise if strict
      - paths.content_root missing/empty            -> fallback (warn) OR raise if strict
      - paths.content_root not a directory          -> fallback (warn) OR raise if strict
      - all good                                    -> return Config(config_source="file")
    """
    config_path = METHOD_ROOT / CONFIG_FILENAME
    config_existed_at_start = config_path.exists()

    def _fallback(reason: str) -> Config:
        if require_explicit_content_root:
            raise RuntimeError(
                f"personal-assistant config required but {reason} (expected "
                f"{config_path} or PA_CONTENT_ROOT env). Create "
                f".assistant.local.json (see .assistant.local.json.example) and "
                f"set paths.content_root, or set PA_CONTENT_ROOT to a valid vault path."
            )
        _emit_fallback_warning(reason, config_path, config_existed=config_existed_at_start)
        return Config(
            method_root=METHOD_ROOT,
            content_root=METHOD_ROOT,
            config_source="fallback",
            config_path=config_path,
        )

    def _validate(raw: str, source_label: str) -> tuple[Path | None, str]:
        """Validate a raw content_root string. Shared by env and file paths.

        Returns (path, "") on success, (None, reason) on failure.
        """
        # Reject relative paths early — same F3 portability hazard the per-checkout
        # config is meant to avoid; per challenger suggestion S1 on PR #16.
        expanded = os.path.expanduser(raw)
        if not Path(expanded).is_absolute():
            return None, f"{source_label} must be absolute or ~-prefixed; got '{raw}'"
        path = Path(expanded).resolve()
        if not path.is_dir():
            return None, (
                f"{source_label} resolves to {path} which is not an existing directory"
            )
        # F1 / C2 (challenger): if content_root === method_root OR content_root is
        # anywhere inside method_root, the F1 pollution path is open — the user
        # mis-pointed the config at the method checkout or co-located content
        # inside it. Either way, refuse on the foundation.
        method_resolved = METHOD_ROOT.resolve()
        if path == method_resolved or path.is_relative_to(method_resolved):
            return None, (
                f"{source_label} resolves to {path}, which is at or inside the "
                f"method root ({method_resolved}); content must live in a separate vault repo"
            )
        return path, ""

    # Env-var routing path (A from #214). Takes precedence over file when use_env=True
    # and PA_CONTENT_ROOT is set+non-empty. Honors the same validation as the file path.
    if use_env:
        env_raw = os.environ.get("PA_CONTENT_ROOT")
        if env_raw is not None and env_raw != "":
            env_path, err = _validate(env_raw, "PA_CONTENT_ROOT")
            if env_path is not None:
                if os.environ.get("PA_QUIET") != "1":
                    print(
                        f"[pa] content_root via PA_CONTENT_ROOT = {env_path}",
                        file=sys.stderr,
                    )
                return Config(
                    method_root=METHOD_ROOT,
                    content_root=env_path,
                    config_source="env",
                    config_path=config_path,
                )
            # Env was set but invalid — fail loudly via existing _fallback path so
            # the misconfiguration is visible (the failure mode #12's F1 was designed against).
            return _fallback(err)

    # File-based routing (today's behaviour, unchanged from before #214).
    if not config_existed_at_start:
        return _fallback(f"{CONFIG_FILENAME} not found at method root")

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _fallback(f"{CONFIG_FILENAME} is not valid JSON ({exc})")

    if not isinstance(data, dict):
        return _fallback(f"{CONFIG_FILENAME} root is not a JSON object")

    paths = data.get("paths") or {}
    raw_content_root = paths.get("content_root")
    if not raw_content_root or not isinstance(raw_content_root, str):
        return _fallback(f"{CONFIG_FILENAME} is missing paths.content_root")

    file_path_resolved, err = _validate(raw_content_root, "paths.content_root")
    if file_path_resolved is None:
        return _fallback(err)

    return Config(
        method_root=METHOD_ROOT,
        content_root=file_path_resolved,
        config_source="file",
        config_path=config_path,
    )
