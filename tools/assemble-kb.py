#!/usr/bin/env -S uv run --quiet --with tiktoken --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["tiktoken>=0.7"]
# ///
"""Assemble layer-3 knowledge base files into a single rendered prompt slice.

Usage:
    tools/assemble-kb.py            # print rendered KB to stdout, token count to stderr
    tools/assemble-kb.py --check    # verify ≤4K token budget; exit 1 if exceeded
    tools/assemble-kb.py --json     # output JSON with metadata + rendered string

The output is the "always-in-context" content that the assistant should load on every
invocation. It is the materialization of layer 3 in the three-layer memory architecture.

Reproducibility: same input files → same output (file order is alphabetical; no
timestamps or randomness in the output). This is the basis for acceptance criterion 4.

The token budget (4K, configurable via --budget) is intended to actively force signal/
noise tradeoffs in KB curation. If the budget is routinely hit and contributors compress
load-bearing content, OR is never approached and KB drifts to a dumping ground, falsifier
F3 on issue #4 fires and the budget design is wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import tiktoken

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_DIR = PROJECT_ROOT / "kb"
DEFAULT_BUDGET = 4_000

# Order is alphabetical for reproducibility, but we hoist 'people' first
# (so the user's identity grounds everything else), then 'org', then 'decisions',
# then 'glossary'. Any new files are appended alphabetically.
PRIORITY_ORDER = ("people.md", "org.md", "decisions.md", "glossary.md")


def discover_kb_files() -> list[Path]:
    files = sorted(p for p in KB_DIR.glob("*.md") if p.is_file())
    in_priority = [KB_DIR / name for name in PRIORITY_ORDER if (KB_DIR / name).exists()]
    rest = [p for p in files if p.name not in PRIORITY_ORDER]
    return in_priority + rest


def render(files: list[Path]) -> str:
    sections: list[str] = [
        "# Personal-Assistant Knowledge Base (layer 3)",
        "",
        "This is the always-in-context knowledge the assistant carries. It is the user's",
        "ground-truth on org, recurring people, durable decisions, and project glossary.",
        "When answering, cite KB entries by their `## <heading>` when relevant.",
        "Never invent KB entries — if a fact isn't here, say so.",
        "",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8").rstrip() + "\n"
        rel = path.relative_to(PROJECT_ROOT)
        sections.append(f"<!-- BEGIN kb-file: {rel} -->")
        sections.append(text)
        sections.append(f"<!-- END kb-file: {rel} -->")
        sections.append("")
    return "\n".join(sections)


def count_tokens(text: str) -> int:
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Assemble layer-3 KB into a prompt slice.")
    parser.add_argument("--check", action="store_true", help="Exit 1 if the budget is exceeded.")
    parser.add_argument("--json", action="store_true", help="Output JSON with metadata.")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="Token budget (default 4000).")
    args = parser.parse_args(argv[1:])

    files = discover_kb_files()
    if not files:
        print(f"no kb files found under {KB_DIR}", file=sys.stderr)
        return 1

    rendered = render(files)
    tokens = count_tokens(rendered)

    if args.json:
        payload = {
            "files": [str(p.relative_to(PROJECT_ROOT)) for p in files],
            "token_count": tokens,
            "budget": args.budget,
            "within_budget": tokens <= args.budget,
            "rendered": rendered,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(rendered)
        print(
            f"\n[assemble-kb] {len(files)} files, {tokens} tokens (budget {args.budget})",
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
