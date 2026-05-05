#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Assemble layer-3 knowledge base files into a single rendered prompt slice.

Usage:
    tools/assemble-kb.py            # print rendered KB to stdout, token count to stderr
    tools/assemble-kb.py --check    # verify ≤4K token budget; exit 1 if exceeded
    tools/assemble-kb.py --json     # output JSON with metadata + rendered string

The output is the "always-in-context" content that the assistant should load on every
invocation. It is the materialization of layer 3 in the three-layer memory architecture.

Reads from TWO sources per #12's method/content split:
  - method-repo: `<method_root>/kb/glossary.md` — canonical project terms
  - content-vault: `<content_root>/kb/{people,org,decisions}.md` (+ any other user files)
`<content_root>` is resolved via `.assistant.local.json` (see tools/_config.py); when
absent, both fall back to method root with a loud warning so fixture/test runs still work.

Per #12's challenger F2 ("split-source unreachability must error loudly, not silently
truncate"), the assembler ALSO refuses to produce output if the user's vault is
configured but the kb_content_root or kb_method_glossary is unreachable.

The token budget (4K, configurable via --budget) is intended to actively force signal/
noise tradeoffs in KB curation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Local import; sibling of this script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402
from _tokens import estimate_tokens  # noqa: E402

DEFAULT_BUDGET = 4_000

# Order is alphabetical for reproducibility, but we hoist 'people' first
# (so the user's identity grounds everything else), then 'org', then 'decisions',
# then 'glossary'. Any new files are appended alphabetically.
PRIORITY_ORDER = ("people.md", "org.md", "decisions.md", "glossary.md")


def discover_kb_files(content_kb: Path, method_glossary: Path) -> tuple[list[Path], list[str]]:
    """Return (files_in_priority_order, errors). Errors are non-empty when split-source
    invariant is violated (per F2): the configured kb directory or the method glossary is
    unreachable, OR the user-content side of the split has zero non-glossary files
    (challenger C1: empty content_kb is the same silent-truncation failure F2 was
    scoped to catch).
    """
    errors: list[str] = []

    # Method glossary is mandatory if it doesn't exist in the method repo
    # (i.e. we expect kb/glossary.md to be a committed file). This catches the
    # "user moved kb/ wholesale into the vault and forgot to leave glossary in method".
    if not method_glossary.exists():
        errors.append(f"method-canonical glossary missing at {method_glossary}")

    if not content_kb.exists() or not content_kb.is_dir():
        errors.append(f"content kb directory missing at {content_kb}")

    files: list[Path] = []
    content_files: list[Path] = []
    if content_kb.is_dir():
        # Files from content_root/kb (excluding glossary.md if present in vault — method's wins)
        for path in sorted(content_kb.glob("*.md")):
            if path.is_file() and path.name != "glossary.md":
                content_files.append(path)
    files.extend(content_files)
    if method_glossary.exists():
        files.append(method_glossary)

    # C1 (challenger): empty content_kb directory → silently-truncated KB. Treat as
    # an error so callers under explicit config refuse rather than producing a
    # glossary-only output that's indistinguishable from "fully assembled".
    if content_kb.is_dir() and not content_files:
        errors.append(
            f"content kb directory at {content_kb} contains no non-glossary files; "
            f"expected user content like people.md / org.md / decisions.md — "
            f"did you forget to migrate or author them?"
        )

    # Apply priority order
    by_name = {p.name: p for p in files}
    in_priority = [by_name[name] for name in PRIORITY_ORDER if name in by_name]
    rest = [p for p in files if p.name not in PRIORITY_ORDER]
    return in_priority + rest, errors


def render(files: list[Path], method_root: Path, content_root: Path) -> str:
    sections: list[str] = [
        "# Personal-Assistant Knowledge Base (layer 3)",
        "",
        "This is the always-in-context knowledge the assistant carries. It is the user's",
        "ground-truth on org, recurring people, durable decisions, and project glossary.",
        "When answering, cite KB entries by their `## <heading>` when relevant.",
        "Never invent KB entries — if a fact isn't here, say so.",
        "",
    ]

    def _disp(p: Path) -> str:
        try:
            return f"method:{p.relative_to(method_root)}"
        except ValueError:
            try:
                return f"content:{p.relative_to(content_root)}"
            except ValueError:
                return str(p)

    for path in files:
        text = path.read_text(encoding="utf-8").rstrip() + "\n"
        rel = _disp(path)
        sections.append(f"<!-- BEGIN kb-file: {rel} -->")
        sections.append(text)
        sections.append(f"<!-- END kb-file: {rel} -->")
        sections.append("")
    return "\n".join(sections)


def count_tokens(text: str) -> int:
    return estimate_tokens(text)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Assemble layer-3 KB into a prompt slice.")
    parser.add_argument("--check", action="store_true", help="Exit 1 if the budget is exceeded.")
    parser.add_argument("--json", action="store_true", help="Output JSON with metadata.")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="Token budget (default 4000).")
    parser.add_argument("--strict-config", action="store_true", help="Refuse to fall back to method root when .assistant.local.json is missing/malformed (per #12 F2).")
    args = parser.parse_args(argv[1:])

    cfg = load_config(require_explicit_content_root=args.strict_config)
    content_kb = cfg.kb_content_root
    method_glossary = cfg.kb_method_glossary

    files, errors = discover_kb_files(content_kb, method_glossary)

    # Per #12 F2: if either source is unreachable AND we have an explicit config,
    # refuse to produce output. If we're in fallback mode (no config), the warning
    # was already emitted by load_config; we still try to assemble what we can find.
    if cfg.config_source == "file" and errors:
        for e in errors:
            print(f"[assemble-kb] ERROR: {e}", file=sys.stderr)
        print("[assemble-kb] refusing to assemble a partial KB when config is explicit (#12 F2).", file=sys.stderr)
        return 1

    if not files:
        if errors:
            for e in errors:
                print(f"[assemble-kb] ERROR: {e}", file=sys.stderr)
        print(f"[assemble-kb] no kb files found", file=sys.stderr)
        return 1

    rendered = render(files, cfg.method_root, cfg.content_root)
    tokens = count_tokens(rendered)

    def _disp(p: Path) -> str:
        try:
            return f"method:{p.relative_to(cfg.method_root)}"
        except ValueError:
            try:
                return f"content:{p.relative_to(cfg.content_root)}"
            except ValueError:
                return str(p)

    if args.json:
        payload = {
            "files": [_disp(p) for p in files],
            "token_count": tokens,
            "budget": args.budget,
            "within_budget": tokens <= args.budget,
            "config_source": cfg.config_source,
            "method_root": str(cfg.method_root),
            "content_root": str(cfg.content_root),
            "rendered": rendered,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(rendered)
        print(
            f"\n[assemble-kb] {len(files)} files, {tokens} tokens (budget {args.budget}); "
            f"config={cfg.config_source} content_root={cfg.content_root}",
            file=sys.stderr,
        )

    if tokens > args.budget:
        print(
            f"[assemble-kb] WARNING: token count {tokens} exceeds budget {args.budget}",
            file=sys.stderr,
        )
        if args.check:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
