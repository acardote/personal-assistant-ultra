#!/usr/bin/env -S uv run --quiet --with jsonschema --with pyyaml --with tiktoken --with slack-sdk --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4", "pyyaml>=6", "tiktoken>=0.7"]
# ///
"""Harvest items from a Source into layer 1 (raw archive) and layer 2 (memory objects).

Usage:
    tools/harvest.py --source slack-fixture --fixture-dir tests/fixtures/slack
    tools/harvest.py --source granola --folder ~/.granola/exports --since 2026-04-01
    tools/harvest.py --source gmeet --folder ~/Drive/Meet-transcripts
    tools/harvest.py --source transcripts --folder ~/transcript-drop
    tools/harvest.py --source <name> --dry-run    # list what would be harvested, don't write

Slack and Gmail were retired from the CLI in #5/#6 reopen — their MCP-based
harvest now runs at the SKILL layer (see SKILL.md). The CLI keeps file-based
sources (granola folder, gmeet folder, generic transcript drop) and the
slack-fixture path used for tests.

The Source interface (informal protocol — `Source` ABC):
    - `name: str` — short identifier ("slack", "gmail", ...)
    - `source_kind: str` — frontmatter value for memory objects ("slack_thread", ...)
    - `list_new(since, state) -> Iterable[ItemRef]` — items not yet harvested
    - `fetch(ref) -> RawArtifact` — full content for an item ref
    - `dedupe_key(ref) -> str` — stable key that survives edits/reposts

Idempotency invariant: re-running with the same `--since` against an unchanged source
produces zero NEW memory objects. The harvester maintains per-source state under
`.harvest/<source-name>.json` mapping dedupe_keys to landed memory paths.

This child (#5) ships the abstraction + the Slack source. Additional sources
(Granola, Gmail, Meet, generic transcripts) are tracked in #6.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402

_CFG = load_config()
METHOD_ROOT = _CFG.method_root
RAW_ROOT = _CFG.raw_root
MEMORY_ROOT = _CFG.memory_root
STATE_DIR = _CFG.harvest_state_root
PROJECT_ROOT = METHOD_ROOT  # legacy alias used by `derive_raw_path` etc. for relative-display


# ───────────────────────────────────────────────────────────────────────
# Core abstraction
# ───────────────────────────────────────────────────────────────────────

@dataclass
class ItemRef:
    """A handle pointing at a fetchable item. Source-specific ids live in `meta`."""
    id: str                       # source-internal id (channel_id:thread_ts for Slack, etc.)
    title: str                    # short human-readable title for filenames/logs
    created_at: datetime          # when the item was created at the source
    suggested_kind: str = "thread"  # memory-object `kind` field
    meta: dict = field(default_factory=dict)


@dataclass
class RawArtifact:
    """The fetched content of an item. Written to layer 1 verbatim."""
    content: str                  # serialized form (Markdown, JSON, plain text)
    extension: str                # file extension including leading dot (e.g. ".md", ".json")
    suggested_kind: str = "thread"


@dataclass
class HarvestState:
    """Per-source dedup state. JSON-serializable."""
    source_name: str
    seen: dict[str, str] = field(default_factory=dict)  # dedupe_key -> memory_path

    @classmethod
    def load(cls, source_name: str) -> "HarvestState":
        path = STATE_DIR / f"{source_name}.json"
        if not path.exists():
            return cls(source_name=source_name)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(source_name=source_name, seen=data.get("seen", {}))

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = STATE_DIR / f"{self.source_name}.json"
        path.write_text(
            json.dumps({"source_name": self.source_name, "seen": self.seen}, indent=2, sort_keys=True),
            encoding="utf-8",
        )


class Source(ABC):
    """Abstract harvester source. Subclass and implement the four methods."""

    name: str = ""
    source_kind: str = ""

    @abstractmethod
    def list_new(self, since: datetime, state: HarvestState) -> Iterable[ItemRef]:
        """Yield refs for items created/updated since `since` not already in state.seen."""

    @abstractmethod
    def fetch(self, ref: ItemRef) -> RawArtifact:
        """Fetch the full content for a ref."""

    @abstractmethod
    def dedupe_key(self, ref: ItemRef) -> str:
        """Stable key for idempotency. Same ref → same key, even on re-fetch."""


# ───────────────────────────────────────────────────────────────────────
# Source utilities (shared by file-based + fixture sources)
# ───────────────────────────────────────────────────────────────────────

def _slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:max_len] or "untitled"


# ───────────────────────────────────────────────────────────────────────
# Slack-via-Web-API source: REMOVED (per #5 reopen)
# ───────────────────────────────────────────────────────────────────────
#
# The user has Slack MCP configured (mcp__claude_ai_Slack__*) and prefers
# MCP-orchestrated harvest over a Python Web-API client. Slack ingestion now
# happens at the SKILL layer: when the personal-assistant skill runs harvest,
# Claude calls the Slack MCP tools directly to discover + fetch threads, then
# invokes tools/compress.py via Bash to produce memory objects. The CLI
# `harvest --source slack` path is retired; `slack-fixture` remains for tests.
#
# See SKILL.md for the orchestration contract; #11 will land the scheduled
# routine that drives the MCP-orchestrated harvest unattended.


def _legacy_slack_dedupe_key(channel: str, thread_ts: str) -> str:
    """Kept for SlackFixtureSource compatibility (it used SlackSource's
    classmethod; inlining preserves dedupe-key shape across reads of old
    state files)."""
    return f"slack:{channel}:{thread_ts}"


class _RemovedSlackSourceMarker(Source):
    """Tombstone for the removed SlackSource. Defined only so the existing
    reference in build_source() can be re-routed to a clean error message
    (and so the type system doesn't shift). Never instantiable."""
    name = "slack"
    source_kind = "slack_thread"

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "harvest --source slack via Web API has been retired (#5 reopen). "
            "Slack harvest now runs via MCP through the personal-assistant skill: "
            "in a Claude Code session, invoke the skill and ask it to harvest. "
            "For offline tests use --source slack-fixture; the architecture "
            "details are documented in SKILL.md and parent #1's sequence."
        )

    def list_new(self, since, state):  # pragma: no cover
        raise RuntimeError("removed")

    def fetch(self, ref):  # pragma: no cover
        raise RuntimeError("removed")

    def dedupe_key(self, ref):  # pragma: no cover
        return ""
        # Render thread as a structured Markdown document — preserves speaker
        # attribution per F3, and is human-readable in the layer-1 archive.
        lines = [f"# Slack thread {ref.id}", ""]
        for msg in resp.get("messages", []):
            user = msg.get("user", "?")
            ts = msg.get("ts", "?")
            iso = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat() if ts != "?" else "?"
            text = msg.get("text", "")
            lines.append(f"## {iso} — user:{user} (ts:{ts})")
            lines.append("")
            lines.append(text)
            lines.append("")
        return RawArtifact(content="\n".join(lines), extension=".md", suggested_kind="thread")

    def dedupe_key(self, ref: ItemRef) -> str:
        return self._dedupe_key_for(ref.meta["channel"], ref.meta["thread_ts"])

    @staticmethod
    def _dedupe_key_for(channel: str, thread_ts: str) -> str:
        return f"slack:{channel}:{thread_ts}"


# ───────────────────────────────────────────────────────────────────────
# Slack fixture source — for offline testing & idempotency demo
# ───────────────────────────────────────────────────────────────────────

class SlackFixtureSource(Source):
    """Reads pre-saved Slack thread JSON files from a fixture directory.

    Used to exercise the harvester end-to-end without live Slack auth. Each file
    must contain `{"channel": "C...", "thread_ts": "...", "messages": [...]}` mimicking
    the shape of `conversations.replies`. Output is identical to `SlackSource`'s, so
    the dedupe_key + memory-object pipeline is exercised the same way.
    """

    name = "slack-fixture"
    source_kind = "slack_thread"

    def __init__(self, fixture_dir: Path):
        if not fixture_dir.is_dir():
            raise ValueError(f"fixture dir not found: {fixture_dir}")
        self.fixture_dir = fixture_dir

    def list_new(self, since: datetime, state: HarvestState) -> Iterator[ItemRef]:
        # Yield every fixture; the harvest loop filters by state.seen and keeps
        # the metric honest. Cleaner than filtering twice.
        for path in sorted(self.fixture_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            channel = data["channel"]
            thread_ts = data["thread_ts"]
            first_text = (data["messages"][0].get("text", "") if data.get("messages") else "")
            yield ItemRef(
                id=f"{channel}:{thread_ts}",
                title=_slugify(first_text.splitlines()[0] if first_text else path.stem),
                created_at=datetime.fromtimestamp(float(thread_ts), tz=timezone.utc),
                suggested_kind="thread",
                meta={"channel": channel, "thread_ts": thread_ts, "_fixture_path": str(path)},
            )

    def fetch(self, ref: ItemRef) -> RawArtifact:
        path = Path(ref.meta["_fixture_path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        lines = [f"# Slack thread {ref.id}", ""]
        for msg in data.get("messages", []):
            user = msg.get("user", "?")
            ts = msg.get("ts", "?")
            iso = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat() if ts != "?" else "?"
            text = msg.get("text", "")
            lines.append(f"## {iso} — user:{user} (ts:{ts})")
            lines.append("")
            lines.append(text)
            lines.append("")
        return RawArtifact(content="\n".join(lines), extension=".md", suggested_kind="thread")

    def dedupe_key(self, ref: ItemRef) -> str:
        return _legacy_slack_dedupe_key(ref.meta["channel"], ref.meta["thread_ts"])


# ───────────────────────────────────────────────────────────────────────
# File-folder source mixin (shared by Granola, Meet transcripts, generic drops)
# ───────────────────────────────────────────────────────────────────────

class _FolderSource(Source):
    """Base for sources that watch a folder of files. Subclasses set extension globs,
    `source_kind`, `kind`, and override `_render_raw` if format-specific rendering is needed.

    Idempotency: dedupe_key combines absolute path + content sha256, so a renamed file is
    treated as new (correct: the user might rename to organize) and a re-saved file with
    new content also re-harvests. F1 (cross-source dedup) is intentionally not handled
    at this layer — that's a higher-level concern tracked in #6's challenger F1.
    """

    extensions: tuple[str, ...] = ()
    default_kind: str = "note"
    raw_extension: str = ".md"

    def __init__(self, folder: Path):
        if not folder.is_dir():
            raise ValueError(f"folder not found: {folder}")
        self.folder = folder

    def list_new(self, since: datetime, state: HarvestState) -> Iterator[ItemRef]:
        for ext in self.extensions:
            for path in sorted(self.folder.rglob(f"*{ext}")):
                stat = path.stat()
                created_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                if created_at < since:
                    continue
                yield ItemRef(
                    id=str(path.resolve()),
                    title=_slugify(path.stem),
                    created_at=created_at,
                    suggested_kind=self.default_kind,
                    meta={"path": str(path.resolve())},
                )

    def fetch(self, ref: ItemRef) -> RawArtifact:
        path = Path(ref.meta["path"])
        content = path.read_text(encoding="utf-8", errors="replace")
        rendered = self._render_raw(path, content, ref)
        return RawArtifact(content=rendered, extension=self.raw_extension, suggested_kind=self.default_kind)

    def dedupe_key(self, ref: ItemRef) -> str:
        # Content-only sha, NOT path-based — making the key cross-machine portable
        # per #12 phase 2 (challenger C3 on PR #16): including absolute paths in
        # dedup keys leaked maintainer-machine paths into committed state and broke
        # cross-machine consistency. The trade-off: identical-content files in two
        # different folders dedup together, which is what we want — the same Granola
        # export saved twice is the same memory.
        import hashlib
        path = Path(ref.meta["path"])
        sha = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        return f"{self.name}:{sha}"

    def _render_raw(self, path: Path, content: str, ref: ItemRef) -> str:
        """Default: prepend a provenance header. Override for format-aware rendering."""
        return (
            f"# {self.name} :: {path.name}\n"
            f"_source path: `{path}`_\n"
            f"_modified: {ref.created_at.isoformat()}_\n\n"
            + content
        )


class GranolaFolderSource(_FolderSource):
    """Watches a folder of Granola exports (Markdown / RTF / plain text)."""
    name = "granola"
    source_kind = "granola_note"
    extensions = (".md", ".txt", ".rtf")
    default_kind = "note"


class GMeetTranscriptFolderSource(_FolderSource):
    """Watches a folder of Google Meet transcripts (.vtt / .txt). Typical input:
    a Drive folder synced locally where Meet auto-saves transcripts."""
    name = "gmeet"
    source_kind = "gmeet_transcript"
    extensions = (".vtt", ".txt")
    default_kind = "thread"

    def _render_raw(self, path: Path, content: str, ref: ItemRef) -> str:
        # .vtt has WEBVTT cues with timestamp + speaker; preserve verbatim per F3.
        return (
            f"# Google Meet transcript :: {path.name}\n"
            f"_source path: `{path}`_\n"
            f"_modified: {ref.created_at.isoformat()}_\n\n"
            "```\n"
            + content
            + "\n```"
        )


class GenericTranscriptDropSource(_FolderSource):
    """Watches a manually-managed folder for `.vtt`/`.srt`/`.txt` files dropped by the user."""
    name = "transcripts"
    source_kind = "transcript_file"
    extensions = (".vtt", ".srt", ".txt")
    default_kind = "thread"

    def _render_raw(self, path: Path, content: str, ref: ItemRef) -> str:
        # Same verbatim preservation as Meet — these formats are speaker+timestamped.
        return (
            f"# Transcript drop :: {path.name}\n"
            f"_source path: `{path}`_\n"
            f"_modified: {ref.created_at.isoformat()}_\n\n"
            "```\n"
            + content
            + "\n```"
        )


# ───────────────────────────────────────────────────────────────────────
# Gmail-via-OAuth source: REMOVED (per #6 reopen)
# ───────────────────────────────────────────────────────────────────────
#
# The user has Gmail MCP available and prefers MCP-orchestrated harvest over
# a Python-side OAuth client. Gmail ingestion happens at the SKILL layer:
# Claude calls Gmail MCP tools to query threads + extract bodies, then
# invokes tools/compress.py via Bash. The CLI `harvest --source gmail` path
# is retired. (Granola similarly moves to the MCP path; only file-based
# fallbacks — folder watchers — remain in this CLI.)
#
# Implementation history is preserved in git: git log -- tools/harvest.py.


class _RemovedGmailSourceMarker(Source):
    """Tombstone for the removed GmailSource. See _RemovedSlackSourceMarker."""
    name = "gmail"
    source_kind = "gmail_thread"

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "harvest --source gmail via OAuth has been retired (#6 reopen). "
            "Gmail harvest now runs via MCP through the personal-assistant skill: "
            "in a Claude Code session, invoke the skill and ask it to harvest. "
            "See SKILL.md."
        )

    def list_new(self, since, state):  # pragma: no cover
        raise RuntimeError("removed")

    def fetch(self, ref):  # pragma: no cover
        raise RuntimeError("removed")

    def dedupe_key(self, ref):  # pragma: no cover
        return ""


# ───────────────────────────────────────────────────────────────────────
# Harvest loop
# ───────────────────────────────────────────────────────────────────────

def _disp(p: Path) -> str:
    """Display a path relative to method root when possible; otherwise absolute.
    Avoids ValueError when the path is outside the method root (e.g. under
    content_root in vault mode)."""
    try:
        return str(p.relative_to(METHOD_ROOT))
    except ValueError:
        return str(p)


def derive_raw_path(source: Source, ref: ItemRef, ext: str) -> Path:
    """Stable path under raw/<source-kind>/<title>-<id-hash>.<ext>."""
    safe_id = ref.id.replace(":", "_").replace("/", "_")
    return RAW_ROOT / source.source_kind / f"{ref.title}-{safe_id}{ext}"


def derive_memory_path(source: Source, ref: ItemRef) -> Path:
    safe_id = ref.id.replace(":", "_").replace("/", "_")
    return MEMORY_ROOT / source.source_kind / f"{ref.title}-{safe_id}.md"


def run_compress(raw_path: Path, kind: str, source_kind: str, out: Path) -> None:
    """Invoke tools/compress.py — already validated end-to-end in #3."""
    subprocess.run(
        [
            str(PROJECT_ROOT / "tools" / "compress.py"),
            str(raw_path),
            "--kind", kind,
            "--source-kind", source_kind,
            "--out", str(out),
        ],
        check=True,
    )


def harvest(source: Source, since: datetime, dry_run: bool = False) -> dict:
    """Run the harvest loop. Returns a summary dict for evidence/output."""
    state = HarvestState.load(source.name)
    summary = {
        "source": source.name,
        "since": since.isoformat(),
        "skipped_already_seen": 0,
        "new_raw": [],
        "new_memory": [],
    }
    for ref in source.list_new(since, state):
        key = source.dedupe_key(ref)
        if key in state.seen:
            summary["skipped_already_seen"] += 1
            continue
        raw = source.fetch(ref)
        raw_path = derive_raw_path(source, ref, raw.extension)
        memory_path = derive_memory_path(source, ref)
        if dry_run:
            print(f"[dry-run] would write {_disp(raw_path)} and {_disp(memory_path)}", file=sys.stderr)
            continue
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(raw.content, encoding="utf-8")
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        run_compress(raw_path, kind=raw.suggested_kind, source_kind=source.source_kind, out=memory_path)
        state.seen[key] = _disp(memory_path)
        summary["new_raw"].append(_disp(raw_path))
        summary["new_memory"].append(_disp(memory_path))
    if not dry_run:
        state.save()
    return summary


# ───────────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────────

def build_source(args: argparse.Namespace) -> Source:
    if args.source == "slack":
        # Retired — instantiating raises with guidance to the skill path.
        return _RemovedSlackSourceMarker()
    if args.source == "slack-fixture":
        fixture_dir = Path(args.fixture_dir or (PROJECT_ROOT / "tests" / "fixtures" / "slack"))
        return SlackFixtureSource(fixture_dir=fixture_dir.resolve())
    if args.source == "granola":
        if not args.folder:
            raise SystemExit("--folder <path> is required for granola source")
        return GranolaFolderSource(folder=Path(args.folder).resolve())
    if args.source == "gmeet":
        if not args.folder:
            raise SystemExit("--folder <path> is required for gmeet source")
        return GMeetTranscriptFolderSource(folder=Path(args.folder).resolve())
    if args.source == "transcripts":
        if not args.folder:
            raise SystemExit("--folder <path> is required for transcripts source")
        return GenericTranscriptDropSource(folder=Path(args.folder).resolve())
    if args.source == "gmail":
        # Retired — instantiating raises with guidance to the skill path.
        return _RemovedGmailSourceMarker()
    raise SystemExit(f"unknown source: {args.source}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Harvest items from a source into layers 1+2.")
    parser.add_argument("--source", required=True, help="Source name: slack-fixture | granola | gmeet | transcripts (slack/gmail are MCP-orchestrated via the skill, not this CLI — see SKILL.md)")
    parser.add_argument("--since", help="ISO date or datetime (default: 30 days ago).")
    parser.add_argument("--fixture-dir", help="Fixture directory (slack-fixture only).")
    parser.add_argument("--folder", help="Folder path (granola / gmeet / transcripts sources).")
    # --channels and --query were tied to retired sources (slack/gmail) — removed.
    parser.add_argument("--dry-run", action="store_true", help="List what would be harvested.")
    args = parser.parse_args(argv[1:])

    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    else:
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(days=30)

    source = build_source(args)
    summary = harvest(source, since=since, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
