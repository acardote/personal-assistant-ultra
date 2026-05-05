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

# Default doc-file globs. Covers all user-facing markdown surfaces:
#   - SKILL.md and any sibling docs in skill directories
#   - All top-level markdown (README.md, CLAUDE.md, CONTRIBUTING.md, ...)
#   - docs/
#   - kb-templates/ (templates legitimately reference kb/<file>.md but always
#     same-line-qualified with "your vault" — caught by allow rule)
DEFAULT_GLOBS = [
    ".claude/skills/**/*.md",
    "*.md",
    "docs/**/*.md",
    "kb-templates/**/*.md",
    "templates/**/*.md",
]

# Regex patterns. Each is (name, pattern, allow-when-same-line-contains, path-filter-substring).
# Allow-terms are matched on the SAME LINE only (not the preceding 2 lines as
# in v1). Per challenger C2 on PR #19: cross-line context allowed unrelated
# antecedents to grant immunity to bare references nearby.
# Allow-terms are also tightened to specific tokens (slash-prefixed or
# multi-word phrases) so they can't be matched by incidental prose like
# "for example" — challenger C1/C3 on PR #19.
# Path-filter-substring (last element): if non-empty, the rule applies ONLY to
# files whose path contains the substring. Used to scope template-only checks.
PATTERNS = [
    (
        "kb-people",
        re.compile(r"\bkb/people\.md\b"),
        ("<content_root>/", "your vault", "kb-templates/", ".md.example", "<!-- legacy -->"),
        "",
    ),
    (
        "kb-org",
        re.compile(r"\bkb/org\.md\b"),
        ("<content_root>/", "your vault", "kb-templates/", ".md.example", "<!-- legacy -->"),
        "",
    ),
    (
        "kb-decisions",
        re.compile(r"\bkb/decisions\.md\b"),
        ("<content_root>/", "your vault", "kb-templates/", ".md.example", "<!-- legacy -->"),
        "",
    ),
    # `memory/<source-kind>/` paths in user-facing docs imply the method repo.
    # Source-kind class extended to [a-z0-9_-]+ to catch hyphens (file-drop)
    # and digits — challenger S1 on PR #19.
    (
        "memory-source-kind",
        re.compile(r"\bmemory/[a-z0-9_-]+/[^\s\)\]\"`]+\.md\b"),
        ("<content_root>/", "your vault", "memory/examples/", "<!-- legacy -->"),
        "",
    ),
    # Hardcoded Slack user IDs in TEMPLATE files are footguns: a fork-and-forget
    # user pastes the literal prompt and silently DMs the original maintainer
    # instead of themselves. Templates must use `<YOUR_SLACK_USER_ID>` placeholder.
    # Slack IDs are [UW][A-Z0-9]{8,10} (U=user, W=workspace, plus enterprise).
    # Per #32 round-1 challenger finding.
    # Path-filter "templates/routines/" scopes the check to routine templates only —
    # ADRs, READMEs, and other docs may legitimately reference Slack IDs (e.g., as
    # examples, in probe results, or in user-specific notes that aren't templates).
    (
        "hardcoded-slack-id-in-routine-template",
        re.compile(r"\b[UW][A-Z0-9]{8,12}\b"),
        ("<YOUR_SLACK_USER_ID>", "<!-- example -->", "for example", "regex", "[A-Z0-9]"),
        "templates/routines/",
    ),
]


def lint_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        path_for_filter = str(path.relative_to(METHOD_ROOT))
    except ValueError:
        path_for_filter = str(path)
    violations: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        for name, pattern, allow_terms, path_filter in PATTERNS:
            if path_filter and path_filter not in path_for_filter:
                continue
            for m in pattern.finditer(line):
                # Same-line allowance only — see PATTERNS docstring for why.
                if any(term in line for term in allow_terms):
                    continue
                violations.append(
                    f"{path_for_filter}:{line_no}: [{name}] {line.strip()}"
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
        "Qualify (on the SAME line as the path) with one of: `<content_root>/...`, "
        "`your vault`, `kb-templates/...`, `.md.example`, `memory/examples/...`, or "
        "`<!-- legacy -->`. Or remove the reference. See #18 for the policy.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
