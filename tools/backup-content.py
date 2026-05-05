#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Backup the content vault to a single local tar.gz file. Standalone of git.

Usage:
    tools/backup-content.py                              # backup with defaults
    tools/backup-content.py --out path/to/file.tar.gz    # custom output path
    tools/backup-content.py --include-raw                # also include raw/
    tools/backup-content.py --include-credentials        # also include creds (DEFAULT OFF)
    tools/backup-content.py --restore archive.tar.gz --target /path/to/vault
    tools/backup-content.py --restore archive.tar.gz --target /path/to/vault --force

Backup behaviour:
- Reads `content_root` via tools/_config.py (refuses without explicit config).
- Walks `<content_root>` directly (NOT git-aware — captures gitignored files
  too, so `--include-raw` actually picks up the local-only raw archive per
  challenger F3 on PR #20).
- By default includes everything under `content_root` EXCEPT `raw/` (PII)
  and `*-credentials.json` / `*-token.json` (secrets). Flags opt those in.
- Includes a top-level `manifest.json` in the archive with: schema_version,
  timestamp, content_root path at backup time, included paths, per-file
  SHA-256, the flags used.
- Default output: `~/personal-assistant-backups/personal-assistant-content-<UTC>.tar.gz`.

Restore behaviour:
- Validates manifest schema + per-file checksums before extracting.
- Refuses on a non-empty target unless `--force`. Default safe-by-default
  per challenger F2 on the original #13 falsifiers.
- Does NOT delete files in the target that aren't in the archive — restore
  is additive. Use a clean target dir when you want a pristine result.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402

SCHEMA_VERSION = 1
DEFAULT_BACKUP_DIR = Path.home() / "personal-assistant-backups"
CREDENTIAL_GLOBS = ("*-credentials.json", "*-token.json", "*-credentials/*")


@dataclass
class BackupSpec:
    content_root: Path
    out_path: Path
    include_raw: bool
    include_credentials: bool


def is_credential_path(rel_path: Path) -> bool:
    name = rel_path.name
    if name.endswith("-credentials.json") or name.endswith("-token.json"):
        return True
    return any(part.endswith("-credentials") for part in rel_path.parts)


def is_under(rel_path: Path, top: str) -> bool:
    parts = rel_path.parts
    return bool(parts) and parts[0] == top


def discover_files(spec: BackupSpec) -> list[Path]:
    """Walk content_root and select files for backup. Returns absolute paths."""
    selected: list[Path] = []
    for root, dirs, files in os.walk(spec.content_root):
        root_path = Path(root)
        # Skip .git and .archive subdirectories — neither belongs in a content backup.
        dirs[:] = [d for d in dirs if d not in (".git", ".archive")]
        for fname in files:
            f = root_path / fname
            rel = f.relative_to(spec.content_root)
            if not spec.include_raw and is_under(rel, "raw"):
                continue
            if not spec.include_credentials and is_credential_path(rel):
                continue
            selected.append(f)
    return sorted(selected)


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(spec: BackupSpec, files: list[Path]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "content_root_at_backup_time": str(spec.content_root),
        "included_raw": spec.include_raw,
        "included_credentials": spec.include_credentials,
        "files": [
            {
                "path": str(f.relative_to(spec.content_root)),
                "size_bytes": f.stat().st_size,
                "sha256": sha256_of_file(f),
            }
            for f in files
        ],
    }


def do_backup(spec: BackupSpec) -> int:
    files = discover_files(spec)
    if not files:
        print(f"[backup] no files to back up under {spec.content_root}", file=sys.stderr)
        return 1
    manifest = build_manifest(spec, files)
    spec.out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(spec.out_path, "w:gz") as tf:
        for f in files:
            arcname = f.relative_to(spec.content_root)
            tf.add(f, arcname=str(arcname))
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))
    print(
        f"[backup] wrote {spec.out_path} ({spec.out_path.stat().st_size} bytes, "
        f"{len(files)} files; raw={spec.include_raw} credentials={spec.include_credentials})",
        file=sys.stderr,
    )
    print(str(spec.out_path))
    return 0


def do_restore(archive_path: Path, target: Path, force: bool) -> int:
    if not archive_path.exists():
        print(f"[restore] archive not found: {archive_path}", file=sys.stderr)
        return 1
    target = target.resolve()
    if target.exists() and any(target.iterdir()) and not force:
        print(
            f"[restore] target {target} is non-empty. Refusing to extract on top of existing content. "
            "Use --force if you really mean to.",
            file=sys.stderr,
        )
        return 1

    with tarfile.open(archive_path, "r:gz") as tf:
        try:
            manifest_member = tf.getmember("manifest.json")
        except KeyError:
            print("[restore] archive has no manifest.json — refusing to restore.", file=sys.stderr)
            return 1
        manifest_bytes = tf.extractfile(manifest_member).read()
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError as exc:
            print(f"[restore] manifest is not valid JSON: {exc}", file=sys.stderr)
            return 1
        if manifest.get("schema_version") != SCHEMA_VERSION:
            print(
                f"[restore] manifest schema_version {manifest.get('schema_version')} "
                f"!= expected {SCHEMA_VERSION}",
                file=sys.stderr,
            )
            return 1

        # Pre-validate checksums against archive contents BEFORE extracting.
        members_by_path = {m.name: m for m in tf.getmembers()}
        for entry in manifest.get("files", []):
            rel = entry["path"]
            if rel not in members_by_path:
                print(f"[restore] manifest references {rel} but archive doesn't contain it.", file=sys.stderr)
                return 1
            data = tf.extractfile(members_by_path[rel]).read()
            actual = hashlib.sha256(data).hexdigest()
            if actual != entry["sha256"]:
                print(
                    f"[restore] checksum mismatch for {rel}: "
                    f"manifest={entry['sha256']} archive={actual}",
                    file=sys.stderr,
                )
                return 1

        # Extract.
        target.mkdir(parents=True, exist_ok=True)
        for entry in manifest.get("files", []):
            rel = entry["path"]
            member = members_by_path[rel]
            data = tf.extractfile(member).read()
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

    print(
        f"[restore] extracted {len(manifest.get('files', []))} file(s) to {target} "
        f"(from backup at {manifest.get('timestamp_utc')})",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Backup or restore the content vault.")
    parser.add_argument("--restore", help="Path to a backup archive to restore from. If set, runs restore mode.")
    parser.add_argument("--target", help="Restore target directory (required with --restore).")
    parser.add_argument("--force", action="store_true", help="Allow --restore to extract over a non-empty target.")
    parser.add_argument("--out", help="Backup output path (default: ~/personal-assistant-backups/...)")
    parser.add_argument("--include-raw", action="store_true", help="Include `raw/` (default off — PII protection).")
    parser.add_argument("--include-credentials", action="store_true", help="Include credential files (default off — secret protection).")
    args = parser.parse_args(argv[1:])

    if args.restore:
        if not args.target:
            parser.error("--restore requires --target <path>")
        return do_restore(Path(args.restore).resolve(), Path(args.target).resolve(), force=args.force)

    cfg = load_config(require_explicit_content_root=True)
    out_path = Path(args.out).resolve() if args.out else (
        DEFAULT_BACKUP_DIR / f"personal-assistant-content-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}.tar.gz"
    )
    spec = BackupSpec(
        content_root=cfg.content_root,
        out_path=out_path,
        include_raw=args.include_raw,
        include_credentials=args.include_credentials,
    )
    return do_backup(spec)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
