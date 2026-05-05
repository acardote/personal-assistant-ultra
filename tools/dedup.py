"""Multi-fidelity event matching + ranked retrieval (issue #10).

The C-model dedup: when the same event lands via multiple harvest sources
(Granola + Meet + Gmail of the same meeting), produce ONE canonical memory
object plus ranked alternates — not three duplicates.

Public API:

    config = load_config()
    result = cluster_with_existing(new_memo, corpus, config)
    # result.role     : "canonical" (new event OR new is more authoritative
    #                    than existing canonical)
    #                  | "alternate"  (cluster exists, new is less authoritative)
    # result.event_id : string (uuid)
    # result.demoted_id : id of the existing memory that was the canonical and
    #                     should now be demoted (frontmatter rewrite needed)
    # result.cluster_members : ids of the existing members of this cluster

Algorithm:
- Tokenize each memo's body to a word multiset (stopwords removed,
  length-3 minimum, lowercase).
- For each candidate pair, compute:
    date_score = max(0, 1 - |delta_days| / window_days)
    cosine_score = cosine(tokens_a, tokens_b)
    score = 0.3 * date_score + 0.7 * cosine_score
- If max score >= cluster_threshold AND cosine_score >=
  cosine_min_for_consideration, the memos are in the same event cluster.
- Within a cluster, the canonical is the lowest-`source_authority` member
  (per dedup-config.json's `source_authority`); ties broken by earliest
  `created_at`.

This is intentionally a simple bag-of-words + date heuristic — embeddings
are deferred. The thresholds are tunable in `dedup-config.json`. Falsifiers
F1 (back-to-back meetings over-merged), F2 (stale Gmail canonical when
Granola is edited later), F3 (alternates orphaned in retrieval) are tracked
on issue #10; this module's job is to provide the foundation those
falsifiers test.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config as load_method_config  # noqa: E402

DEDUP_CONFIG_PATH = Path(__file__).resolve().parent / "dedup-config.json"

# Small stopword set; we don't need NLTK-grade for bag-of-words clustering.
STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "can",
    "her", "was", "one", "our", "out", "day", "get", "has", "him",
    "his", "how", "man", "new", "now", "old", "see", "two", "who",
    "boy", "did", "its", "let", "put", "say", "she", "too", "use",
    "this", "that", "with", "have", "from", "they", "will", "would",
    "there", "their", "what", "about", "which", "when", "where",
    "your", "been", "than", "them", "into", "such", "could", "should",
    "these", "those", "then",
}


@dataclass(frozen=True)
class DedupConfig:
    source_authority: dict[str, int]
    default_authority: int
    date_window_days: float
    cosine_min: float
    cluster_threshold: float

    def authority(self, source_kind: str) -> int:
        return self.source_authority.get(source_kind, self.default_authority)


@dataclass
class MemoSummary:
    """Lightweight view of a memory object for clustering — only the fields the
    matching algorithm needs. Loaded from disk via `load_memo_summary`."""
    id: str
    path: Path
    source_kind: str
    created_at: datetime
    body_tokens: Counter
    event_id: str | None
    is_canonical_for_event: bool
    superseded_by: str | None


@dataclass
class ClusterResult:
    role: str  # "canonical" or "alternate"
    event_id: str
    demoted_id: str | None  # id of the previous canonical that should now be demoted
    cluster_members: list[str] = field(default_factory=list)
    score: float = 0.0  # for diagnostics


# ───────────────────────────────────────────────────────────────────────
# Loading
# ───────────────────────────────────────────────────────────────────────

def load_config() -> DedupConfig:
    data = json.loads(DEDUP_CONFIG_PATH.read_text(encoding="utf-8"))
    t = data.get("thresholds", {})
    return DedupConfig(
        source_authority=data.get("source_authority", {}),
        default_authority=data.get("default_authority_for_unknown_kind", 5),
        date_window_days=float(t.get("date_window_days", 7)),
        cosine_min=float(t.get("cosine_min_for_consideration", 0.2)),
        cluster_threshold=float(t.get("cluster_threshold", 0.4)),
    )


def parse_memo_file(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: no YAML frontmatter")
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        raise ValueError(f"{path}: frontmatter not closed")
    front = yaml.safe_load(parts[0][4:]) or {}
    body = parts[1].lstrip("\n")
    return front, body


def tokenize(text: str) -> Counter:
    """Tokenize body text into a multiset of meaningful tokens.
    Lowercase, split on word boundaries, length ≥3, stopwords removed.
    """
    raw = re.findall(r"\b[\w\-]{3,}\b", text.lower())
    return Counter(w for w in raw if w not in STOPWORDS)


def load_memo_summary(path: Path) -> MemoSummary:
    front, body = parse_memo_file(path)

    created_at_raw = front.get("created_at")
    if isinstance(created_at_raw, datetime):
        ca = created_at_raw if created_at_raw.tzinfo else created_at_raw.replace(tzinfo=timezone.utc)
    elif created_at_raw:
        ca = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=timezone.utc)
    else:
        ca = datetime.now(timezone.utc)

    return MemoSummary(
        id=str(front.get("id", path.stem)),
        path=path,
        source_kind=str(front.get("source_kind", "")),
        created_at=ca,
        body_tokens=tokenize(body),
        event_id=front.get("event_id"),
        is_canonical_for_event=bool(front.get("is_canonical_for_event", False)),
        superseded_by=front.get("superseded_by"),
    )


def load_corpus(memory_root: Path) -> list[MemoSummary]:
    """Load all memory summaries from `memory_root`, excluding `.archive/`.
    Falls back to fixture-mode when the directory is absent (returns empty)."""
    if not memory_root.is_dir():
        return []
    archive = memory_root / ".archive"
    out: list[MemoSummary] = []
    for p in sorted(memory_root.rglob("*.md")):
        if archive in p.parents:
            continue
        try:
            out.append(load_memo_summary(p))
        except (OSError, ValueError):
            continue
    return out


# ───────────────────────────────────────────────────────────────────────
# Scoring
# ───────────────────────────────────────────────────────────────────────

def cosine_similarity(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    if dot == 0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    return dot / (norm_a * norm_b)


def date_score(a: datetime, b: datetime, window_days: float) -> float:
    delta = abs((a - b).total_seconds()) / 86400.0
    if delta >= window_days:
        return 0.0
    return 1.0 - delta / window_days


def pair_score(a: MemoSummary, b: MemoSummary, cfg: DedupConfig) -> tuple[float, float, float]:
    cs = cosine_similarity(a.body_tokens, b.body_tokens)
    ds = date_score(a.created_at, b.created_at, cfg.date_window_days)
    return 0.3 * ds + 0.7 * cs, cs, ds


# ───────────────────────────────────────────────────────────────────────
# Clustering
# ───────────────────────────────────────────────────────────────────────

def cluster_members(corpus: list[MemoSummary], event_id: str) -> list[MemoSummary]:
    return [m for m in corpus if m.event_id == event_id]


def pick_canonical(members: list[MemoSummary], cfg: DedupConfig) -> MemoSummary:
    """Lowest authority number wins (= highest authority); ties broken by
    earliest created_at (older = more deliberate, often the email summary)."""
    return min(members, key=lambda m: (cfg.authority(m.source_kind), m.created_at))


def cluster_with_existing(
    new: MemoSummary,
    corpus: list[MemoSummary],
    cfg: DedupConfig | None = None,
) -> ClusterResult:
    """Decide whether `new` joins an existing cluster or starts a new one.
    `corpus` should NOT include `new` itself (it hasn't been written yet,
    or has been excluded by id by the caller).
    """
    cfg = cfg or load_config()

    # Exclude any memo whose body is empty (gives nan in cosine).
    pool = [m for m in corpus if m.body_tokens]

    best_score = 0.0
    best_match: MemoSummary | None = None
    best_cosine = 0.0
    for existing in pool:
        score, cs, _ = pair_score(new, existing, cfg)
        if cs < cfg.cosine_min:
            continue
        if score > best_score:
            best_score = score
            best_match = existing
            best_cosine = cs

    if best_match is None or best_score < cfg.cluster_threshold:
        # New event: own cluster, canonical by default.
        import uuid
        return ClusterResult(
            role="canonical",
            event_id=f"evt-{uuid.uuid4()}",
            demoted_id=None,
            cluster_members=[],
            score=best_score,
        )

    # Cluster found. Find existing members (those sharing the matched memo's event_id).
    event_id = best_match.event_id
    if not event_id:
        # Legacy memo without an event_id — assign one fresh and treat best_match
        # as the seed of this cluster. The caller is responsible for backfilling
        # best_match's event_id.
        import uuid
        event_id = f"evt-{uuid.uuid4()}"
        members = [best_match]
    else:
        members = cluster_members(corpus, event_id)

    # Determine current canonical (or pick by authority if none flagged).
    flagged_canonical = next((m for m in members if m.is_canonical_for_event), None)
    current_canonical = flagged_canonical or pick_canonical(members, cfg)

    new_authority = cfg.authority(new.source_kind)
    current_authority = cfg.authority(current_canonical.source_kind)

    if new_authority < current_authority:
        # New is more authoritative → becomes the new canonical; demote old.
        return ClusterResult(
            role="canonical",
            event_id=event_id,
            demoted_id=current_canonical.id,
            cluster_members=[m.id for m in members],
            score=best_score,
        )
    else:
        # New is less or equally authoritative → joins as alternate.
        return ClusterResult(
            role="alternate",
            event_id=event_id,
            demoted_id=None,
            cluster_members=[m.id for m in members],
            score=best_score,
        )
