#!/usr/bin/env -S uv run --quiet --with jsonschema --with pyyaml --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4", "pyyaml>=6"]
# ///
"""Validate a memory-object Markdown file against docs/schemas/memory-object.schema.json.

Usage:
    tools/validate-memory-object.py memory/examples/2026-q2-platform-strategy.md
    tools/validate-memory-object.py memory/examples/*.md

Exits 0 on success, 1 on failure. Prints validation errors with file paths.

Reuses the file's own YAML frontmatter (between the first two `---` lines).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "docs" / "schemas" / "memory-object.schema.json"


def load_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: no YAML frontmatter (file must start with '---')")
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        raise ValueError(f"{path}: frontmatter not closed (expected '\\n---\\n')")
    front_text = parts[0][4:]  # strip leading '---\n'
    data = yaml.safe_load(front_text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: frontmatter is not a YAML mapping")
    return data


def validate_provenance(memo: dict, memo_path: Path) -> list[str]:
    """Same-machine provenance check: for file: URIs, ensure the target exists."""
    errors: list[str] = []
    uri = memo.get("source_uri", "")
    if uri.startswith("file:"):
        rel = uri[len("file:") :]
        target = (PROJECT_ROOT / rel).resolve() if rel.startswith("./") or not rel.startswith("/") else Path(rel)
        if rel.startswith("./"):
            target = (PROJECT_ROOT / rel[2:]).resolve()
        if not target.exists():
            errors.append(
                f"{memo_path}: source_uri '{uri}' does not resolve to an existing file "
                f"(checked {target})"
            )
    return errors


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: validate-memory-object.py <memory-object-path> [more...]", file=sys.stderr)
        return 2

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    failures = 0
    for arg in argv[1:]:
        path = Path(arg)
        try:
            front = load_frontmatter(path)
        except (OSError, ValueError) as exc:
            print(f"FAIL {path}: {exc}")
            failures += 1
            continue

        schema_errors = sorted(validator.iter_errors(front), key=lambda e: list(e.absolute_path))
        provenance_errors = validate_provenance(front, path)

        if not schema_errors and not provenance_errors:
            print(f"OK   {path}: schema valid, source_uri resolves")
            continue

        failures += 1
        for err in schema_errors:
            loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
            print(f"FAIL {path}: schema error at '{loc}': {err.message}")
        for msg in provenance_errors:
            print(f"FAIL {msg}")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
