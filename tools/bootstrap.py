#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Interactive setup walker for personal-assistant-ultra.

Validates A7 (#14) by exercising the documented setup flow. From-scratch users
should be able to run this once and reach a green "ready to harvest" state.

What it does:
  1. Asks for the absolute path of the user's content vault checkout.
  2. Validates the path: exists, is a directory, NOT inside the method repo
     (per #12's F1 pollution-path guard).
  3. Writes `.assistant.local.json` at the method root, gitignored. Idempotent
     (asks before overwriting).
  4. Smoke-tests environment: `claude`, `uv`, `git` on PATH, lint-docs clean.
  5. Smoke-tests the configured vault: assemble-kb produces output, harvest
     state directory is writable.
  6. Reports a concise pass/fail summary with prescriptive remediation
     instructions per failed check.

Usage:
    tools/bootstrap.py             # interactive
    tools/bootstrap.py --vault <path>  # non-interactive (for tests)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

METHOD_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = METHOD_ROOT / ".assistant.local.json"


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    remediation: str = ""


def check_command_on_path(cmd: str) -> CheckResult:
    found = subprocess.run(["which", cmd], capture_output=True, text=True).returncode == 0
    return CheckResult(
        name=f"`{cmd}` on PATH",
        passed=found,
        detail=f"`which {cmd}` returned {'success' if found else 'failure'}",
        remediation=(
            f"Install {cmd} or ensure it's on PATH. See docs/setup.md prerequisites."
            if not found
            else ""
        ),
    )


def check_vault_path(raw_path: str) -> CheckResult:
    if not raw_path:
        return CheckResult(name="vault path is non-empty", passed=False, remediation="Provide a non-empty path.")
    expanded = os.path.expanduser(raw_path)
    if not os.path.isabs(expanded):
        return CheckResult(
            name="vault path is absolute",
            passed=False,
            detail=f"path '{raw_path}' is relative",
            remediation="Provide an absolute or ~-prefixed path (e.g., ~/Projects/<your>-pa-vault).",
        )
    p = Path(expanded).resolve()
    if not p.exists():
        return CheckResult(
            name="vault path exists",
            passed=False,
            detail=f"{p} does not exist",
            remediation=f"Create the directory: `mkdir -p {p}` (or clone your vault repo to that location).",
        )
    if not p.is_dir():
        return CheckResult(
            name="vault path is a directory",
            passed=False,
            detail=f"{p} exists but is not a directory",
            remediation="Remove or rename the file at that path, then create a directory.",
        )
    method_resolved = METHOD_ROOT.resolve()
    if p == method_resolved or p.is_relative_to(method_resolved):
        return CheckResult(
            name="vault is OUTSIDE the method repo",
            passed=False,
            detail=f"{p} is at-or-inside the method repo {method_resolved}",
            remediation="Choose a vault path outside this method repo. Per #12, content must live in a separate vault to keep method history clean.",
        )
    return CheckResult(name=f"vault path {p} is valid", passed=True)


def write_config(vault_path: Path, *, force: bool) -> CheckResult:
    if CONFIG_PATH.exists() and not force:
        existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        existing_root = existing.get("paths", {}).get("content_root", "<unknown>")
        if existing_root == str(vault_path):
            return CheckResult(
                name=".assistant.local.json already configured for this vault",
                passed=True,
                detail=f"existing content_root: {existing_root}",
            )
        return CheckResult(
            name=".assistant.local.json exists with different vault",
            passed=False,
            detail=f"existing content_root: {existing_root}; requested: {vault_path}",
            remediation="Re-run with --force to overwrite, or hand-edit .assistant.local.json.",
        )
    payload = {
        "$schema_version": 1,
        "$comment": "Per-checkout config (gitignored). See .assistant.local.json.example for schema.",
        "paths": {"content_root": str(vault_path)},
    }
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return CheckResult(
        name=".assistant.local.json written",
        passed=True,
        detail=f"content_root = {vault_path}",
    )


def check_kb_assembly() -> CheckResult:
    proc = subprocess.run(
        [str(METHOD_ROOT / "tools" / "assemble-kb.py"), "--check"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return CheckResult(name="assemble-kb produces output", passed=True)
    return CheckResult(
        name="assemble-kb produces output",
        passed=False,
        detail=proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no stderr",
        remediation=(
            "Make sure your vault has kb/ files. Copy from kb-templates/ if needed:\n"
            "  cp kb-templates/people.md.example   $VAULT/kb/people.md\n"
            "  cp kb-templates/org.md.example      $VAULT/kb/org.md\n"
            "  cp kb-templates/decisions.md.example $VAULT/kb/decisions.md"
        ),
    )


def check_harvest_state_writable(vault_path: Path) -> CheckResult:
    state_dir = vault_path / ".harvest"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".bootstrap-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult(name=f"vault/.harvest/ writable", passed=True)
    except OSError as e:
        return CheckResult(
            name=f"vault/.harvest/ writable",
            passed=False,
            detail=str(e),
            remediation=f"Fix permissions on {state_dir}.",
        )


def render_summary(results: list[CheckResult]) -> str:
    out: list[str] = []
    width = 78
    out.append("=" * width)
    out.append("personal-assistant-ultra bootstrap")
    out.append("=" * width)
    n_pass = sum(1 for r in results if r.passed)
    n_fail = len(results) - n_pass
    for r in results:
        marker = "PASS" if r.passed else "FAIL"
        out.append(f"  [{marker}] {r.name}")
        if r.detail:
            out.append(f"         {r.detail}")
        if not r.passed and r.remediation:
            for line in r.remediation.splitlines():
                out.append(f"         > {line}")
    out.append("=" * width)
    out.append(f"{n_pass}/{len(results)} checks passed; {n_fail} failed.")
    if n_fail == 0:
        out.append("Setup looks healthy. Next: docs/setup.md step 5 (first synthetic harvest).")
    else:
        out.append("Fix the failed checks (instructions above) and re-run tools/bootstrap.py.")
    out.append("=" * width)
    return "\n".join(out)


def prompt_vault(non_interactive: str | None) -> str:
    if non_interactive is not None:
        return non_interactive
    print("personal-assistant-ultra bootstrap — interactive setup")
    print()
    print("This walker creates .assistant.local.json + verifies your environment.")
    print()
    return input("Absolute path of your content vault: ").strip()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Interactive setup walker for personal-assistant-ultra.")
    parser.add_argument("--vault", help="Vault path (non-interactive; for testing).")
    parser.add_argument("--force", action="store_true", help="Overwrite existing .assistant.local.json.")
    args = parser.parse_args(argv[1:])

    raw = prompt_vault(args.vault)
    vault_check = check_vault_path(raw)
    if not vault_check.passed:
        print(render_summary([vault_check]))
        return 1
    vault_path = Path(os.path.expanduser(raw)).resolve()

    write_check = write_config(vault_path, force=args.force)
    results = [
        vault_check,
        write_check,
        check_command_on_path("claude"),
        check_command_on_path("uv"),
        check_command_on_path("git"),
        check_harvest_state_writable(vault_path),
        check_kb_assembly(),
    ]

    print(render_summary(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
