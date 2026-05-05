#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Acceptance tests for #13 — backup/migrate tooling.

Tests:
  T1 — backup → restore round-trip preserves files + checksums.
  T2 — backup discovers new top-level dirs (e.g., journals/, attachments/)
       added under content_root. Walk-based, not hardcoded-path-based —
       closes challenger F1 ("silent omission via hardcoded path list").
  T3 — restore refuses to extract over a non-empty target without --force
       (challenger F2 — safe-by-default).
  T4 — --include-raw actually includes raw/ files. NOT git-aware so even
       gitignored files get into the archive (challenger F3).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run(*args: str) -> tuple[int, str, str]:
    result = subprocess.run(args, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def main() -> int:
    backup_tool = PROJ / "tools" / "backup-content.py"

    # Build a synthetic content_root with the structure backup-content.py expects.
    tmp = Path(tempfile.mkdtemp(prefix="backup-test-"))
    try:
        vault = tmp / "vault"
        method = tmp / "method"
        method.mkdir()
        vault.mkdir()
        (vault / ".git").mkdir()  # should be skipped
        write(vault / "memory" / "examples" / "doc.md", "memory body")
        write(vault / "kb" / "people.md", "kb body")
        write(vault / ".harvest" / "slack.json", "{}")
        write(vault / ".harvest" / "gmail-credentials.json", "{}")  # secret — excluded by default
        write(vault / "raw" / "slack" / "thread.md", "raw body")  # excluded by default
        write(vault / "journals" / "2026-05.md", "journal body")  # NEW top-level dir — F1 probe

        # Need a method-checkout context for backup-content.py to read .assistant.local.json.
        # Simulate by copying the tool + config into the temp method dir.
        (method / "tools").mkdir()
        for f in ("backup-content.py", "_config.py"):
            shutil.copy2(PROJ / "tools" / f, method / "tools" / f)
        (method / ".assistant.local.json").write_text(
            f'{{"paths": {{"content_root": "{vault}"}}}}', encoding="utf-8"
        )
        (method / "tools" / "backup-content.py").chmod(0o755)
        # Update the script's METHOD_ROOT search by symlinking _config.py at the right place.
        # _config.py uses `Path(__file__).resolve().parent.parent` to find method root, which
        # is `<method>/tools/_config.py` -> `<method>`. So <method>/.assistant.local.json
        # resolves correctly.

        # T1 + T2 — backup with defaults (excludes raw + credentials).
        out_path = tmp / "backup.tar.gz"
        rc, _, err = run(str(method / "tools" / "backup-content.py"), "--out", str(out_path))
        assert rc == 0, f"backup failed: {err}"

        with tarfile.open(out_path, "r:gz") as tf:
            arc_names = set(tf.getnames())
        print(f"Test T1 — backup contains expected files")
        print(f"  archive members: {sorted(arc_names)}")
        assert "manifest.json" in arc_names, "manifest missing"
        assert "memory/examples/doc.md" in arc_names
        assert "kb/people.md" in arc_names
        assert ".harvest/slack.json" in arc_names
        # Defaults exclude:
        assert "raw/slack/thread.md" not in arc_names, "raw/ should be excluded by default"
        assert ".harvest/gmail-credentials.json" not in arc_names, "credentials should be excluded by default"
        # F1 probe (T2): journal dir picked up by walk
        assert "journals/2026-05.md" in arc_names, "F1: journals/ silently omitted"
        print("  PASS — defaults select correct files; T2/F1 (journals/ picked up by walk)\n")

        # T3 — restore over non-empty target refuses
        target = tmp / "restored"
        target.mkdir()
        (target / "existing.md").write_text("existing")
        rc, _, err = run(str(method / "tools" / "backup-content.py"),
                         "--restore", str(out_path), "--target", str(target))
        print("Test T3 — restore refuses non-empty target without --force")
        print(f"  exit={rc}; stderr tail: {err.strip().splitlines()[-1] if err.strip() else '<empty>'}")
        assert rc == 1, "restore should refuse"
        assert "non-empty" in err, "expected non-empty refusal message"
        # With --force, it goes through:
        rc, _, err = run(str(method / "tools" / "backup-content.py"),
                         "--restore", str(out_path), "--target", str(target), "--force")
        assert rc == 0, f"restore --force should succeed: {err}"
        print(f"  PASS — refuses without --force; succeeds with --force\n")

        # Verify round-trip: extracted content matches original
        for original in vault.rglob("*"):
            if original.is_file():
                rel = original.relative_to(vault)
                # Skip excluded files
                if rel.parts and rel.parts[0] == "raw":
                    continue
                if "credentials" in original.name:
                    continue
                if rel.parts and rel.parts[0] == ".git":
                    continue
                restored_path = target / rel
                assert restored_path.exists(), f"missing in restore: {rel}"
                assert restored_path.read_bytes() == original.read_bytes(), f"content mismatch: {rel}"

        # T4 — --include-raw actually picks up gitignored raw/
        out_path_raw = tmp / "backup-with-raw.tar.gz"
        rc, _, err = run(str(method / "tools" / "backup-content.py"),
                         "--out", str(out_path_raw), "--include-raw")
        assert rc == 0
        with tarfile.open(out_path_raw, "r:gz") as tf:
            arc_names_raw = set(tf.getnames())
        print("Test T4 — --include-raw pulls in raw/ via filesystem walk (not git-aware)")
        assert "raw/slack/thread.md" in arc_names_raw, "F3: raw not included with --include-raw"
        # Credentials still excluded unless --include-credentials also given.
        assert ".harvest/gmail-credentials.json" not in arc_names_raw
        print("  PASS — --include-raw scoops up gitignored raw/ contents (F3)\n")

        print("=== All #13 acceptance tests passed ===")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
