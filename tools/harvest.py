#!/usr/bin/env -S uv run --quiet --with jsonschema --with pyyaml --with tiktoken --with slack-sdk --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "jsonschema>=4",
#   "pyyaml>=6",
#   "tiktoken>=0.7",
#   "slack-sdk>=3.27",
#   "google-api-python-client>=2.140",
#   "google-auth-oauthlib>=1.2",
#   "google-auth-httplib2>=0.2",
# ]
# ///
"""Harvest items from a Source into layer 1 (raw archive) and layer 2 (memory objects).

Usage:
    tools/harvest.py --source slack --since 2026-04-01
    tools/harvest.py --source slack --since 2026-04-01 --channels C0123,C0456
    tools/harvest.py --source slack-fixture --fixture-dir tests/fixtures/slack
    tools/harvest.py --source <name> --dry-run    # list what would be harvested, don't write

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
import os
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
# Slack source — Web API
# ───────────────────────────────────────────────────────────────────────

def _slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:max_len] or "untitled"


class SlackSource(Source):
    """Harvests Slack threads via Web API (`conversations.history` + `conversations.replies`).

    Auth: `SLACK_USER_TOKEN` env var. Channels: comma-separated list of channel IDs via
    `--channels` (this source treats no `--channels` as an explicit error rather than
    pulling everything — F2 calls out that scope creep here is the noise-inversion failure).
    """

    name = "slack"
    source_kind = "slack_thread"

    def __init__(self, channels: list[str]):
        if not channels:
            raise ValueError(
                "slack source requires --channels <C0123,C0456,...>; harvesting all "
                "channels by default would invite F2 (signal/noise inversion)."
            )
        self.channels = channels
        token = os.environ.get("SLACK_USER_TOKEN")
        if not token:
            raise RuntimeError(
                "SLACK_USER_TOKEN is not set. Set it (a user OAuth token with conversations:history scope) "
                "or use --source slack-fixture for offline testing."
            )
        from slack_sdk import WebClient  # imported lazily so fixture path doesn't need it
        self.client = WebClient(token=token)

    def list_new(self, since: datetime, state: HarvestState) -> Iterator[ItemRef]:
        oldest = str(since.timestamp())
        for channel in self.channels:
            cursor = None
            while True:
                resp = self.client.conversations_history(
                    channel=channel,
                    oldest=oldest,
                    cursor=cursor,
                    limit=200,
                )
                for msg in resp.get("messages", []):
                    if msg.get("subtype") in {"channel_join", "channel_leave", "bot_message"}:
                        # F2 mitigation: skip low-signal subtypes by default.
                        continue
                    thread_ts = msg.get("thread_ts") or msg.get("ts")
                    item_id = f"{channel}:{thread_ts}"
                    key = self._dedupe_key_for(channel, thread_ts)
                    if key in state.seen:
                        continue
                    text = (msg.get("text") or "").splitlines()[0][:80] or "untitled-message"
                    yield ItemRef(
                        id=item_id,
                        title=_slugify(text),
                        created_at=datetime.fromtimestamp(float(thread_ts), tz=timezone.utc),
                        suggested_kind="thread",
                        meta={"channel": channel, "thread_ts": thread_ts},
                    )
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

    def fetch(self, ref: ItemRef) -> RawArtifact:
        resp = self.client.conversations_replies(
            channel=ref.meta["channel"],
            ts=ref.meta["thread_ts"],
            limit=1000,
        )
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
        return SlackSource._dedupe_key_for(ref.meta["channel"], ref.meta["thread_ts"])


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
# Gmail source (API-based; requires OAuth setup by the user)
# ───────────────────────────────────────────────────────────────────────

class GmailSource(Source):
    """Harvests Gmail threads via the Gmail API. Requires the user to set up OAuth
    credentials (`credentials.json`) and complete an OAuth flow once to persist
    `token.json`. After that, the source is non-interactive.

    Auth state lives at `.harvest/gmail-credentials.json` and `.harvest/gmail-token.json`
    (both git-ignored). The user provides `--query` (Gmail search syntax) to scope
    harvest, mirroring the Slack `--channels` discipline as an F2 mitigation against
    signal/noise inversion ('inbox' is too broad to be a default).
    """

    name = "gmail"
    source_kind = "gmail_thread"

    def __init__(self, query: str):
        if not query:
            raise ValueError(
                "gmail source requires --query <gmail-search-syntax>; harvesting "
                "all of inbox by default would invite F2 (signal/noise inversion)."
            )
        self.query = query
        self.credentials_path = STATE_DIR / "gmail-credentials.json"
        self.token_path = STATE_DIR / "gmail-token.json"
        if not self.credentials_path.exists():
            raise RuntimeError(
                f"Gmail credentials not found at {self.credentials_path}.\n"
                "To enable Gmail harvesting:\n"
                "  1. Create a Google Cloud project + enable the Gmail API.\n"
                "  2. Create OAuth client credentials (Desktop App).\n"
                f"  3. Save the downloaded JSON as {self.credentials_path}.\n"
                "  4. Re-run this command — a browser window will open for the OAuth flow.\n"
                f"     The resulting token will be cached at {self.token_path}.\n"
                "Required scope: `https://www.googleapis.com/auth/gmail.readonly`."
            )
        self._service = self._build_service()

    def _build_service(self):
        # Lazy import — only reached when the user has set up creds.
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), scopes)
                creds = flow.run_local_server(port=0)
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return build("gmail", "v1", credentials=creds)

    def list_new(self, since: datetime, state: HarvestState) -> Iterator[ItemRef]:
        epoch = int(since.timestamp())
        full_query = f"{self.query} after:{epoch}"
        next_page = None
        while True:
            resp = self._service.users().threads().list(userId="me", q=full_query, pageToken=next_page).execute()
            for t in resp.get("threads", []):
                yield ItemRef(
                    id=t["id"],
                    title=_slugify(t.get("snippet", t["id"])[:80]),
                    created_at=since,  # refined when fetched
                    suggested_kind="thread",
                    meta={"thread_id": t["id"]},
                )
            next_page = resp.get("nextPageToken")
            if not next_page:
                break

    def fetch(self, ref: ItemRef) -> RawArtifact:
        thread = self._service.users().threads().get(userId="me", id=ref.meta["thread_id"]).execute()
        lines = [f"# Gmail thread {ref.meta['thread_id']}", ""]
        for msg in thread.get("messages", []):
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from_ = headers.get("From", "?")
            date = headers.get("Date", "?")
            subject = headers.get("Subject", "(no subject)")
            body = self._extract_text(msg.get("payload", {}))
            lines.append(f"## {date} — From: {from_}")
            lines.append(f"Subject: {subject}")
            lines.append("")
            lines.append(body)
            lines.append("")
        return RawArtifact(content="\n".join(lines), extension=".md", suggested_kind="thread")

    @staticmethod
    def _extract_text(payload: dict) -> str:
        import base64
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for part in payload.get("parts", []) or []:
            text = GmailSource._extract_text(part)
            if text:
                return text
        return ""

    def dedupe_key(self, ref: ItemRef) -> str:
        return f"gmail:{ref.meta['thread_id']}"


# ───────────────────────────────────────────────────────────────────────
# Harvest loop
# ───────────────────────────────────────────────────────────────────────

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
            print(f"[dry-run] would write {raw_path.relative_to(PROJECT_ROOT)} and {memory_path.relative_to(PROJECT_ROOT)}", file=sys.stderr)
            continue
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(raw.content, encoding="utf-8")
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        run_compress(raw_path, kind=raw.suggested_kind, source_kind=source.source_kind, out=memory_path)
        state.seen[key] = str(memory_path.relative_to(PROJECT_ROOT))
        summary["new_raw"].append(str(raw_path.relative_to(PROJECT_ROOT)))
        summary["new_memory"].append(str(memory_path.relative_to(PROJECT_ROOT)))
    if not dry_run:
        state.save()
    return summary


# ───────────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────────

def build_source(args: argparse.Namespace) -> Source:
    if args.source == "slack":
        channels = [c.strip() for c in (args.channels or "").split(",") if c.strip()]
        return SlackSource(channels=channels)
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
        if not args.query:
            raise SystemExit("--query <gmail-search-syntax> is required for gmail source")
        return GmailSource(query=args.query)
    raise SystemExit(f"unknown source: {args.source}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Harvest items from a source into layers 1+2.")
    parser.add_argument("--source", required=True, help="Source name: slack | slack-fixture")
    parser.add_argument("--since", help="ISO date or datetime (default: 30 days ago).")
    parser.add_argument("--channels", help="Comma-separated channel IDs (slack only).")
    parser.add_argument("--fixture-dir", help="Fixture directory (slack-fixture only).")
    parser.add_argument("--folder", help="Folder path (granola / gmeet / transcripts sources).")
    parser.add_argument("--query", help="Gmail search query (gmail source only).")
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
