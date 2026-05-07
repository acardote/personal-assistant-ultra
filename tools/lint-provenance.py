#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Lint agent-produced KB + artefact provenance per ADR-0003.

Catches:
  - Method-repo `kb/glossary.md` containing `<!-- produced_by -->` comments
    (glossary uses PR-only provenance per ADR-0003; the source file ships
    to other users, so embedding session_id + query would leak context).
  - Vault `kb/{people,org,decisions}.md` headings dated post-ADR-acceptance
    (2026-05-07) that lack a `<!-- produced_by ... -->` comment, OR have
    a malformed one (missing required fields, non-canonical source forms).
  - Vault `artefacts/<kind>/art-*.md` missing YAML frontmatter with the
    required `produced_by` keys.
  - Vault `artefacts/export/art-*.<ext>` missing the sibling
    `<id>.provenance.json` sidecar.
  - Sources cited in any of the above using non-canonical forms (must be
    `kb#heading`, `mem://<memory-id>`, or `https://...`).

Grandfathering: KB headings whose `**Date:** <YYYY-MM-DD>` line predates
ADR_ACCEPTANCE_DATE are grandfathered (per ADR-0003 retroactive scope).
Headings without a Date line are also grandfathered — agent-produced
entries always carry a Date field.

Cross-repo behavior: the lint reads `<method_root>/.assistant.local.json`
to find the vault. When the config is missing or falls back, vault-side
checks are SKIPPED with a loud stderr notice. Pass `--require-vault` to
make missing config fail the lint (use this in vault-side CI).

Exit codes: 0 clean, 1 violations, 2 config error when --require-vault.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config

# Per ADR-0003 acceptance date. KB headings dated >= this need provenance.
ADR_ACCEPTANCE_DATE = "2026-05-07"

# `<!-- produced_by: session=<8-hex>, query="<short>", at=<iso8601>, sources=[<...>] -->`
PRODUCED_BY_RE = re.compile(
    r"<!--\s*produced_by:\s*(?P<body>[^>]*?)\s*-->",
    re.IGNORECASE,
)

# Canonical source forms per ADR-0003.
CANONICAL_SOURCE_RE = re.compile(
    r"^(?:"
    r"kb#[\w\-]+"                      # kb#heading-slug
    r"|mem://[\w\-]+"                  # mem://<memory-id>
    r"|https?://\S+"                   # bare URL
    r")$"
)

# `## <heading>` capture (ATX-style only; KB files don't use Setext).
H2_RE = re.compile(r"^##\s+(?P<title>\S.*?)\s*$", re.MULTILINE)

# Date markers across the three KB files: decisions.md uses `**Date:**`; people.md
# / org.md use `**Last verified:**`. Either qualifies as the date for grandfathering.
DATE_RE = re.compile(
    r"^\s*[-*]?\s*\*\*(?:Date|Last verified|Created|Verified):\*\*\s*(?P<date>\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)

# Entry-indicator: a section is an "entry" (vs format docs / schema) iff its
# body has at least one bullet of the shape `- **<field>:**`. Catches the
# F1 failure mode where an agent adds a new heading but forgets the date
# marker — the section still looks like an entry, so it can't slip past
# grandfathering by being undated.
ENTRY_BULLET_RE = re.compile(r"^\s*-\s+\*\*[A-Z][\w\s/-]*:\*\*", re.MULTILINE)

# 8 lowercase hex chars per ADR-0003 session_id format.
SESSION_RE = re.compile(r"^[0-9a-f]{8}$")

# ISO8601 — accept date or full timestamp (with or without zone). Calendar-validating
# this is overkill; we just need a sane shape. Wrong shape is what the lint catches.
ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:Z|[+\-]\d{2}:?\d{2})?)?$")

REQUIRED_FRONTMATTER_KEYS = {"id", "kind", "created_at", "produced_by", "title"}
REQUIRED_PRODUCED_BY_KEYS = {"session_id", "query", "model", "sources_cited"}

VALID_KINDS = {"analysis", "plan", "draft", "report", "export", "memo"}


class Violation:
    __slots__ = ("path", "line", "kind", "message")

    def __init__(self, path: Path, line: int | None, kind: str, message: str):
        self.path = path
        self.line = line
        self.kind = kind
        self.message = message

    def render(self, base: Path) -> str:
        try:
            rel = self.path.relative_to(base)
        except ValueError:
            rel = self.path
        loc = f"{rel}:{self.line}" if self.line else str(rel)
        return f"[{self.kind}] {loc}: {self.message}"


def parse_produced_by(body: str) -> dict[str, str | list[str]]:
    """Parse the comma-separated key=value body of an inline produced_by comment.

    Format: `session=<8-hex>, query="<short>", at=<iso8601>, sources=[<a>, <b>]`.
    Returns dict; missing keys absent (caller validates required ones).
    """
    out: dict[str, str | list[str]] = {}
    # Split on top-level commas only — the sources=[...] list has internal commas.
    depth = 0
    chunks: list[str] = []
    cur: list[str] = []
    for ch in body:
        if ch == "[":
            depth += 1
            cur.append(ch)
        elif ch == "]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            chunks.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        chunks.append("".join(cur))

    for chunk in chunks:
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        k = k.strip()
        v = v.strip()
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            items = [item.strip() for item in inner.split(",") if item.strip()]
            out[k] = items
        else:
            out[k] = v
    return out


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def validate_produced_by_comment(
    body: str, *, ctx: str
) -> tuple[bool, list[str]]:
    """Return (ok, list-of-error-messages) for an inline produced_by body."""
    errors: list[str] = []
    fields = parse_produced_by(body)
    session = fields.get("session", "")
    query = fields.get("query", "")
    at = fields.get("at", "")
    sources = fields.get("sources", [])

    if not isinstance(session, str) or not SESSION_RE.match(session):
        errors.append(f"{ctx}: produced_by.session must be 8 lowercase hex chars (got {session!r})")
    if not isinstance(query, str) or not query.strip():
        errors.append(f"{ctx}: produced_by.query is empty")
    if not isinstance(at, str) or not ISO8601_RE.match(at):
        errors.append(f"{ctx}: produced_by.at must be ISO8601 (got {at!r})")
    if not isinstance(sources, list) or not sources:
        errors.append(f"{ctx}: produced_by.sources is empty (need ≥1 canonical entry)")
    else:
        for src in sources:
            if not CANONICAL_SOURCE_RE.match(src):
                errors.append(
                    f"{ctx}: produced_by source {src!r} is not canonical "
                    f"(need kb#heading | mem://<id> | https://...)"
                )

    return (len(errors) == 0), errors


def check_method_glossary(method_root: Path) -> list[Violation]:
    """Glossary must NOT contain `<!-- produced_by -->` (PR-only provenance)."""
    glossary = method_root / "kb" / "glossary.md"
    if not glossary.is_file():
        return []
    text = glossary.read_text(encoding="utf-8")
    out: list[Violation] = []
    for m in PRODUCED_BY_RE.finditer(text):
        out.append(Violation(
            path=glossary,
            line=line_of(text, m.start()),
            kind="glossary-must-be-clean",
            message=(
                "method-scoped glossary must not embed produced_by — "
                "use PR-only provenance (per ADR-0003)"
            ),
        ))
    return out


def _strip_code_fences(text: str) -> str:
    """Replace text inside fenced code blocks with blank lines so heading /
    bullet regexes don't match against schema examples documented inside them.
    The line count stays correct so line_of() still reports right line numbers.
    Recognizes triple-backtick and triple-tilde fences (no info-string parsing
    — only fence detection). Indented code blocks are not stripped (rare in
    KB files; over-engineering would be premature)."""
    out: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in text.split("\n"):
        stripped = line.lstrip()
        if not in_fence and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = True
            fence_marker = stripped[:3]
            out.append("")  # the fence opener line itself isn't markdown content
            continue
        if in_fence and stripped.startswith(fence_marker):
            in_fence = False
            fence_marker = ""
            out.append("")
            continue
        if in_fence:
            out.append("")  # blank line preserves the line count
        else:
            out.append(line)
    return "\n".join(out)


def split_kb_sections(text: str) -> list[tuple[str, int, str]]:
    """Return list of (heading_title, heading_line_no, section_body).

    Strips fenced code blocks so format-template examples documented inside
    backticks (e.g., the `## <Decision title>` schema example in decisions.md)
    don't get treated as real headings."""
    text = _strip_code_fences(text)
    matches = list(H2_RE.finditer(text))
    sections: list[tuple[str, int, str]] = []
    for i, m in enumerate(matches):
        title = m.group("title")
        line = line_of(text, m.start())
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((title, line, text[body_start:body_end]))
    return sections


def check_vault_kb(content_root: Path) -> list[Violation]:
    """Vault KB headings dated >= ADR_ACCEPTANCE_DATE must have a well-formed
    produced_by comment. Earlier-dated or undated headings are grandfathered.
    Any produced_by that IS present (even on grandfathered entries) is validated
    so manual additions don't drift."""
    out: list[Violation] = []
    kb_dir = content_root / "kb"
    for fname in ("people.md", "org.md", "decisions.md"):
        path = kb_dir / fname
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        sections = split_kb_sections(text)
        for title, line, body in sections:
            date_match = DATE_RE.search(body)
            entry_date = date_match.group("date") if date_match else None
            comment_match = PRODUCED_BY_RE.search(body)
            looks_like_entry = bool(ENTRY_BULLET_RE.search(body))

            # F1 closer: post-acceptance dated entry without provenance.
            if entry_date and entry_date >= ADR_ACCEPTANCE_DATE and not comment_match:
                out.append(Violation(
                    path=path,
                    line=line,
                    kind="kb-missing-produced-by",
                    message=(
                        f"heading '## {title}' dated {entry_date} (>= ADR-0003 "
                        f"acceptance {ADR_ACCEPTANCE_DATE}) requires "
                        f"<!-- produced_by: ... -->"
                    ),
                ))

            # F1 closer (B1 fixup): undated *entry* sections — body has bullet
            # fields like `- **Role:**` — can't slip through grandfathering. If
            # there is no date marker but the section is shaped like an entry,
            # require provenance regardless. Format/schema sections (no bullet
            # fields) stay grandfathered.
            if not entry_date and looks_like_entry and not comment_match:
                out.append(Violation(
                    path=path,
                    line=line,
                    kind="kb-missing-produced-by",
                    message=(
                        f"heading '## {title}' has entry-shape body (bullet fields) "
                        f"but no date marker (`**Date:**` / `**Last verified:**`) "
                        f"AND no <!-- produced_by: ... --> — cannot be grandfathered"
                    ),
                ))

            if comment_match:
                ctx = f"heading '## {title}'"
                ok, errors = validate_produced_by_comment(comment_match.group("body"), ctx=ctx)
                if not ok:
                    body_line_offset = line + body[:comment_match.start()].count("\n")
                    for err in errors:
                        out.append(Violation(
                            path=path,
                            line=body_line_offset,
                            kind="kb-malformed-produced-by",
                            message=err,
                        ))
    return out


def parse_yaml_frontmatter(text: str) -> dict | None:
    """Minimal YAML-frontmatter extractor for our well-known shape.

    Returns None if no frontmatter or unparseable. We deliberately avoid pulling
    in PyYAML — the artefact frontmatter is a closed schema (we wrote both the
    producer and the lint) and small enough to parse with a hand-written walker
    that supports our specific shapes (scalar, list, single-level nested map).
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    block = text[4:end]
    return _parse_simple_yaml(block)


def _parse_simple_yaml(text: str) -> dict | None:
    """Parse a constrained YAML subset: top-level scalars + lists, plus one
    level of nested maps with their own scalars/lists. Returns None on parse
    error."""
    out: dict = {}
    cur_key: str | None = None
    cur_nested: dict | None = None
    cur_list_key: str | None = None
    cur_list: list | None = None

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        # 2-space indent → nested under the most recent top-level mapping key.
        if line.startswith("  "):
            inner = line[2:]
            if inner.startswith("- "):
                # list item under the current top-level key, OR nested key.
                item = inner[2:].strip()
                if cur_list is not None:
                    cur_list.append(item)
                elif cur_nested is not None and cur_list_key is not None:
                    cur_nested.setdefault(cur_list_key, []).append(item)
                else:
                    return None
                i += 1
                continue
            # nested key under cur_key (4-space deep would be sub-nested; not supported)
            if cur_nested is None:
                # inline expand: cur_key was scalar; the nested 2-space line is its dict shape
                cur_nested = {}
                out[cur_key] = cur_nested
            stripped = inner.lstrip()
            indent_inner = len(inner) - len(stripped)
            # 4-space inside the nested map = list under nested key
            if indent_inner == 2 and stripped.startswith("- "):
                item = stripped[2:].strip()
                if cur_list_key is None:
                    return None
                cur_nested.setdefault(cur_list_key, []).append(item)
                i += 1
                continue
            if ":" not in stripped:
                return None
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                cur_list_key = k
                cur_nested[k] = []
            else:
                cur_nested[k] = v
                cur_list_key = None
            i += 1
            continue
        # top-level
        cur_nested = None
        cur_list_key = None
        cur_list = None
        if ":" not in line:
            return None
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        cur_key = k
        if v == "":
            # could be a nested map OR a list — peek next non-blank line
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].lstrip().startswith("- "):
                cur_list = []
                out[k] = cur_list
                cur_nested = None
            else:
                cur_nested = {}
                out[k] = cur_nested
        else:
            out[k] = v
            cur_nested = None
        i += 1
    return out


def check_artefact_md(path: Path) -> list[Violation]:
    """Markdown artefact: must have YAML frontmatter with required produced_by."""
    out: list[Violation] = []
    text = path.read_text(encoding="utf-8")
    fm = parse_yaml_frontmatter(text)
    if fm is None:
        out.append(Violation(
            path=path,
            line=1,
            kind="artefact-missing-frontmatter",
            message="markdown artefact must start with YAML frontmatter delimited by ---",
        ))
        return out
    missing = REQUIRED_FRONTMATTER_KEYS - fm.keys()
    if missing:
        out.append(Violation(
            path=path,
            line=1,
            kind="artefact-missing-keys",
            message=f"frontmatter missing required keys: {sorted(missing)}",
        ))
    kind = fm.get("kind", "")
    if isinstance(kind, str) and kind and kind not in VALID_KINDS:
        out.append(Violation(
            path=path,
            line=1,
            kind="artefact-invalid-kind",
            message=f"kind must be one of {sorted(VALID_KINDS)} (got {kind!r})",
        ))
    pb = fm.get("produced_by")
    if not isinstance(pb, dict):
        out.append(Violation(
            path=path,
            line=1,
            kind="artefact-malformed-produced-by",
            message="produced_by must be a YAML map",
        ))
        return out
    out.extend(_validate_produced_by_dict(path, pb))
    return out


def _validate_produced_by_dict(path: Path, pb: dict) -> list[Violation]:
    """Shared validator for produced_by dicts (Markdown frontmatter + sidecar JSON)."""
    out: list[Violation] = []
    pb_missing = REQUIRED_PRODUCED_BY_KEYS - pb.keys()
    if pb_missing:
        out.append(Violation(
            path=path,
            line=1,
            kind="artefact-produced-by-missing-keys",
            message=f"produced_by missing required keys: {sorted(pb_missing)}",
        ))
    sources = pb.get("sources_cited", [])
    if not isinstance(sources, list) or not sources:
        out.append(Violation(
            path=path,
            line=1,
            kind="artefact-empty-sources",
            message="produced_by.sources_cited is empty (need ≥1 canonical entry)",
        ))
    else:
        for src in sources:
            if not isinstance(src, str) or not CANONICAL_SOURCE_RE.match(src):
                out.append(Violation(
                    path=path,
                    line=1,
                    kind="artefact-non-canonical-source",
                    message=(
                        f"sources_cited entry {src!r} not canonical "
                        f"(need kb#heading | mem://<id> | https://...)"
                    ),
                ))
    return out


def check_artefact_export(path: Path) -> list[Violation]:
    """Non-text artefact: must have sibling `<id>.provenance.json` with produced_by."""
    sidecar = path.with_suffix(".provenance.json")
    if not sidecar.is_file():
        return [Violation(
            path=path,
            line=None,
            kind="export-missing-sidecar",
            message=f"non-text artefact requires sidecar {sidecar.name} in same dir",
        )]
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [Violation(
            path=sidecar,
            line=None,
            kind="export-sidecar-malformed",
            message=f"sidecar is not valid JSON: {exc}",
        )]
    if not isinstance(data, dict):
        return [Violation(
            path=sidecar,
            line=None,
            kind="export-sidecar-malformed",
            message="sidecar root must be a JSON object",
        )]
    return _validate_produced_by_dict(sidecar, data)


def check_vault_artefacts(content_root: Path) -> list[Violation]:
    out: list[Violation] = []
    art_dir = content_root / "artefacts"
    if not art_dir.is_dir():
        return out
    for kind_dir in sorted(art_dir.iterdir()):
        if not kind_dir.is_dir():
            continue
        kind = kind_dir.name
        if kind not in VALID_KINDS:
            continue
        for path in sorted(kind_dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            # Skip non-art files (.gitkeep, README, etc.) and sidecars (validated alongside).
            if not name.startswith("art-"):
                continue
            if name.endswith(".provenance.json"):
                continue
            if kind == "export":
                out.extend(check_artefact_export(path))
            elif name.endswith(".md"):
                out.extend(check_artefact_md(path))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument(
        "--require-vault",
        action="store_true",
        help="exit 2 if .assistant.local.json is missing or fell back (use in vault-side CI)",
    )
    p.add_argument(
        "--method-only",
        action="store_true",
        help="check only method-side rules (glossary), skip vault — for method-repo CI",
    )
    args = p.parse_args(argv)

    cfg = load_config(require_explicit_content_root=False)
    method_root = cfg.method_root

    violations: list[Violation] = []
    violations.extend(check_method_glossary(method_root))

    if args.method_only:
        skipped_reason = "vault checks skipped: --method-only"
        vault_active = False
    elif cfg.config_source == "fallback":
        if args.require_vault:
            print("[lint-provenance] --require-vault set but no vault config; aborting", file=sys.stderr)
            return 2
        skipped_reason = (
            "vault checks skipped: .assistant.local.json missing or fell back "
            "(set up a vault config or pass --method-only to silence)"
        )
        vault_active = False
    else:
        skipped_reason = None
        vault_active = True

    if vault_active:
        violations.extend(check_vault_kb(cfg.content_root))
        violations.extend(check_vault_artefacts(cfg.content_root))

    if skipped_reason and not args.method_only:
        print(f"[lint-provenance] NOTE: {skipped_reason}", file=sys.stderr)

    if violations:
        # Sort for stable output: method-side first (glossary), then by path.
        violations.sort(key=lambda v: (str(v.path), v.line or 0))
        print(f"[lint-provenance] {len(violations)} violation(s):", file=sys.stderr)
        for v in violations:
            print("  " + v.render(method_root if v.path.is_relative_to(method_root) else cfg.content_root), file=sys.stderr)
        return 1

    scope = "method only" if not vault_active else "method + vault"
    print(f"[lint-provenance] clean ({scope}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
