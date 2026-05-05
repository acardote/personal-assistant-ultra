#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Lint in-repo docs for stale references that imply user content lives in the
method repo.

Per #18 F3: A6 (method/content separation) has no enforcement surface unless we
catch doc regressions mechanically. This script is the gate.

What it catches:
  - Bare references to `kb/people.md`, `kb/org.md`, `kb/decisions.md` that
    don't qualify as `<content_root>/kb/...` or "in your vault".
  - Bare `memory/<source-kind>/...` references in claim-shape positions
    that imply method-repo location.

What it does NOT catch:
  - References to `kb/glossary.md` (correctly method-side).
  - References inside `.bruno/`, `.github/`, `.claude/`, `tools/`, schema docs,
    or test fixtures (these are tooling internals, not user-facing docs).
  - References inside historical / changelog blocks marked `<!-- legacy -->`.

Usage:
    tools/lint-docs.py            # exits 0 if clean, 1 if violations
    tools/lint-docs.py --files X  # lint only specific files

Designed to run cheaply in CI on every PR that touches docs / SKILL.md.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

METHOD_ROOT = Path(__file__).resolve().parent.parent

# Default doc-file globs to lint. SKILL.md is the load-bearing one; README and docs/
# are the broader user-facing surface.
DEFAULT_GLOBS = [
    ".claude/skills/**/SKILL.md",
    "README.md",
    "docs/**/*.md",
]

# Regex patterns. Each is (name, pattern, allow-when-context-contains).
# `allow-when-context-contains` is a substring whose presence in the same line
# (or the previous 2 lines) marks the reference as intentionally qualified —
# e.g. "<content_root>/kb/people.md" or "in your vault".
PATTERNS = [
    (
        "kb-people",
        re.compile(r"\bkb/people\.md\b"),
        ("<content_root>", "in your vault", "kb-templates", ".example"),
    ),
    (
        "kb-org",
        re.compile(r"\bkb/org\.md\b"),
        ("<content_root>", "in your vault", "kb-templates", ".example"),
    ),
    (
        "kb-decisions",
        re.compile(r"\bkb/decisions\.md\b"),
        ("<content_root>", "in your vault", "kb-templates", ".example"),
    ),
    # `memory/<source-kind>/` paths in user-facing docs imply the method repo.
    # Allow when explicitly content-rooted, fixture-pathed (memory/examples/), or
    # tagged as a legacy reference.
    (
        "memory-source-kind",
        re.compile(r"\bmemory/[a-z_]+/[^\s\)\]\"`]+\.md\b"),
        ("<content_root>", "in your vault", "memory/examples/", "<!-- legacy -->"),
    ),
]


def lint_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    violations: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        # Context window: this line + 2 preceding (allows stating a "vault" framing
        # in the sentence before the reference).
        ctx_lines = lines[max(0, line_no - 3) : line_no]
        ctx = "\n".join(ctx_lines)
        for name, pattern, allow_terms in PATTERNS:
            for m in pattern.finditer(line):
                if any(term in ctx for term in allow_terms):
                    continue
                try:
                    disp = str(path.relative_to(METHOD_ROOT))
                except ValueError:
                    disp = str(path)
                violations.append(
                    f"{disp}:{line_no}: [{name}] {line.strip()}"
                )
    return violations


def discover_files(globs: list[str]) -> list[Path]:
    files: list[Path] = []
    for g in globs:
        for p in METHOD_ROOT.glob(g):
            if p.is_file():
                files.append(p)
    # Dedup + stable order
    return sorted(set(files))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Lint in-repo docs for stale method/content references.")
    parser.add_argument(
        "--files",
        nargs="*",
        help="Specific files to lint (default: discover via DEFAULT_GLOBS).",
    )
    args = parser.parse_args(argv[1:])

    files = [Path(f).resolve() for f in args.files] if args.files else discover_files(DEFAULT_GLOBS)
    if not files:
        print("[lint-docs] no files matched.", file=sys.stderr)
        return 0

    all_violations: list[str] = []
    for path in files:
        all_violations.extend(lint_file(path))

    if not all_violations:
        print(f"[lint-docs] {len(files)} file(s) checked, clean.", file=sys.stderr)
        return 0

    print(f"[lint-docs] {len(all_violations)} violation(s) in {len(files)} file(s):", file=sys.stderr)
    for v in all_violations:
        print(f"  {v}", file=sys.stderr)
    print(
        "\n[lint-docs] These references imply user content lives in the method repo. "
        "Qualify with `<content_root>/kb/...` / `in your vault` / `memory/examples/` "
        "/ `<!-- legacy -->`, or remove. See #18 for the policy.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
