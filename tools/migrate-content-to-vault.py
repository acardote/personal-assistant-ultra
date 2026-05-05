#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""One-time migration: move user-content from the method repo to the content vault.

Run this once after creating the content vault repo + setting up `.assistant.local.json`.
The script:

  1. Refuses to run without a valid `.assistant.local.json` (uses --strict-config).
  2. Refuses to run if content_root contains anything besides `.git/` (no clobbering).
  3. Copies method-repo `kb/{people,org,decisions}.md` -> `<content_root>/kb/`.
  4. Reports what was copied; does NOT delete from the method repo (that's a deliberate
     follow-up step the user takes after verifying the vault has the content).
  5. Optionally `--also-harvest-state` copies `<method>/.harvest/<source>.json` to the
     vault. By default these are skipped because today's dedup keys carry absolute
     paths from the harvesting machine; the keys regenerate cleanly on next harvest.

Usage:
    tools/migrate-content-to-vault.py            # dry-run by default; reports what it would do
    tools/migrate-content-to-vault.py --apply    # actually copy files
    tools/migrate-content-to-vault.py --apply --also-harvest-state

The script is idempotent against the method repo: re-running with `--apply` after
content already exists in the vault is a no-op for the kb files (refuses to copy on top).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402

KB_FILES_TO_MIGRATE = ("people.md", "org.md", "decisions.md")


def list_vault_contents(vault: Path) -> list[Path]:
    """Returns vault entries excluding .git/."""
    return [p for p in vault.iterdir() if p.name != ".git"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Migrate user content from method repo to vault.")
    parser.add_argument("--apply", action="store_true", help="Actually copy files (default: dry-run).")
    parser.add_argument(
        "--also-harvest-state",
        action="store_true",
        help="Also migrate .harvest/<source>.json. Default: false (today's dedup keys aren't portable; let them regenerate on the vault machine).",
    )
    parser.add_argument(
        "--allow-non-empty-vault",
        action="store_true",
        help="Allow migration into a vault that has content beyond .git/. Use with care; intended for re-runs after partial migration.",
    )
    args = parser.parse_args(argv[1:])

    cfg = load_config(require_explicit_content_root=True)  # refuses without .assistant.local.json
    method = cfg.method_root
    vault = cfg.content_root

    print(f"[migrate] method root: {method}", file=sys.stderr)
    print(f"[migrate] vault root:  {vault}", file=sys.stderr)

    vault_contents = list_vault_contents(vault)
    if vault_contents and not args.allow_non_empty_vault:
        print(f"[migrate] ERROR: vault has existing content besides .git/:", file=sys.stderr)
        for p in sorted(vault_contents)[:10]:
            print(f"  - {p.name}", file=sys.stderr)
        print(
            "[migrate] refusing to migrate into a non-empty vault. Pass "
            "--allow-non-empty-vault if you know what you're doing.",
            file=sys.stderr,
        )
        return 1

    plan: list[tuple[Path, Path, str]] = []  # (source, dest, kind)

    method_kb = method / "kb"
    vault_kb = vault / "kb"
    for filename in KB_FILES_TO_MIGRATE:
        src = method_kb / filename
        if not src.exists():
            print(f"[migrate] skip kb/{filename} (not present in method repo — already migrated?)", file=sys.stderr)
            continue
        dest = vault_kb / filename
        if dest.exists():
            print(f"[migrate] skip kb/{filename} (already exists in vault)", file=sys.stderr)
            continue
        plan.append((src, dest, "kb file"))

    if args.also_harvest_state:
        method_harvest = method / ".harvest"
        vault_harvest = vault / ".harvest"
        if method_harvest.exists():
            for f in sorted(method_harvest.glob("*.json")):
                if f.name.endswith("-credentials.json") or f.name.endswith("-token.json"):
                    print(f"[migrate] skip {f.name} (credential file — never migrate)", file=sys.stderr)
                    continue
                dest = vault_harvest / f.name
                if dest.exists():
                    continue
                plan.append((f, dest, "harvest state"))

    if not plan:
        print("[migrate] nothing to migrate.", file=sys.stderr)
        return 0

    print(f"\n[migrate] {'APPLY' if args.apply else 'DRY-RUN'} — {len(plan)} file(s) to copy:", file=sys.stderr)
    for src, dest, kind in plan:
        print(f"  [{kind}] {src.relative_to(method)}  ->  {dest.relative_to(vault)}", file=sys.stderr)

    if not args.apply:
        print("\n[migrate] dry-run only. Pass --apply to actually copy.", file=sys.stderr)
        return 0

    for src, dest, _kind in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    print(f"\n[migrate] copied {len(plan)} file(s) into vault at {vault}.", file=sys.stderr)
    print("\n[migrate] next steps (you do these manually):", file=sys.stderr)
    print(f"  1. cd {vault} && git add . && git commit -m 'initial: migrated kb from method repo' && git push", file=sys.stderr)
    print(f"  2. After verifying the vault has the content, remove the originals from the method repo:", file=sys.stderr)
    for src, _dest, _kind in plan:
        if src.is_relative_to(method):
            print(f"     git rm {src.relative_to(method)}", file=sys.stderr)
    print("  3. Commit the method-repo removals on the same feature branch as the migration script.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
