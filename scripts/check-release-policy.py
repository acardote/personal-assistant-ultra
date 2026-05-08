#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""
Validate release-policy.yaml's shape and cross-check it against RELEASE.md.

Per #107: this script + RELEASE.md + release-policy.yaml are the three
pieces of the release-policy machinery. CI runs this on every push + PR
to fail loudly on drift between the human-readable and machine-readable
documents. Adapted from acardote/bruno-method/scripts/check-release-policy.py
to use this repo's PEP-723 / uv convention so CI doesn't need a separate
pip install step.

Exit codes:
  0 — OK
  1 — drift / validation failure (CI should fail)
"""

import os
import sys


_SURFACES_HEADING_RE = "## Surfaces in scope"


def _extract_surfaces_section(md_text: str) -> str:
    """Return the body of the `## Surfaces in scope` section of RELEASE.md
    (between that heading and the next `## ` heading). Empty string if the
    heading isn't found — the validator will then fail check 2 deterministically
    on every surface, which is the correct signal."""
    start = md_text.find(_SURFACES_HEADING_RE)
    if start == -1:
        return ""
    body_start = start + len(_SURFACES_HEADING_RE)
    next_heading = md_text.find("\n## ", body_start)
    if next_heading == -1:
        return md_text[body_start:]
    return md_text[body_start:next_heading]


def main() -> int:
    import yaml  # provided via PEP-723 dependencies above
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    yaml_path = os.path.join(repo_root, "release-policy.yaml")
    md_path = os.path.join(repo_root, "RELEASE.md")

    if not os.path.isfile(yaml_path):
        print(f"ERROR: {yaml_path} does not exist", file=sys.stderr)
        return 1
    if not os.path.isfile(md_path):
        print(f"ERROR: {md_path} does not exist", file=sys.stderr)
        return 1

    with open(yaml_path) as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            print(f"ERROR: release-policy.yaml is not valid YAML: {e}", file=sys.stderr)
            return 1

    if not isinstance(data, dict) or "surfaces" not in data:
        print("ERROR: release-policy.yaml must have a top-level 'surfaces' mapping", file=sys.stderr)
        return 1

    with open(md_path) as f:
        md_text = f.read()

    # Scope check-2 substring matching to the "## Surfaces in scope" section.
    # The full document mentions surface keys (`tools`, `docs`, `templates`,
    # `workflows`) as ordinary prose, so the bare-word fallback was producing
    # false negatives — drift in YAML wouldn't fail because the key happened
    # to appear elsewhere in RELEASE.md (per pr-challenger C2 on #114).
    md_surfaces_section = _extract_surfaces_section(md_text)

    fail = False

    # Check 1: every path listed in surfaces exists on disk.
    for surface_key, surface in data["surfaces"].items():
        paths = []
        if "path" in surface:
            paths.append(surface["path"])
        if "paths" in surface:
            paths.extend(surface["paths"])
        if not paths:
            print(f"FAIL: surface '{surface_key}' has no path or paths", file=sys.stderr)
            fail = True
            continue
        for p in paths:
            full = os.path.join(repo_root, p)
            if not os.path.exists(full):
                print(f"FAIL: surface '{surface_key}' references {p!r} which does not exist", file=sys.stderr)
                fail = True

    # Check 2: every surface key is mentioned in RELEASE.md's "Surfaces in
    # scope" section (NOT anywhere in the document — common-English words
    # like "tools", "templates", "docs" appear in prose throughout, which
    # would mask real drift). Per pr-challenger C2 on #114.
    section_lower = md_surfaces_section.lower()
    for surface_key in data["surfaces"]:
        # Tolerate underscores vs spaces (method_docs vs "method docs").
        variants = {surface_key.lower(), surface_key.lower().replace("_", " "), surface_key.lower().replace("_", "-")}
        s = data["surfaces"][surface_key]
        paths = []
        if "path" in s:
            paths.append(s["path"])
        if "paths" in s:
            paths.extend(s["paths"])
        path_in_section = any(p in md_surfaces_section for p in paths)
        key_in_section = any(v in section_lower for v in variants)
        if not (path_in_section or key_in_section):
            print(
                f"FAIL: surface '{surface_key}' (paths={paths}) is not mentioned "
                f"in RELEASE.md's '## Surfaces in scope' section",
                file=sys.stderr,
            )
            fail = True

    # Check 3: every workflow file under .github/workflows/ either appears
    # in surfaces.workflows.paths OR is documented as out-of-scope.
    workflows_dir = os.path.join(repo_root, ".github", "workflows")
    if os.path.isdir(workflows_dir):
        listed = set(data["surfaces"].get("workflows", {}).get("paths", []))
        for f in sorted(os.listdir(workflows_dir)):
            if not f.endswith((".yml", ".yaml")):
                continue
            rel = os.path.join(".github", "workflows", f)
            if rel not in listed:
                print(f"FAIL: workflow {rel} exists on disk but is not listed in release-policy.yaml surfaces.workflows.paths", file=sys.stderr)
                fail = True

    if fail:
        return 1
    print("OK — release-policy.yaml is in sync with RELEASE.md and disk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
