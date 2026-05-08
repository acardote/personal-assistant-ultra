#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""kb-process — interactive consumer of kb-scan candidate memos (#121 / #116).

Walks `<vault>/artefacts/memo/.unprocessed/`. The user-approval gate lives
in the assistant's chat conversation; this tool is the file-operations
primitive. Subcommands:

  list   — print unprocessed memos (one per line: art-id kind referent)
  show   — print a memo's full body
  apply  — extract the proposed diff, append to the right kb file with
           inline `<!-- produced_by -->` carrying the CURRENT session_id
           (NOT the routine session that emitted the memo — F3 closer
           on #121). Move memo to `.processed/`. Run lint-provenance.
  reject — move memo to `.rejected/` without applying.

Atomic ordering: every state-changing subcommand maintains the invariant
**kb content ⟺ memo in `.processed/`** (F5 closer):
  apply: WRITE kb → LINT → MOVE memo. Lint failure rolls back the kb write
         and leaves memo in .unprocessed/ — no kb dirty + memo elsewhere.
  reject: MOVE memo to .rejected/ in one rename (no kb write).

Idempotency: apply checks if the memo's id already appears in the target
kb file's existing `<!-- produced_by ... art-<uuid> ... -->` comments. If
yes, refuses with a clear error (F4 closer).
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
    """Pull the ```diff block out of the memo body. Strip leading `+ ` from
    each line — that's the diff format kb-scan emits, marking lines to add."""
    m = DIFF_BLOCK_RE.search(memo_body)
    if not m:
        raise ValueError("memo body has no ```diff block")
    diff = m.group("body")
    out_lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+ "):
            out_lines.append(line[2:])
        elif line.startswith("+"):
            out_lines.append(line[1:])
        else:
            # Lines without `+` prefix (context, headers) — preserve verbatim.
            out_lines.append(line)
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
    (returncode, stderr)."""
    lint_path = method_root / "tools" / "lint-provenance.py"
    if not lint_path.is_file():
        return 0, ""  # lint not available; soft-pass
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
    if args.json:
        out = []
        for p in memos:
            try:
                fm, _ = parse_memo_frontmatter(p)
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
            kind = detect_memo_kind(fm)
            referent = detect_memo_referent(fm)
        except ValueError as exc:
            kind, referent = "MALFORMED", str(exc)
        print(f"  {p.stem:<{width}}  [{kind}]  {referent}")
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
