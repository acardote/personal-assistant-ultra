#!/usr/bin/env -S uv run --quiet --with pyyaml --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""Prune expired memory objects from the retrievable layer-2 corpus into a cold archive.

Usage:
    tools/prune.py                              # prune memory/, move expired to memory/.archive/
    tools/prune.py --dry-run                    # report what would be pruned, don't move
    tools/prune.py --simulate 12months          # 12-month synthetic timeline; verify no monotonic growth (acceptance criterion 4)
    tools/prune.py --report                     # show counts by kind + age + budget impact

Expiry windows are read from `tools/expiry-windows.json` (per-kind day counts; null = never).

Cold archive: `memory/.archive/<original-relative-path>` — kept on disk so the user can
manually re-reference, but excluded from retrieval (the route.py loader skips
`memory/.archive/...`).

F3 mitigation: memory objects with `expiry_locked: true` in frontmatter are NEVER pruned
even if past `expires_at`. The compression pipeline must respect the same flag (compress.py
already preserves user-edited expires_at — the lock makes the contract explicit).
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402
from _tokens import estimate_tokens  # noqa: E402

_CFG = load_config()
METHOD_ROOT = _CFG.method_root
MEMORY_ROOT = _CFG.memory_root
ARCHIVE_ROOT = MEMORY_ROOT / ".archive"
WINDOWS_PATH = METHOD_ROOT / "tools" / "expiry-windows.json"
PROJECT_ROOT = METHOD_ROOT  # legacy alias


# ───────────────────────────────────────────────────────────────────────
# Config loading
# ───────────────────────────────────────────────────────────────────────

@dataclass
class ExpiryConfig:
    windows: dict[str, int | None]
    default_for_unknown_kind: int

    def days_for(self, kind: str) -> int | None:
        if kind in self.windows:
            return self.windows[kind]
        return self.default_for_unknown_kind

    @classmethod
    def load(cls) -> "ExpiryConfig":
        data = json.loads(WINDOWS_PATH.read_text(encoding="utf-8"))
        return cls(
            windows=data["windows"],
            default_for_unknown_kind=data.get("default_for_unknown_kind", 90),
        )


# ───────────────────────────────────────────────────────────────────────
# Frontmatter helpers
# ───────────────────────────────────────────────────────────────────────

def parse_memory_object(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: no YAML frontmatter")
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        raise ValueError(f"{path}: frontmatter not closed")
    front = yaml.safe_load(parts[0][4:])
    return front or {}, parts[1].lstrip("\n")


def list_memory_files() -> list[Path]:
    return sorted(p for p in MEMORY_ROOT.rglob("*.md") if ARCHIVE_ROOT not in p.parents)


def is_expired(front: dict, now: datetime) -> bool:
    expires_at = front.get("expires_at")
    if expires_at is None or expires_at == "null" or expires_at == "":
        return False
    if isinstance(expires_at, datetime):
        dt = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt < now


def is_locked(front: dict) -> bool:
    return bool(front.get("expiry_locked", False))


def body_token_count(body: str) -> int:
    return estimate_tokens(body)


# ───────────────────────────────────────────────────────────────────────
# Prune
# ───────────────────────────────────────────────────────────────────────

@dataclass
class PruneSummary:
    moved: list[str] = field(default_factory=list)
    skipped_not_expired: int = 0
    skipped_locked: int = 0
    errors: list[str] = field(default_factory=list)
    retrievable_token_count: int = 0


def prune(*, now: datetime | None = None, dry_run: bool = False) -> PruneSummary:
    now = now or datetime.now(timezone.utc)
    summary = PruneSummary()
    for path in list_memory_files():
        try:
            front, body = parse_memory_object(path)
        except (OSError, ValueError) as exc:
            summary.errors.append(f"{path}: {exc}")
            continue
        if not is_expired(front, now):
            summary.skipped_not_expired += 1
            summary.retrievable_token_count += body_token_count(body)
            continue
        if is_locked(front):
            summary.skipped_locked += 1
            summary.retrievable_token_count += body_token_count(body)
            continue
        rel = path.relative_to(MEMORY_ROOT)
        target = ARCHIVE_ROOT / rel
        if dry_run:
            summary.moved.append(f"(dry-run) {rel}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            summary.moved.append(str(rel))
    return summary


def report() -> dict:
    """Read-only inventory: counts by kind + age + retrievable tokens."""
    config = ExpiryConfig.load()
    now = datetime.now(timezone.utc)
    by_kind: dict[str, int] = {}
    age_buckets = {"<30d": 0, "30-90d": 0, "90-180d": 0, ">180d": 0}
    expiring_soon: list[str] = []
    total_tokens = 0
    files = list_memory_files()
    for path in files:
        try:
            front, body = parse_memory_object(path)
        except (OSError, ValueError):
            continue
        kind = front.get("kind", "<unknown>")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        created_at_raw = front.get("created_at")
        if created_at_raw:
            ca = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            age_days = (now - ca).days
            if age_days < 30:
                age_buckets["<30d"] += 1
            elif age_days < 90:
                age_buckets["30-90d"] += 1
            elif age_days < 180:
                age_buckets["90-180d"] += 1
            else:
                age_buckets[">180d"] += 1
        expires_at_raw = front.get("expires_at")
        if expires_at_raw and expires_at_raw not in (None, "null"):
            try:
                ea = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
                if ea.tzinfo is None:
                    ea = ea.replace(tzinfo=timezone.utc)
                if 0 <= (ea - now).days <= 14:
                    # Display path relative to method root when possible (fallback mode);
                    # otherwise relative to content root; otherwise absolute. The two
                    # try blocks below are SEPARATE from the datetime parse so a path
                    # outside both roots doesn't silently drop the expiring entry —
                    # which was the pattern flagged in PR #17 review.
                    try:
                        disp = str(path.relative_to(METHOD_ROOT))
                    except ValueError:
                        try:
                            disp = str(path.relative_to(_CFG.content_root))
                        except ValueError:
                            disp = str(path)
                    expiring_soon.append(disp)
            except ValueError:
                # Malformed expires_at — skip its expiry tracking but keep this
                # entry in the corpus accounting (token total below).
                pass
        total_tokens += body_token_count(body)
    return {
        "files": len(files),
        "retrievable_tokens": total_tokens,
        "by_kind": by_kind,
        "age_buckets": age_buckets,
        "expiring_within_14d": expiring_soon,
        "config_windows": config.windows,
    }


# ───────────────────────────────────────────────────────────────────────
# Recency decay (used by route.py retrieval; here as a library function)
# ───────────────────────────────────────────────────────────────────────

def recency_weight(created_at: datetime, now: datetime, half_life_days: float = 90.0) -> float:
    """Exponential decay: e^(-age_days/half_life). 0 days → 1.0; half_life days → 0.5."""
    age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
    return math.exp(-age_days * math.log(2) / half_life_days)


# ───────────────────────────────────────────────────────────────────────
# 12-month simulation (acceptance criterion 4)
# ───────────────────────────────────────────────────────────────────────

# Realistic kind distribution per Sei + the user's harvester sources.
# Adds up to 1.0. Emphasizes thread/note (most common from harvester) and
# a few non-expiring kinds (decision/legal) so the simulation surfaces
# whether the strategy/weekly/thread expiry actually keeps the corpus bounded
# even as legal/decision items accumulate.
SIM_KIND_DISTRIBUTION: dict[str, float] = {
    "thread": 0.35,
    "note": 0.20,
    "weekly": 0.15,
    "strategy": 0.08,
    "retrospective": 0.06,
    "decision": 0.06,
    "legal": 0.04,
    "incident": 0.03,
    "customer_call": 0.03,
}


def simulate_12_months(*, items_per_month: int = 30, seed: int = 42) -> dict:
    """Runs a synthetic 12-month timeline; writes synthetic memory objects to
    a temp directory; runs prune at the end of each month; reports the
    retrievable token count over time. Asserts no monotonic growth.

    Synthetic items are constructed without invoking the LLM — body content is
    deterministic filler at a controlled token count, so the simulation tests
    prune logic, not compression. Compression is exercised separately by #3.
    """
    import random
    import tempfile

    rng = random.Random(seed)
    config = ExpiryConfig.load()

    # Use a temp memory root so we don't touch real memory/.
    # We make the prune function operate on this temp root.
    global MEMORY_ROOT, ARCHIVE_ROOT
    saved_memory_root = MEMORY_ROOT
    saved_archive_root = ARCHIVE_ROOT
    tmp_root = Path(tempfile.mkdtemp(prefix="prune-sim-"))
    MEMORY_ROOT = tmp_root
    ARCHIVE_ROOT = tmp_root / ".archive"

    try:
        kinds = list(SIM_KIND_DISTRIBUTION.keys())
        weights = list(SIM_KIND_DISTRIBUTION.values())

        # Body filler that's ~600 tokens. Repeating phrase of known density.
        filler_phrase = "Synthetic memory body for prune simulation. " * 100  # ~700 tokens
        filler_tokens = body_token_count(filler_phrase)

        timeline: list[dict] = []
        # Start 12 months ago and walk forward.
        start = datetime(2025, 5, 5, tzinfo=timezone.utc)
        for month in range(12):
            month_start = start + timedelta(days=30 * month)
            month_end = month_start + timedelta(days=30)
            # Write items_per_month items uniformly across this month.
            for _ in range(items_per_month):
                kind = rng.choices(kinds, weights=weights, k=1)[0]
                created = month_start + timedelta(seconds=rng.randint(0, 30 * 86400 - 1))
                ttl_days = config.days_for(kind)
                expires = (created + timedelta(days=ttl_days)) if ttl_days else None
                front = {
                    "id": f"mem-sim-{uuid.uuid4()}",
                    "source_uri": f"sim://item-{rng.randint(1000, 9999)}",
                    "source_kind": "sim",
                    "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ") if expires else None,
                    "kind": kind,
                    "tags": ["sim", kind],
                }
                body = filler_phrase
                rel_path = MEMORY_ROOT / kind / f"{front['id']}.md"
                rel_path.parent.mkdir(parents=True, exist_ok=True)
                rendered = (
                    "---\n"
                    + yaml.safe_dump(front, sort_keys=False, default_flow_style=False)
                    + "---\n\n"
                    + body
                )
                rel_path.write_text(rendered, encoding="utf-8")
            # End-of-month prune at simulated "now" = end of month
            summary = prune(now=month_end, dry_run=False)
            timeline.append({
                "month": month + 1,
                "month_end": month_end.isoformat(),
                "moved": len(summary.moved),
                "retrievable_tokens": summary.retrievable_token_count,
                "retrievable_files": len(list_memory_files()),
            })

        # Falsifier check: layer-2 token count must not grow monotonically.
        # We accept growth in early months as the corpus warms up, but by
        # month 4+, expiry should bite and total retrievable should not be
        # strictly increasing month-over-month for the rest of the year.
        retrievable = [t["retrievable_tokens"] for t in timeline]
        plateau = retrievable[3:]  # months 4-12 should show net pruning behaviour
        is_monotonic_after_warmup = all(plateau[i] < plateau[i + 1] for i in range(len(plateau) - 1))

        return {
            "items_per_month": items_per_month,
            "kind_distribution": SIM_KIND_DISTRIBUTION,
            "timeline": timeline,
            "retrievable_tokens_series": retrievable,
            "monotonic_growth_after_warmup": is_monotonic_after_warmup,
            "falsifier_fires": is_monotonic_after_warmup,
            "filler_tokens_per_item": filler_tokens,
        }
    finally:
        MEMORY_ROOT = saved_memory_root
        ARCHIVE_ROOT = saved_archive_root
        shutil.rmtree(tmp_root, ignore_errors=True)


# ───────────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Prune expired memory objects + simulate 12-month timeline.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be pruned without moving.")
    parser.add_argument("--simulate", help="Run synthetic timeline. Currently supports: 12months.")
    parser.add_argument("--items-per-month", type=int, default=30, help="Items written per month in simulate mode.")
    parser.add_argument("--report", action="store_true", help="Read-only inventory of memory/.")
    parser.add_argument("--json", action="store_true", help="JSON output.")
    args = parser.parse_args(argv[1:])

    if args.simulate:
        if args.simulate != "12months":
            parser.error("only --simulate 12months is supported")
        result = simulate_12_months(items_per_month=args.items_per_month)
        out = json.dumps(result, indent=2, default=str)
        print(out)
        return 1 if result["falsifier_fires"] else 0

    if args.report:
        r = report()
        if args.json:
            print(json.dumps(r, indent=2))
        else:
            print(f"files: {r['files']}")
            print(f"retrievable tokens: {r['retrievable_tokens']}")
            print(f"by kind: {r['by_kind']}")
            print(f"age buckets: {r['age_buckets']}")
            print(f"expiring within 14d: {len(r['expiring_within_14d'])} files")
        return 0

    s = prune(dry_run=args.dry_run)
    if args.json:
        print(json.dumps({
            "moved": s.moved,
            "skipped_not_expired": s.skipped_not_expired,
            "skipped_locked": s.skipped_locked,
            "retrievable_token_count": s.retrievable_token_count,
            "errors": s.errors,
        }, indent=2))
    else:
        print(f"moved: {len(s.moved)}")
        for m in s.moved:
            print(f"  - {m}")
        print(f"skipped (not expired): {s.skipped_not_expired}")
        print(f"skipped (locked): {s.skipped_locked}")
        print(f"retrievable tokens: {s.retrievable_token_count}")
        if s.errors:
            print(f"errors:")
            for e in s.errors:
                print(f"  - {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
