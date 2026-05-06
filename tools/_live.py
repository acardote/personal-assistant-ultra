"""Live-call gap detection (#39-A).

Decides whether the router should fall back to live MCP calls because
the memory layer is unlikely to answer the query well.

Two triggers:
  - **zero_hit**: `load_memory_objects` returned no matches. Memory has
    nothing keyword-grounded to offer; live data is the only path.
  - **topic_pinned**: the query mentions a topic that the user has
    flagged as fast-evolving (e.g. weekly syncs that change between
    harvest runs). Live is preferred even when memory has hits, because
    those hits are likely stale.

Topic-pinned config lives at `<content_root>/.harvest/live-pinned.txt`,
one topic per line, blank lines and `#` comment lines ignored. Matching
is **case-insensitive, word-boundary** against the raw query — substring
matching produced too many false positives on quoted text and code
fences (per pr-challenger F4 on #48).

This module is intentionally pure: no I/O on import, no global state,
no MCP calls. It reports "should we go live" — actually going live is
#39-B's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Cap the matched_topic that flows through metrics events. Pin entries
# are user-authored, but consumers (dashboard, future export) shouldn't
# carry unbounded user strings. 64 chars fits real topic phrases like
# "Acko Projects Weekly Sync" with headroom; longer pins are still
# matched — only the recorded value is bounded.
MATCHED_TOPIC_MAX_CHARS = 64


@dataclass(frozen=True)
class LiveDecision:
    should_go_live: bool
    reason: str | None  # "zero_hit" | "topic_pinned" | None
    matched_topic: str | None  # set whenever a pin matched, even if reason=zero_hit


_NO_LIVE = LiveDecision(should_go_live=False, reason=None, matched_topic=None)


def load_pinned_topics(content_root: Path) -> list[str]:
    """Read pinned topics from `<content_root>/.harvest/live-pinned.txt`.

    Returns [] if the file is missing — that's the expected default for
    a fresh vault and must NOT trigger live calls.
    """
    path = content_root / ".harvest" / "live-pinned.txt"
    if not path.is_file():
        return []
    topics: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        topics.append(line)
    return topics


def _bound_topic(topic: str) -> str:
    """Bound a matched topic for metrics emission. The unbounded value
    stays available on LiveDecision for in-memory routing, but anything
    that crosses to disk (events file) goes through here."""
    return topic[:MATCHED_TOPIC_MAX_CHARS]


def _find_matching_topic(query: str, topics: list[str]) -> str | None:
    """Return the first topic that word-boundary-matches the query, or
    None. Case-insensitive."""
    if not topics:
        return None
    q = query.lower()
    for topic in topics:
        pattern = rf"\b{re.escape(topic.lower())}\b"
        if re.search(pattern, q):
            return topic
    return None


def should_go_live(
    query: str,
    memory_hits: int,
    *,
    content_root: Path,
    _pinned_topics_override: list[str] | None = None,
) -> LiveDecision:
    """Decide whether to augment with live calls.

    `_pinned_topics_override` is a test-only seam (leading underscore +
    explicit name). Production callers pass nothing and let the module
    read the config file.

    Order of checks: zero-hit first (the broader signal — when memory
    has nothing, live is the only path). When zero_hit fires we *also*
    scan pinned topics so #39-B can preferentially route to the matched
    source even though zero_hit is the primary reason. Topic-pinned-only
    fires when memory had hits but a pin overrode them as stale.
    """
    topics = (
        _pinned_topics_override
        if _pinned_topics_override is not None
        else load_pinned_topics(content_root)
    )
    matched = _find_matching_topic(query, topics)

    if memory_hits == 0:
        return LiveDecision(
            should_go_live=True,
            reason="zero_hit",
            matched_topic=_bound_topic(matched) if matched else None,
        )

    if matched is not None:
        return LiveDecision(
            should_go_live=True,
            reason="topic_pinned",
            matched_topic=_bound_topic(matched),
        )

    return _NO_LIVE
