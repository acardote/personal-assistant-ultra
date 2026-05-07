#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Write-back pipeline: live raw artifacts → memory layer (#39-D).

The live-call adapters (#39-B.1/.2/.3) write artifacts to
`<content_root>/raw/live/<source>/<ts>-<hash>.md`. They explicitly do NOT
compress at fetch time — that latency would blow the <30s p95 query budget
on parent #39. This tool does the compression out-of-band, batch-style.

For each unprocessed file in `raw/live/<source>/`:
  1. Run `tools/compress.py <file> --source-kind <source> --provenance live`.
  2. compress.py writes the memory object to `memory/<source>/<file>.md`
     (the `live/` segment is stripped per derive_memory_path's provenance
     branch — same path as harvest-fetched memory, so #10 event-id dedup
     can catch dupes across pipelines).
  3. Move the raw file to `raw/live/<source>/.processed/<file>.md` so
     subsequent runs skip it.

State strategy: `.processed/` subdir per source. No external state file.
Reproducible (delete the dir to reprocess everything) and inspectable.

## Quality bar

The parent #39 scope says "live results that meet a quality bar are
compressed." This tool's bar today is **non-empty body** (live-result-write
already refused to write zero-byte artifacts; we trust that gate). Future
filters (e.g. dedup-on-content-hash before compress) belong here.

## Failure handling

If compress.py fails on a single file (LLM timeout, dedup conflict, etc.),
log the failure, leave the file in place (NOT moved to .processed/), and
proceed to the next. The next run will retry. This is the same retry-once
posture as the harvest pipeline.

## Usage

    tools/live-writeback.py                  # process all sources
    tools/live-writeback.py --source granola_note  # one source only
    tools/live-writeback.py --dry-run        # list what would be processed

Exits 0 on success, 1 on argument errors, 2 if any file failed to compress
(but other files may still have been processed successfully — check stderr
for per-file status).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402
from _metrics import emit, inherit_or_start, time_event  # noqa: E402

VALID_SOURCES = {"granola_note", "slack_thread", "slack_dm", "gmail_thread"}
PROCESSED_DIR_NAME = ".processed"


def find_unprocessed(content_root: Path, sources: list[str]) -> list[tuple[str, Path]]:
    """Return (source, raw_path) for every live artifact not yet processed."""
    out: list[tuple[str, Path]] = []
    for src in sources:
        src_dir = content_root / "raw" / "live" / src
        if not src_dir.is_dir():
            continue
        for p in sorted(src_dir.iterdir()):
            if p.is_dir():
                continue  # skip .processed/ and any other dirs
            if p.suffix != ".md":
                continue
            out.append((src, p))
    return out


def compress_one(raw_path: Path, source: str, *, method_root: Path) -> bool:
    """Run compress.py with --provenance live. Returns True on success."""
    compress_tool = method_root / "tools" / "compress.py"
    cmd = [
        str(compress_tool),
        str(raw_path),
        "--source-kind", source,
        "--provenance", "live",
    ]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except OSError as e:
        print(f"[live-writeback] failed to invoke compress on {raw_path.name}: {e}", file=sys.stderr)
        return False
    if result.returncode != 0:
        # Surface compress's stderr for debugging; keep the raw file in place.
        sys.stderr.write(result.stderr)
        return False
    return True


def mark_processed(raw_path: Path) -> None:
    """Move the raw file into the per-source .processed/ subdir."""
    processed_dir = raw_path.parent / PROCESSED_DIR_NAME
    processed_dir.mkdir(exist_ok=True)
    raw_path.rename(processed_dir / raw_path.name)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write-back live raw artifacts → memory (#39-D).")
    parser.add_argument(
        "--source",
        choices=sorted(VALID_SOURCES),
        default=None,
        help="Process one source only (default: all three).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be processed without invoking compress.",
    )
    args = parser.parse_args(argv[1:])

    cfg = load_config()
    inherit_or_start()

    sources = [args.source] if args.source else sorted(VALID_SOURCES)
    targets = find_unprocessed(cfg.content_root, sources)

    if args.dry_run:
        print(f"[live-writeback] dry-run: {len(targets)} unprocessed file(s)")
        for src, p in targets:
            print(f"  {src}: {p.relative_to(cfg.content_root)}")
        return 0

    if not targets:
        print("[live-writeback] no unprocessed files; nothing to do.", file=sys.stderr)
        return 0

    success = 0
    failed = 0
    with time_event("live_writeback", target_count=len(targets)) as wb_tracker:
        for src, raw_path in targets:
            print(f"[live-writeback] {src}: {raw_path.name}", file=sys.stderr)
            ok = compress_one(raw_path, src, method_root=cfg.method_root)
            if ok:
                mark_processed(raw_path)
                success += 1
                emit("live_writeback_item", source=src, status="success",
                     filename=raw_path.name)
            else:
                failed += 1
                emit("live_writeback_item", source=src, status="error",
                     filename=raw_path.name)
        wb_tracker["success_count"] = success
        wb_tracker["failed_count"] = failed

    print(f"[live-writeback] done: {success} processed, {failed} failed", file=sys.stderr)
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
