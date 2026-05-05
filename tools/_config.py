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


def load_config(*, require_explicit_content_root: bool = False) -> Config:
    """Load the per-checkout config. Returns a Config with both roots resolved.

    Behaviour matrix:
      - .assistant.local.json missing       -> fallback (warn) OR raise if `require_explicit_content_root`
      - .assistant.local.json malformed     -> fallback (warn) OR raise if `require_explicit_content_root`
      - paths.content_root missing/empty    -> fallback (warn) OR raise if `require_explicit_content_root`
      - paths.content_root not a directory  -> fallback (warn) OR raise if `require_explicit_content_root`
      - all good                            -> return Config(content_root=resolved path)
    """
    config_path = METHOD_ROOT / CONFIG_FILENAME
    config_existed_at_start = config_path.exists()

    def _fallback(reason: str) -> Config:
        if require_explicit_content_root:
            raise RuntimeError(
                f"personal-assistant config required but {reason} (expected {config_path}). "
                f"Create .assistant.local.json (see .assistant.local.json.example) and set "
                f"paths.content_root."
            )
        _emit_fallback_warning(reason, config_path, config_existed=config_existed_at_start)
        return Config(
            method_root=METHOD_ROOT,
            content_root=METHOD_ROOT,
            config_source="fallback",
            config_path=config_path,
        )

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

    # Reject relative paths early — almost always a misconfiguration (per challenger
    # suggestion S1 on PR #16): relative paths resolve against process cwd, which is
    # the F3 portability hazard the per-checkout config is meant to avoid.
    expanded = os.path.expanduser(raw_content_root)
    if not (os.path.isabs(expanded) or expanded.startswith("~")):
        return _fallback(
            f"paths.content_root must be absolute or ~-prefixed; got '{raw_content_root}'"
        )
    content_root = Path(expanded).resolve()

    if not content_root.is_dir():
        return _fallback(
            f"paths.content_root resolves to {content_root} which is not an existing directory"
        )

    # F1 / C2 (challenger): if content_root === method_root, the user has either
    # mis-pointed the config at the method checkout or hasn't set up a vault yet.
    # Either way, refusing here is the right move — letting writers proceed under
    # an explicit config that points at the method repo is the exact pollution path
    # F1 was scoped to prevent.
    if content_root == METHOD_ROOT:
        return _fallback(
            f"paths.content_root resolves to the method root ({METHOD_ROOT}); "
            f"content must live in a separate vault repo, not co-located with method"
        )

    return Config(
        method_root=METHOD_ROOT,
        content_root=content_root,
        config_source="file",
        config_path=config_path,
    )
