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
is case-insensitive substring; the topic string is matched against the
raw query text.

This module is intentionally pure: no I/O on import, no global state,
no MCP calls. It reports "should we go live" — actually going live is
#39-B's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LiveDecision:
    should_go_live: bool
    reason: str | None  # "zero_hit" | "topic_pinned" | None
    matched_topic: str | None  # populated only when reason == "topic_pinned"


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


def should_go_live(
    query: str,
    memory_hits: int,
    *,
    content_root: Path,
    pinned_topics: list[str] | None = None,
) -> LiveDecision:
    """Decide whether to augment with live calls.

    `pinned_topics` is injectable for tests; production callers pass
    `None` and let the module read the config file.

    Order of checks: zero-hit first (cheapest, decisive when true),
    topic-pinned second (requires substring scan over the query). If
    both fire, zero-hit wins because it's the broader signal — the
    matched_topic field is reserved for the case where memory had hits
    but a pin overrode them.
    """
    if memory_hits == 0:
        return LiveDecision(should_go_live=True, reason="zero_hit", matched_topic=None)

    topics = pinned_topics if pinned_topics is not None else load_pinned_topics(content_root)
    if not topics:
        return _NO_LIVE

    haystack = query.lower()
    for topic in topics:
        if topic.lower() in haystack:
            return LiveDecision(
                should_go_live=True,
                reason="topic_pinned",
                matched_topic=topic,
            )

    return _NO_LIVE
