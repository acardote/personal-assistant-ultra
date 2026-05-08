#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""kb-process — interactive consumer of kb-scan candidate memos (#121 / #116).

Walks `<vault>/artefacts/memo/.unprocessed/`. The user-approval gate lives
in the assistant's chat conversation; this tool is the file-operations
primitive. Subcommands:

  list           — print unprocessed memos (drift candidates marked [DRIFT])
  show           — print a memo's full body
  apply          — extract the proposed diff, append to the right kb file
                   with inline `<!-- produced_by -->` carrying the CURRENT
                   session_id (NOT the routine session that emitted the
                   memo — F3 closer on #121). Move memo to `.processed/`.
                   Run lint-provenance. **Refuses on drift candidates** —
                   the user must use drift-apply for those.
  reject         — move memo to `.rejected/` without applying.
  drift-apply    — append a `### <iso-date> — ...` amendment under the
                   decision named in `affects_decision: art://<via-uuid>`
                   (slice 3 of #135). Resolves the via-uuid against the
                   current `kb/decisions.md` at apply time (F5: a stale
                   reference is refused, not silently appended). Otherwise
                   matches `apply`'s atomic write→lint→move ordering.
  drift-dismiss  — archive the drift memo to `.rejected/` and record a
                   dismissal entry under `<vault>/.harvest/drift-dismissals/
                   <via-uuid>.json` so slice 4's suppression mechanism can
                   read the per-decision dismissal count.

Atomic ordering: every state-changing subcommand maintains the invariant
**kb content ⟺ memo in `.processed/`** (F5 closer):
  apply / drift-apply: WRITE kb → LINT → MOVE memo. Lint failure rolls
         back the kb write and leaves memo in .unprocessed/.
  reject / drift-dismiss: MOVE memo to .rejected/ in one rename (no kb write).

Idempotency: apply / drift-apply check if the memo's id already appears in
the target kb file's existing `<!-- produced_by ... via=art-<uuid> ... -->`
comments. If yes, refuse (F4 closer for apply, F3 closer for drift-apply).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config  # noqa: E402

VAULT_KIND_TO_FILE = {
    "person": "people.md",
    "org": "org.md",
    "decision": "decisions.md",
}

# Glossary updates use PR-only provenance per docs/kb-editorial-rules.md;
# kb-process refuses to auto-write to method glossary.
GLOSSARY_REFUSAL = (
    "glossary candidates need PR-only provenance against the method repo "
    "(per editorial-rules amendment in #117). kb-process won't auto-write "
    "method-side glossary.md. Open a PR manually with the proposed diff in "
    "the body."
)


# ---------------------------------------------------------------------
# Memo discovery
# ---------------------------------------------------------------------


def memo_dir(content_root: Path, bucket: str) -> Path:
    return content_root / "artefacts" / "memo" / f".{bucket}"


def list_memos(content_root: Path, bucket: str = "unprocessed") -> list[Path]:
    d = memo_dir(content_root, bucket)
    if not d.is_dir():
        return []
    return sorted(d.glob("art-*.md"))


def parse_memo_frontmatter(memo_path: Path) -> tuple[dict, str]:
    """Return (frontmatter-dict, body-string). Raises ValueError on shape failure."""
    text = memo_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{memo_path.name}: missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError(f"{memo_path.name}: unterminated frontmatter")
    fm = yaml.safe_load(text[4:end])
    if not isinstance(fm, dict):
        raise ValueError(f"{memo_path.name}: frontmatter is not a YAML map")
    body = text[end + 4:].lstrip("\n")
    return fm, body


def is_drift_candidate(fm: dict) -> bool:
    """True iff frontmatter sets `drift_candidate: true` (case-insensitive,
    optional surrounding quotes — matches the slice-1 lint's `_drift_truthy`).
    Drift candidates require a separate handler; `apply` refuses on them."""
    v = fm.get("drift_candidate")
    # YAML scalar `true` parses to Python `True` via PyYAML safe_load (used here),
    # whereas the lint's hand-rolled walker keeps it as the string "true". Both
    # forms must be accepted.
    if isinstance(v, bool):
        return v is True
    if isinstance(v, str):
        return v.strip().strip('"').strip("'").lower() in ("true", "yes")
    return False


def detect_memo_kind(fm: dict) -> str:
    """Return person / org / decision / glossary based on the title prefix
    `Candidate <kind>: <referent>`. Defensive against missing/malformed."""
    title = str(fm.get("title", ""))
    m = re.match(r"Candidate\s+(person|org|decision|glossary)\s*:", title, re.IGNORECASE)
    if not m:
        raise ValueError(f"can't detect memo kind from title {title!r}")
    return m.group(1).lower()


def detect_memo_referent(fm: dict) -> str:
    title = str(fm.get("title", ""))
    m = re.match(r"Candidate\s+\w+\s*:\s*(.+?)\s*$", title)
    return m.group(1) if m else title


# ---------------------------------------------------------------------
# Diff extraction
# ---------------------------------------------------------------------


DIFF_BLOCK_RE = re.compile(
    r"```diff\s*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def extract_proposed_diff(memo_body: str) -> str:
    """Pull the ```diff block out of the memo body. STRICT mode: every
    non-blank line MUST start with `+` (followed by optional space). Refuses
    with ValueError otherwise — kb-scan is the only producer of these
    memos, so a non-conforming diff is a producer bug, not a thing to
    silently pass through (per pr-challenger #122 suggestion 3)."""
    m = DIFF_BLOCK_RE.search(memo_body)
    if not m:
        raise ValueError("memo body has no ```diff block")
    diff = m.group("body")
    out_lines: list[str] = []
    for n, line in enumerate(diff.splitlines(), start=1):
        if not line.strip():
            out_lines.append("")
            continue
        if line.startswith("+ "):
            out_lines.append(line[2:])
        elif line.startswith("+"):
            out_lines.append(line[1:])
        else:
            raise ValueError(
                f"diff line {n} lacks `+` prefix: {line!r}. The kb-scan "
                f"memo format requires every non-blank line in the ```diff "
                f"block to start with `+` — refusing to inject ambiguous "
                f"content into the kb file."
            )
    return "\n".join(out_lines).rstrip() + "\n"


# ---------------------------------------------------------------------
# Provenance comment
# ---------------------------------------------------------------------


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_id_from_env() -> str:
    """Return current PA_SESSION_ID if set+valid, else mint a fresh 8-hex.
    The slash command sets PA_SESSION_ID; this tool runs under that session."""
    sid = os.environ.get("PA_SESSION_ID", "").strip()
    if re.match(r"^[0-9a-f]{8}$", sid):
        return sid
    return uuid.uuid4().hex[:8]


def render_produced_by_comment(
    *, session_id: str, query: str, sources: list[str], memo_id: str
) -> str:
    """Inline produced_by comment per editorial-rules + ADR-0003.

    The session_id is the CURRENT interactive session (whoever ran apply),
    NOT the routine session that emitted the memo. This is F3's closer:
    the inline comment attributes the kb edit to the approval gate, not to
    the harvest. The memo_id is included in `sources` (or via a separate
    `via=` field) so the trail can be reconstructed if needed.
    """
    sources_str = ", ".join(sources) if sources else ""
    # Quote the query so commas inside it don't break parsing.
    return (
        f"<!-- produced_by: session={session_id}, "
        f"query=\"{query}\", "
        f"at={now_iso()}, "
        f"sources=[{sources_str}], "
        f"via={memo_id} -->"
    )


# ---------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------


def memo_already_applied(kb_text: str, memo_id: str) -> bool:
    """Return True if the kb file already contains a produced_by comment with
    via=<memo_id>. F4 closer: prevents duplicate entries on retry."""
    needle = f"via={memo_id}"
    return needle in kb_text


# ---------------------------------------------------------------------
# Lint integration
# ---------------------------------------------------------------------


def run_lint(method_root: Path) -> tuple[int, str]:
    """Run lint-provenance.py against the configured vault. Returns
    (returncode, stderr).

    Hard-fails (rc=2) if lint-provenance.py is missing — a configured vault
    + missing lint is a deployment misconfiguration, not soft-passable. The
    F5 atomic gate depends on the lint actually running (per pr-challenger
    #122 suggestion 1)."""
    lint_path = method_root / "tools" / "lint-provenance.py"
    if not lint_path.is_file():
        return 2, (
            f"lint-provenance.py not found at {lint_path}. apply requires "
            f"the lint to gate kb writes; refusing to proceed without it."
        )
    r = subprocess.run(
        [str(lint_path), "--require-vault"],
        capture_output=True, text=True,
    )
    return r.returncode, r.stderr


# ---------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------


def append_to_kb(kb_path: Path, comment: str, entry_text: str) -> None:
    """Append the entry block to the KB file. The produced_by comment goes
    INSIDE the new section, immediately after the `## <heading>` line —
    the lint walks sections starting at each `## ` and looks for the
    comment in the section body. Placing it ABOVE the heading would put
    it in the previous section's body and fail the lint.

    Adds a leading blank line if the file doesn't already end with one."""
    existing = kb_path.read_text(encoding="utf-8") if kb_path.is_file() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing and not existing.endswith("\n\n"):
        existing += "\n"

    # Split the entry into [heading line, ... rest ...] and inject the
    # comment immediately after the heading.
    entry_text = entry_text.rstrip("\n")
    lines = entry_text.split("\n", 1)
    if len(lines) == 2 and lines[0].startswith("## "):
        new_block = lines[0] + "\n" + comment + "\n" + lines[1].rstrip() + "\n"
    else:
        # Fallback: no recognizable heading — still emit, but lint may catch.
        new_block = comment + "\n" + entry_text + "\n"
    kb_path.write_text(existing + new_block, encoding="utf-8")


def cmd_apply(args, cfg) -> int:
    art_id = args.art_id
    memo_path = memo_dir(cfg.content_root, "unprocessed") / f"{art_id}.md"
    if not memo_path.is_file():
        print(f"[kb-process] memo {art_id} not found in .unprocessed/", file=sys.stderr)
        return 1

    try:
        fm, body = parse_memo_frontmatter(memo_path)
    except ValueError as exc:
        print(f"[kb-process] {exc}", file=sys.stderr)
        return 1

    # Drift candidates carry their own slice-3 handler (drift-apply): the
    # amendment shape is `### <date> — ...` under an existing decision, not
    # a fresh `## <heading>`. Refusing here avoids accidentally applying
    # the diff-block (which doesn't exist on drift memos) as a new entry.
    if is_drift_candidate(fm):
        print(
            f"[kb-process] {memo_path.name} is a drift candidate. "
            f"Use `kb-process drift-apply {art_id}` (per slice 3 of #135) "
            f"or `kb-process drift-dismiss {art_id}`.",
            file=sys.stderr,
        )
        return 1

    try:
        kind = detect_memo_kind(fm)
    except ValueError as exc:
        print(f"[kb-process] {exc}", file=sys.stderr)
        return 1

    if kind == "glossary":
        print(f"[kb-process] {GLOSSARY_REFUSAL}", file=sys.stderr)
        return 1
    if kind not in VAULT_KIND_TO_FILE:
        print(f"[kb-process] unknown kind: {kind}", file=sys.stderr)
        return 1

    target_path = cfg.content_root / "kb" / VAULT_KIND_TO_FILE[kind]

    # F4: idempotency check.
    existing_kb = target_path.read_text(encoding="utf-8") if target_path.is_file() else ""
    if memo_already_applied(existing_kb, art_id):
        print(
            f"[kb-process] memo {art_id} already applied to {target_path.name} "
            f"(via={art_id} marker present). Refusing duplicate write. "
            f"Use `kb-process reject {art_id}` if the memo should be archived.",
            file=sys.stderr,
        )
        return 1

    try:
        entry_text = extract_proposed_diff(body)
    except ValueError as exc:
        print(f"[kb-process] {exc}", file=sys.stderr)
        return 1

    pb_data = fm.get("produced_by") or {}
    sources = pb_data.get("sources_cited") or []
    if not isinstance(sources, list):
        sources = []
    referent = detect_memo_referent(fm)
    interactive_session = session_id_from_env()
    comment = render_produced_by_comment(
        session_id=interactive_session,
        query=f"kb-process apply: {kind} candidate '{referent}'",
        sources=[str(s) for s in sources],
        memo_id=art_id,
    )

    # Atomic-ish ordering: write kb → run lint → move memo.
    # If lint fails, roll back the kb write so we don't leave kb dirty.
    pre_state = existing_kb  # may be empty string if file didn't exist
    pre_existed = target_path.is_file()
    append_to_kb(target_path, comment, entry_text)

    rc, stderr = run_lint(cfg.method_root)
    if rc != 0:
        # Roll back.
        if pre_existed:
            target_path.write_text(pre_state, encoding="utf-8")
        else:
            target_path.unlink(missing_ok=True)
        print("[kb-process] lint-provenance refused after apply; rolled back kb write.", file=sys.stderr)
        print(stderr, file=sys.stderr)
        return 1

    # Lint clean — move memo to .processed/.
    processed_dir = memo_dir(cfg.content_root, "processed")
    processed_dir.mkdir(parents=True, exist_ok=True)
    final_memo = processed_dir / memo_path.name
    memo_path.replace(final_memo)

    print(f"[kb-process] applied {art_id} → {target_path.name}; archived to .processed/")
    return 0


# ---------------------------------------------------------------------
# Drift-apply (slice 3 of #135)
# ---------------------------------------------------------------------


def find_decision_section_by_via(text: str, via_uuid: str) -> Optional[tuple[int, int, str]]:
    """Locate the `## <title>` H2 section in `kb/decisions.md` whose body
    contains `via=art-<via_uuid>`. Return `(start_offset, end_offset, title)`
    or None when the via-uuid no longer resolves (F5: stale reference at
    apply time)."""
    sections = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    offset = 0
    needle = f"via=art-{via_uuid}"
    for sec in sections:
        next_offset = offset + len(sec)
        if not sec.startswith("## "):
            offset = next_offset
            continue
        if needle in sec:
            first_nl = sec.find("\n")
            title = sec[3:first_nl].strip() if first_nl > 0 else sec[3:].strip()
            return (offset, next_offset, title)
        offset = next_offset
    return None


def render_drift_amendment(
    *,
    memory_id: str,
    memory_source_kind: str,
    drift_claim: str,
    drift_confidence: str,
    via_uuid: str,
    memo_id: str,
    decision_title: str,
    session_id: str,
) -> str:
    """Render the `### <iso-date> — ...` amendment block per editorial-rules
    diff-shape rule. Comment carries the user's interactive session_id (F4
    closer); `via=` references the drift memo so reproducibility holds."""
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    one_line = f"drift amendment from mem-{memory_id[:12] if memory_id else 'unknown'}"
    sources = [f"mem://{memory_id}", f"art://{via_uuid}"] if memory_id else [f"art://{via_uuid}"]
    sources_str = ", ".join(sources)
    return (
        f"\n### {today} — {one_line}\n"
        f"<!-- produced_by: session={session_id}, "
        f"query=\"kb-process drift-apply: amendment to '{decision_title}'\", "
        f"at={now_iso()}, "
        f"sources=[{sources_str}], "
        f"via={memo_id} -->\n"
        f"- **Source memory:** mem://{memory_id} ({memory_source_kind})\n"
        f"- **Confidence:** {drift_confidence}\n\n"
        f"{drift_claim.strip()}\n"
    )


def cmd_drift_apply(args, cfg) -> int:
    art_id = args.art_id
    memo_path = memo_dir(cfg.content_root, "unprocessed") / f"{art_id}.md"
    if not memo_path.is_file():
        print(f"[kb-process] memo {art_id} not found in .unprocessed/", file=sys.stderr)
        return 1

    try:
        fm, _body = parse_memo_frontmatter(memo_path)
    except ValueError as exc:
        print(f"[kb-process] {exc}", file=sys.stderr)
        return 1

    if not is_drift_candidate(fm):
        print(
            f"[kb-process] {art_id} is not a drift candidate. Use `kb-process apply` instead.",
            file=sys.stderr,
        )
        return 1

    aff = fm.get("affects_decision", "")
    if not (isinstance(aff, str) and aff.startswith("art://")):
        print(f"[kb-process] {art_id}: affects_decision missing or not in art:// shape", file=sys.stderr)
        return 1
    via_uuid = aff[len("art://"):].strip()
    if not via_uuid:
        print(f"[kb-process] {art_id}: affects_decision has empty uuid", file=sys.stderr)
        return 1

    drift_claim = str(fm.get("drift_claim", "")).strip()
    if not drift_claim:
        print(f"[kb-process] {art_id}: drift_claim is empty", file=sys.stderr)
        return 1
    drift_confidence = str(fm.get("drift_confidence", "")).strip()

    # Surface the source memory id for the amendment body. The drift-scan
    # producer puts it in produced_by.sources_cited as `mem://<id>`.
    pb_data = fm.get("produced_by") or {}
    sources = pb_data.get("sources_cited") or []
    memory_id = ""
    memory_source_kind = ""
    if isinstance(sources, list):
        for s in sources:
            if isinstance(s, str) and s.startswith("mem://"):
                memory_id = s[len("mem://"):]
                # source_kind isn't structured in the memo; leave blank for now.
                # A future slice could embed it explicitly in produced_by.
                break

    decisions_path = cfg.content_root / "kb" / "decisions.md"
    if not decisions_path.is_file():
        print(f"[kb-process] kb/decisions.md not found", file=sys.stderr)
        return 1
    pre_state = decisions_path.read_text(encoding="utf-8")

    # F3 (idempotency): if the memo's via-marker already lives in decisions.md,
    # the amendment was already applied. Refuse — replay-after-crash mustn't
    # duplicate amendments.
    if memo_already_applied(pre_state, art_id):
        print(
            f"[kb-process] memo {art_id} already applied to kb/decisions.md "
            f"(via={art_id} marker present). Refusing duplicate write.",
            file=sys.stderr,
        )
        return 1

    # F5 (resolution-at-apply-time): the drift memo points at a decision
    # via-uuid that may no longer match any kb decision (renamed, deleted,
    # or its produced_by comment edited). Refuse rather than:
    #   - silently no-op (loses the user's intent),
    #   - appending a new heading (corrupts kb structure),
    #   - or writing an orphan amendment.
    bounds = find_decision_section_by_via(pre_state, via_uuid)
    if bounds is None:
        print(
            f"[kb-process] affects_decision art://{via_uuid} no longer resolves "
            f"to any decision in kb/decisions.md (renamed or deleted between "
            f"emission and apply). Refusing to write. Either restore the "
            f"decision or `drift-dismiss {art_id}`.",
            file=sys.stderr,
        )
        return 1
    section_start, section_end, decision_title = bounds

    interactive_session = session_id_from_env()
    amendment = render_drift_amendment(
        memory_id=memory_id,
        memory_source_kind=memory_source_kind,
        drift_claim=drift_claim,
        drift_confidence=drift_confidence,
        via_uuid=via_uuid,
        memo_id=art_id,
        decision_title=decision_title,
        session_id=interactive_session,
    )

    # Inject the amendment at the END of the target section (just before the
    # next ## heading or EOF). Strip trailing whitespace from the section to
    # keep paragraph spacing tight, then re-append the amendment.
    section_text = pre_state[section_start:section_end].rstrip("\n")
    new_section_text = section_text + amendment
    new_state = pre_state[:section_start] + new_section_text + pre_state[section_end:]

    # Atomic ordering: write → lint → move/rollback (matches cmd_apply).
    decisions_path.write_text(new_state, encoding="utf-8")

    rc, stderr = run_lint(cfg.method_root)
    if rc != 0:
        decisions_path.write_text(pre_state, encoding="utf-8")
        print("[kb-process] lint-provenance refused after drift-apply; rolled back kb write.", file=sys.stderr)
        print(stderr, file=sys.stderr)
        return 1

    processed_dir = memo_dir(cfg.content_root, "processed")
    processed_dir.mkdir(parents=True, exist_ok=True)
    final_memo = processed_dir / memo_path.name
    memo_path.replace(final_memo)

    print(
        f"[kb-process] drift-applied {art_id} → amendment under "
        f"`## {decision_title}` in decisions.md; archived to .processed/"
    )
    return 0


# ---------------------------------------------------------------------
# Drift-dismiss + dismissal counter
# ---------------------------------------------------------------------


def dismissal_dir(content_root: Path) -> Path:
    return content_root / ".harvest" / "drift-dismissals"


def record_dismissal(
    content_root: Path,
    *, via_uuid: str, art_id: str, reason: str | None,
) -> None:
    """Append a dismissal entry to `<vault>/.harvest/drift-dismissals/<via>.json`.
    Slice 4's suppression mechanism reads this — count of dismissals per
    decision feeds the per-decision threshold logic.

    Atomic write: tmp + rename so a concurrent reader never sees a partially-
    written file."""
    target_dir = dismissal_dir(content_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    p = target_dir / f"{via_uuid}.json"
    entries: list[dict] = []
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data.get("dismissals")
                if isinstance(existing, list):
                    entries = list(existing)
        except (json.JSONDecodeError, OSError):
            entries = []
    # Idempotency: don't double-count a dismissal already recorded for this art_id.
    if any(isinstance(e, dict) and e.get("art_id") == art_id for e in entries):
        return
    entries.append({
        "art_id": art_id,
        "dismissed_at": now_iso(),
        "reason": reason or "",
    })
    payload = {"via_uuid": via_uuid, "dismissals": entries}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def cmd_drift_dismiss(args, cfg) -> int:
    art_id = args.art_id
    memo_path = memo_dir(cfg.content_root, "unprocessed") / f"{art_id}.md"
    if not memo_path.is_file():
        print(f"[kb-process] memo {art_id} not found in .unprocessed/", file=sys.stderr)
        return 1

    try:
        fm, _body = parse_memo_frontmatter(memo_path)
    except ValueError as exc:
        print(f"[kb-process] {exc}", file=sys.stderr)
        return 1

    if not is_drift_candidate(fm):
        print(
            f"[kb-process] {art_id} is not a drift candidate. Use `kb-process reject` instead.",
            file=sys.stderr,
        )
        return 1

    aff = fm.get("affects_decision", "")
    via_uuid = ""
    if isinstance(aff, str) and aff.startswith("art://"):
        via_uuid = aff[len("art://"):].strip()

    rejected_dir = memo_dir(cfg.content_root, "rejected")
    rejected_dir.mkdir(parents=True, exist_ok=True)
    final_path = rejected_dir / memo_path.name
    memo_path.replace(final_path)

    if args.reason:
        reason_path = final_path.with_suffix(".reason.txt")
        reason_path.write_text(
            f"dismissed_at: {now_iso()}\nreason: {args.reason}\n",
            encoding="utf-8",
        )

    if via_uuid:
        record_dismissal(
            cfg.content_root, via_uuid=via_uuid, art_id=art_id,
            reason=args.reason,
        )

    print(f"[kb-process] dismissed drift candidate {art_id}; archived to .rejected/")
    return 0


# ---------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------


def cmd_reject(args, cfg) -> int:
    art_id = args.art_id
    memo_path = memo_dir(cfg.content_root, "unprocessed") / f"{art_id}.md"
    if not memo_path.is_file():
        print(f"[kb-process] memo {art_id} not found in .unprocessed/", file=sys.stderr)
        return 1

    rejected_dir = memo_dir(cfg.content_root, "rejected")
    rejected_dir.mkdir(parents=True, exist_ok=True)
    final_path = rejected_dir / memo_path.name
    memo_path.replace(final_path)

    if args.reason:
        # Drop a sidecar note with the reject reason for the trail.
        reason_path = final_path.with_suffix(".reason.txt")
        reason_path.write_text(
            f"rejected_at: {now_iso()}\nreason: {args.reason}\n",
            encoding="utf-8",
        )

    print(f"[kb-process] rejected {art_id}; archived to .rejected/")
    return 0


# ---------------------------------------------------------------------
# List + show
# ---------------------------------------------------------------------


def cmd_list(args, cfg) -> int:
    memos = list_memos(cfg.content_root, "unprocessed")

    if args.count:
        # Just the count, on stdout, suitable for `if [ "$(... --count)" -gt 0 ]`
        # consumption — used by the SKILL.md activation contract pre-flight
        # (per #127). Resolves the vault via .assistant.local.json so the
        # caller doesn't have to know the path.
        print(len(memos))
        return 0

    if args.json:
        out = []
        for p in memos:
            try:
                fm, _ = parse_memo_frontmatter(p)
                if is_drift_candidate(fm):
                    kind = "drift"
                    referent = str(fm.get("affects_decision", ""))
                else:
                    kind = detect_memo_kind(fm)
                    referent = detect_memo_referent(fm)
            except ValueError:
                kind, referent = "?", "?"
            out.append({"art_id": p.stem, "kind": kind, "referent": referent})
        print(json.dumps(out, indent=2))
        return 0

    if not memos:
        print("no unprocessed memos")
        return 0
    width = max(len(p.stem) for p in memos)
    print(f"{len(memos)} unprocessed memo(s):")
    for p in memos:
        try:
            fm, _ = parse_memo_frontmatter(p)
            if is_drift_candidate(fm):
                # Drift candidates carry their own [DRIFT] tag; the referent
                # is the affected decision so reviewers can group at-a-glance.
                tag = "DRIFT"
                referent = str(fm.get("affects_decision", "(no affects_decision)"))
            else:
                tag = detect_memo_kind(fm)
                referent = detect_memo_referent(fm)
        except ValueError as exc:
            tag, referent = "MALFORMED", str(exc)
        print(f"  {p.stem:<{width}}  [{tag}]  {referent}")
    return 0


def cmd_show(args, cfg) -> int:
    art_id = args.art_id
    for bucket in ("unprocessed", "processed", "rejected"):
        candidate = memo_dir(cfg.content_root, bucket) / f"{art_id}.md"
        if candidate.is_file():
            sys.stdout.write(candidate.read_text(encoding="utf-8"))
            print(f"\n[kb-process] memo found in .{bucket}/", file=sys.stderr)
            return 0
    print(f"[kb-process] memo {art_id} not found in any bucket", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list unprocessed candidate memos")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--count", action="store_true",
                        help="print just the integer count on stdout (for shell consumption)")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="print a memo's body")
    p_show.add_argument("art_id")
    p_show.set_defaults(func=cmd_show)

    p_apply = sub.add_parser("apply", help="apply a memo's diff to the kb file")
    p_apply.add_argument("art_id")
    p_apply.set_defaults(func=cmd_apply)

    p_reject = sub.add_parser("reject", help="archive a memo without applying")
    p_reject.add_argument("art_id")
    p_reject.add_argument("--reason", help="optional reason text recorded in a sidecar")
    p_reject.set_defaults(func=cmd_reject)

    p_drift_apply = sub.add_parser(
        "drift-apply",
        help="apply a drift candidate as a `### <date> — ...` amendment under the affected decision",
    )
    p_drift_apply.add_argument("art_id")
    p_drift_apply.set_defaults(func=cmd_drift_apply)

    p_drift_dismiss = sub.add_parser(
        "drift-dismiss",
        help="archive a drift candidate to .rejected/ + record dismissal count",
    )
    p_drift_dismiss.add_argument("art_id")
    p_drift_dismiss.add_argument("--reason", help="optional reason text recorded in a sidecar")
    p_drift_dismiss.set_defaults(func=cmd_drift_dismiss)

    args = p.parse_args(argv)
    cfg = load_config(require_explicit_content_root=True)
    return args.func(args, cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"[kb-process] ERROR: {e}", file=sys.stderr)
        sys.exit(2)
