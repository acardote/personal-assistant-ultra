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

# Canonical source forms per ADR-0003 (with art:// added in Amendment 1).
#
# Per #199 / C1 (#200): `kb#<heading>` accepts the literal KB heading text —
# spaces, punctuation, em-dashes, and unicode chars (accented, CJK, emoji)
# are all fine. The old shape `kb#[\w\-]+` rejected the natural human-and-
# agent shape (every Vera vision memo carried `kb#Phase 1 is Atlas 2.0,
# Vera is Phase 2` and similar). No tool resolves `kb#` references
# programmatically today (verified via grep on `tools/` + skills + docs),
# so loosening the shape doesn't break any consumer; it aligns the lint
# with how humans actually write.
#
# The first AND last chars of the body must be non-whitespace (so `kb#` /
# `kb# X` / `kb#X ` all refuse — degenerate empty, leading-space, and
# trailing-space shapes). Per pr-challenger #3 on #202: refusing trailing
# whitespace removes a copy-paste / sloppy-edit footgun at zero cost.
#
# Future-resolver contract: if/when an assembled-KB resolver lands per
# ADR-0003 line 96 (aspirational), it must accept the permissive body
# shape this lint emits — single non-whitespace OR `\S<body>\S`.
CANONICAL_SOURCE_RE = re.compile(
    r"^(?:"
    r"kb#\S(?:[^\n]*\S)?"              # kb#heading-text (literal; refuses leading/trailing ws)
    r"|mem://[\w\-]+"                  # mem://<memory-id>
    r"|art://[\w\-]+"                  # art://<art-uuid> (per Amendment 1)
    r"|https?://\S+"                   # bare URL
    r")$"
)

# `## <heading>` capture (ATX-style only; KB files don't use Setext).
H2_RE = re.compile(r"^##\s+(?P<title>\S.*?)\s*$", re.MULTILINE)


def _normalize_art_uri_body(uri_body: str) -> str:
    """Strip a leading `art-` prefix from an `art://` URI body so the lookup
    against `known_artefact_uuids` (keyed by bare uuid) matches both shapes:

    - `art://<uuid>` — ADR-0003 canonical, what `kb-drift-scan` / `kb-process`
      emit. Body is already bare; this is a no-op.
    - `art://art-<uuid>` — hand-authored shape (e.g. Vera vision memos that
      surfaced #193). Strip the redundant `art-` to recover the bare uuid.

    Per #193 / C3 (#196). Real UUIDs cannot legitimately begin with the
    literal `art-` (only `a` is hex among `a`/`r`/`t`), so the strip is
    safe by uuid-shape invariant."""
    if uri_body.startswith("art-"):
        return uri_body[len("art-"):]
    return uri_body

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
# Per #198 / C1 (#201): `source-pin` is a transient artefact wrapping
# upstream content awaiting canonical `mem://` promotion (see ADR-0003
# Amendment 2). Schema differs from the default in two ways:
#
# - REQUIRED_FRONTMATTER_KEYS_SOURCE_PIN adds top-level `upstream:` (a map
#   carrying at least a `kind:` subfield, plus kind-specific id fields like
#   `granola_meeting_id`). Top-level placement (not under produced_by) keeps
#   the lint's single-level-nested-map parser happy AND splits semantics
#   cleanly: `upstream` is about what the artefact wraps; `produced_by` is
#   about how it was made.
# - REQUIRED_PRODUCED_BY_KEYS_SOURCE_PIN drops `sources_cited` — the
#   upstream IS the provenance; no separate sources list applies.
REQUIRED_FRONTMATTER_KEYS_SOURCE_PIN = REQUIRED_FRONTMATTER_KEYS | {"upstream"}
REQUIRED_PRODUCED_BY_KEYS_SOURCE_PIN = {"session_id", "query", "model"}

VALID_KINDS = {"analysis", "plan", "draft", "report", "export", "memo", "source-pin"}
# Per #198 / C1 (#201): `source-pin` is a transient artefact kind wrapping
# upstream content (granola meeting, slack thread, etc.) before the harvest
# pipeline mints a canonical `mem://` id for it. See ADR-0003 Amendment 2.
# Distinct from `memo` (agent-produced compression) — carries an `upstream:`
# frontmatter block instead of standard `sources_cited:` provenance.

# Project slug shape per ADR-0003 Amendment 1: <YYYYMMDD>-<short-name>-<4hex>.
# Short-name must start AND end alphanumeric (matches tools/project.py).
PROJECT_SLUG_RE = re.compile(r"^\d{8}-[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?-[0-9a-f]{4}$")

# Drift-candidate memo schema (#137 / parent #135). When `drift_candidate: true`
# is set on a memo's frontmatter, these three fields become required and validated.
# When the flag is absent / falsy, all four fields are silently ignored (F1
# backward-compat: existing memos must be unaffected).
DRIFT_REQUIRED_WHEN_FLAG = ("affects_decision", "drift_claim", "drift_confidence")
DRIFT_CONFIDENCE_VALUES = {"high", "medium", "low"}
ART_REF_RE = re.compile(r"^art://[\w\-]+$")


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


def check_artefact_md(
    path: Path,
    *,
    expected_project_id: str | None,
    known_artefact_uuids: set[str],
) -> list[Violation]:
    """Markdown artefact: must have YAML frontmatter with required produced_by.

    `expected_project_id` is set when the artefact lives under
    `<content_root>/projects/<slug>/artefacts/...` — frontmatter `project_id`
    must equal `<slug>`. Flat artefacts pass `None` and skip the check.

    `known_artefact_uuids` is plumbed to the produced_by validator so that
    `art://<uuid>` references in sources_cited can be checked for resolution."""
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
    # Per #198 / C1 (#201): source-pin kind requires top-level `upstream:`
    # in addition to the standard keys. See REQUIRED_FRONTMATTER_KEYS_SOURCE_PIN.
    fm_kind = fm.get("kind", "")
    fm_required = (
        REQUIRED_FRONTMATTER_KEYS_SOURCE_PIN
        if isinstance(fm_kind, str) and fm_kind == "source-pin"
        else REQUIRED_FRONTMATTER_KEYS
    )
    missing = fm_required - fm.keys()
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

    # Project-id check (Amendment 1).
    if expected_project_id is not None:
        actual = fm.get("project_id")
        if actual is None or actual == "":
            out.append(Violation(
                path=path,
                line=1,
                kind="artefact-missing-project-id",
                message=f"project-scoped artefact missing project_id (expected {expected_project_id!r})",
            ))
        elif actual != expected_project_id:
            out.append(Violation(
                path=path,
                line=1,
                kind="artefact-project-id-mismatch",
                message=(
                    f"project_id={actual!r} doesn't match parent project "
                    f"directory {expected_project_id!r}"
                ),
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
    out.extend(_validate_produced_by_dict(path, pb, kind=kind, known_artefact_uuids=known_artefact_uuids))
    if isinstance(kind, str) and kind == "source-pin":
        out.extend(_validate_source_pin_upstream(path, fm))
    out.extend(_validate_drift_fields(path, fm, known_artefact_uuids=known_artefact_uuids))
    return out


def _validate_source_pin_upstream(path: Path, fm: dict) -> list[Violation]:
    """Validate the top-level `upstream:` field on `kind: source-pin` artefacts.
    Per #198 / C1 (#201) + ADR-0003 Amendment 2: must be a YAML map with at
    least a `kind:` subfield (e.g., granola_note, slack_thread). Subfields
    beyond `kind` are kind-specific (granola_meeting_id, slack_message_ts,
    etc.) and not validated here — the lint validates shape, not upstream
    resolution."""
    out: list[Violation] = []
    upstream = fm.get("upstream")
    if upstream is None:
        # Already covered by REQUIRED_FRONTMATTER_KEYS_SOURCE_PIN missing-keys.
        return out
    if not isinstance(upstream, dict):
        out.append(Violation(
            path=path,
            line=1,
            kind="artefact-malformed-upstream",
            message="upstream must be a YAML map (source-pin kind)",
        ))
        return out
    if not isinstance(upstream.get("kind"), str) or not upstream.get("kind"):
        out.append(Violation(
            path=path,
            line=1,
            kind="artefact-upstream-missing-kind",
            message="upstream missing required `kind` subfield (e.g., granola_note, slack_thread)",
        ))
    return out


def _drift_flag_state(fm: dict) -> tuple[str, object]:
    """Classify the `drift_candidate` field into one of four states:

      - 'absent'    — key missing entirely; skip validation (F1).
      - 'false'     — explicit `false` (any case, optional quotes); skip.
      - 'true'      — explicit `true` (any case, optional quotes); validate.
      - 'malformed' — present but parseable as neither (empty value, typo,
                      nested-map artifact). Emit `drift-candidate-malformed`
                      so a half-written flag doesn't silently fail-open.

    Returns (state, raw_value)."""
    if "drift_candidate" not in fm:
        return ("absent", None)
    v = fm["drift_candidate"]
    if isinstance(v, str):
        s = v.strip().strip('"').strip("'").lower()
        if s == "true":
            return ("true", v)
        if s == "false":
            return ("false", v)
    return ("malformed", v)


def _validate_drift_fields(
    path: Path,
    fm: dict,
    *,
    known_artefact_uuids: set[str] | None,
) -> list[Violation]:
    """Drift-candidate memo schema (slice 1 of parent #135).

    Activation: only when `drift_candidate: true` is set on a `kind=memo`
    artefact. Absent / `false` flag short-circuits — no errors raised against
    fields that weren't intended as drift fields (F1 backward-compat).

    Edge cases:
      - `drift_candidate:` with empty value, or any non-bool string, fails
        with `drift-candidate-malformed`. Without this gate, a typo'd flag
        silently disables validation — fail-open that defeats F1's purpose.
      - `drift_candidate: true` on a non-memo `kind` fails with
        `drift-on-non-memo`. Same fail-open class: the spec scopes drift
        candidates to memos; allowing it elsewhere lets validation be
        silently bypassed by writing the wrong `kind`.

    When active (state='true' AND kind=memo), validates:
      - `affects_decision`, `drift_claim`, `drift_confidence` all present
        (`drift-missing-required`).
      - `affects_decision` is in `art://<id>` shape (`drift-affects-malformed`)
        — distinct error from absence so operators can tell 'add the field'
        apart from 'fix the field' (F3).
      - `affects_decision` resolves against the vault's all-uuids index
        (`drift-affects-dangling`) — without resolution the downstream drift
        detector would silently target nonexistent decisions (F2).
      - `drift_confidence` ∈ {high, medium, low} — slice 5 guardrails filter
        on this value, so invalid entries must surface here, not at digest time.
    """
    out: list[Violation] = []
    state, raw = _drift_flag_state(fm)
    if state in ("absent", "false"):
        return out
    if state == "malformed":
        out.append(Violation(
            path=path,
            line=1,
            kind="drift-candidate-malformed",
            message=(
                f"drift_candidate must be exactly 'true' or 'false' "
                f"(got {raw!r}); empty values or typos silently disable "
                f"drift validation — fail-open"
            ),
        ))
        return out

    kind = fm.get("kind")
    if kind != "memo":
        out.append(Violation(
            path=path,
            line=1,
            kind="drift-on-non-memo",
            message=(
                f"drift_candidate: true is only valid on kind=memo "
                f"(got kind={kind!r}); see #137 — drift-candidate schema scope"
            ),
        ))
        return out

    missing: list[str] = []
    for k in DRIFT_REQUIRED_WHEN_FLAG:
        v = fm.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(k)
    if missing:
        out.append(Violation(
            path=path,
            line=1,
            kind="drift-missing-required",
            message=(
                f"drift_candidate: true requires {list(DRIFT_REQUIRED_WHEN_FLAG)}; "
                f"missing: {missing}"
            ),
        ))

    aff = fm.get("affects_decision")
    if isinstance(aff, str) and aff.strip():
        aff_s = aff.strip().strip('"').strip("'")
        if not ART_REF_RE.match(aff_s):
            out.append(Violation(
                path=path,
                line=1,
                kind="drift-affects-malformed",
                message=(
                    f"affects_decision must be art://<uuid> shape, got {aff_s!r}"
                ),
            ))
        elif known_artefact_uuids is not None:
            ref_uuid = _normalize_art_uri_body(aff_s[len("art://"):])
            if ref_uuid not in known_artefact_uuids:
                out.append(Violation(
                    path=path,
                    line=1,
                    kind="drift-affects-dangling",
                    message=(
                        f"affects_decision={aff_s!r} doesn't resolve to any "
                        f"vault artefact (project tier ∪ flat tier). The drift "
                        f"target memo must exist before drift candidates land."
                    ),
                ))

    conf = fm.get("drift_confidence")
    if isinstance(conf, str) and conf.strip():
        conf_s = conf.strip().strip('"').strip("'")
        if conf_s not in DRIFT_CONFIDENCE_VALUES:
            out.append(Violation(
                path=path,
                line=1,
                kind="drift-confidence-invalid",
                message=(
                    f"drift_confidence must be one of "
                    f"{sorted(DRIFT_CONFIDENCE_VALUES)} (got {conf_s!r})"
                ),
            ))

    return out


def _validate_produced_by_dict(
    path: Path,
    pb: dict,
    *,
    kind: str = "",
    known_artefact_uuids: set[str] | None = None,
) -> list[Violation]:
    """Shared validator for produced_by dicts (Markdown frontmatter + sidecar JSON).

    `known_artefact_uuids` is the set of all art-uuids found across the vault
    (project tier + flat tier). When passed, `art://<uuid>` references in
    sources_cited are checked for resolution — dangling refs fail the lint
    (per #98). When None (e.g., per-file invocation outside the vault walk),
    the dangling-check is skipped.

    Per #198 / C1 (#201): `kind` selects the required-key set:
    - `source-pin` requires `upstream:` block (transient artefact wrapping
      upstream content) instead of `sources_cited:`.
    - All other kinds require `sources_cited:` (the standard agent-produced
      shape).
    See ADR-0003 Amendment 2."""
    out: list[Violation] = []
    is_source_pin = (kind == "source-pin")
    required = REQUIRED_PRODUCED_BY_KEYS_SOURCE_PIN if is_source_pin else REQUIRED_PRODUCED_BY_KEYS
    pb_missing = required - pb.keys()
    if pb_missing:
        out.append(Violation(
            path=path,
            line=1,
            kind="artefact-produced-by-missing-keys",
            message=f"produced_by missing required keys: {sorted(pb_missing)}",
        ))
    # source-pin: `sources_cited` is NOT required (upstream IS the provenance).
    # Top-level `upstream:` validation lives in `_validate_source_pin_upstream`.
    # However, if a source-pin author copies a memo template and ends up with
    # `sources_cited:` present-but-malformed (empty list, non-list), don't
    # silently tolerate it — validate the shape when present, even if it's
    # optional (per pr-reviewer #3 on PR #203).
    if is_source_pin:
        if "sources_cited" in pb:
            sources = pb.get("sources_cited", [])
            if not isinstance(sources, list):
                out.append(Violation(
                    path=path,
                    line=1,
                    kind="artefact-malformed-sources",
                    message="produced_by.sources_cited must be a list (got non-list value)",
                ))
            # Empty list is fine on source-pin — upstream block carries the
            # provenance. Non-canonical entries WHEN PRESENT are still violations.
            elif sources:
                for src in sources:
                    if not isinstance(src, str) or not CANONICAL_SOURCE_RE.match(src):
                        out.append(Violation(
                            path=path,
                            line=1,
                            kind="artefact-non-canonical-source",
                            message=(
                                f"sources_cited entry {src!r} not canonical "
                                f"(need kb#heading | mem://<id> | art://<id> | https://...)"
                            ),
                        ))
        return out
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
                        f"(need kb#heading | mem://<id> | art://<id> | https://...)"
                    ),
                ))
                continue
            # Dangling-art-ref check (#98): art://<uuid> must resolve to a
            # known artefact in the vault. Skip when no index passed.
            if isinstance(src, str) and src.startswith("art://") and known_artefact_uuids is not None:
                ref_uuid = _normalize_art_uri_body(src[len("art://"):])
                if ref_uuid not in known_artefact_uuids:
                    out.append(Violation(
                        path=path,
                        line=1,
                        kind="artefact-dangling-art-ref",
                        message=(
                            f"sources_cited entry {src!r} points at no existing "
                            f"artefact in the vault (project tier ∪ flat tier). "
                            f"Either create art-{ref_uuid}.<ext> in artefacts/<kind>/ "
                            f"or remove this entry from sources_cited."
                        ),
                    ))
    return out


def check_artefact_export(
    path: Path,
    *,
    expected_project_id: str | None,
    known_artefact_uuids: set[str],
) -> list[Violation]:
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

    out: list[Violation] = []
    if expected_project_id is not None:
        actual = data.get("project_id")
        if actual is None or actual == "":
            out.append(Violation(
                path=sidecar,
                line=None,
                kind="artefact-missing-project-id",
                message=f"project-scoped export sidecar missing project_id (expected {expected_project_id!r})",
            ))
        elif actual != expected_project_id:
            out.append(Violation(
                path=sidecar,
                line=None,
                kind="artefact-project-id-mismatch",
                message=(
                    f"project_id={actual!r} doesn't match parent project "
                    f"directory {expected_project_id!r}"
                ),
            ))
    # Per pr-reviewer + pr-challenger on PR #203: source-pin is markdown-only
    # per ADR-0003 Amendment 2 — no export sidecars exist for kind=source-pin.
    # If that ever changes, thread `kind=` through this call to route to the
    # source-pin produced_by schema; today the default standard path is correct
    # because every export artefact has `kind: export`.
    out.extend(_validate_produced_by_dict(sidecar, data, known_artefact_uuids=known_artefact_uuids))
    return out


def _walk_artefact_tree(
    art_dir: Path,
    *,
    expected_project_id: str | None,
    known_artefact_uuids: set[str],
) -> list[Violation]:
    """Walk artefacts/<kind>/art-* and dispatch each file to the right checker.
    Returns also a dict-of-uuid-to-paths via the sentinel uuid_index parameter
    to enable cross-tier duplicate detection at the caller level."""
    out: list[Violation] = []
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
            if not name.startswith("art-"):
                continue
            if name.endswith(".provenance.json"):
                continue
            if kind == "export":
                out.extend(check_artefact_export(
                    path,
                    expected_project_id=expected_project_id,
                    known_artefact_uuids=known_artefact_uuids,
                ))
            elif name.endswith(".md"):
                out.extend(check_artefact_md(
                    path,
                    expected_project_id=expected_project_id,
                    known_artefact_uuids=known_artefact_uuids,
                ))
    return out


def _collect_artefact_uuids(art_dir: Path) -> dict[str, list[Path]]:
    """Map art-uuid → list of body file paths under this artefacts/ root.
    Used by check_vault_artefacts to detect cross-tier id collisions
    (Amendment 1 invariant: each art-<uuid> resolves to exactly one file)."""
    index: dict[str, list[Path]] = {}
    if not art_dir.is_dir():
        return index
    for kind_dir in art_dir.iterdir():
        if not kind_dir.is_dir() or kind_dir.name not in VALID_KINDS:
            continue
        for path in kind_dir.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if not name.startswith("art-") or name.endswith(".provenance.json"):
                continue
            # Strip "art-" and the file extension to get the uuid.
            # Index is keyed by bare uuid per ADR-0003 (resolver scans
            # for `art-<uuid>.<ext>` from the URI body, so the URI body
            # itself is bare-uuid). Ref-check sites strip a leading
            # `art-` prefix from the URI body before lookup, tolerating
            # both `art://<uuid>` and `art://art-<uuid>` shapes (the
            # latter is what some hand-authored memos use, e.g. Vera
            # vision memos — see #193 / C3).
            stem = path.stem  # filename without final extension
            if not stem.startswith("art-"):
                continue
            art_uuid = stem[len("art-"):]
            index.setdefault(art_uuid, []).append(path)
    return index


def check_project_slugs(projects_root: Path) -> list[Violation]:
    """Per #99: every directory in `<content_root>/projects/` (excluding
    dot-prefixed dirs which are scaffolding by convention — `.template/`
    today, future `.archive/` etc.) must match the slug convention from
    ADR-0003 Amendment 1: `<YYYYMMDD>-<short-name>-<4hex>`.

    Hand-rolled slugs subvert the cross-machine collision defense (the
    4hex suffix). Use `tools/project.py new` to generate conforming slugs."""
    out: list[Violation] = []
    if not projects_root.is_dir():
        return out
    for child in sorted(projects_root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not PROJECT_SLUG_RE.match(child.name):
            out.append(Violation(
                path=child,
                line=None,
                kind="project-slug-malformed",
                message=(
                    f"project directory {child.name!r} doesn't match the "
                    f"slug convention `<YYYYMMDD>-<short-name>-<4hex>` per "
                    f"ADR-0003 Amendment 1. Use `tools/project.py new "
                    f"<short-name> '<intent>'` to generate conforming slugs "
                    f"for new projects. For an existing folder where the slug "
                    f"is load-bearing in external references (slack permalinks, "
                    f"shared docs), either rename + update the references, or "
                    f"prepend the dot-prefix to opt out of the lint."
                ),
            ))
    return out


def check_vault_artefacts(content_root: Path) -> list[Violation]:
    out: list[Violation] = []

    flat_root = content_root / "artefacts"
    projects_root = content_root / "projects"

    out.extend(check_project_slugs(projects_root))

    # Pass 1 (per #98): build the all-uuids index BEFORE per-artefact walks
    # so dangling-art-ref checks can resolve against the full vault.
    flat_uuids = _collect_artefact_uuids(flat_root)
    project_uuid_indices: dict[str, dict[str, list[Path]]] = {}
    if projects_root.is_dir():
        for proj_dir in sorted(projects_root.iterdir()):
            if not proj_dir.is_dir() or proj_dir.name.startswith("."):
                continue
            project_uuid_indices[proj_dir.name] = _collect_artefact_uuids(proj_dir / "artefacts")

    all_uuids: dict[str, list[Path]] = {k: list(v) for k, v in flat_uuids.items()}
    for slug_index in project_uuid_indices.values():
        for uid, paths in slug_index.items():
            all_uuids.setdefault(uid, []).extend(paths)
    known_artefact_uuids: set[str] = set(all_uuids.keys())

    # Pass 2: per-artefact walks (with the index plumbed for art:// resolution).
    out.extend(_walk_artefact_tree(
        flat_root, expected_project_id=None, known_artefact_uuids=known_artefact_uuids,
    ))
    if projects_root.is_dir():
        for proj_dir in sorted(projects_root.iterdir()):
            if not proj_dir.is_dir() or proj_dir.name.startswith("."):
                continue
            out.extend(_walk_artefact_tree(
                proj_dir / "artefacts",
                expected_project_id=proj_dir.name,
                known_artefact_uuids=known_artefact_uuids,
            ))

    # Cross-tier duplicate-uuid invariant (Amendment 1).
    for uid, paths in all_uuids.items():
        if len(paths) > 1:
            out.append(Violation(
                path=paths[0],
                line=None,
                kind="artefact-uuid-collision",
                message=(
                    f"art://{uid} resolves to {len(paths)} files — invariant violation "
                    f"per ADR-0003 Amendment 1. Files: {[str(p.relative_to(content_root)) for p in paths]}"
                ),
            ))

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
    p.add_argument(
        "--content-root",
        metavar="PATH",
        default=None,
        help=(
            "override vault content_root (skips .assistant.local.json). Intended "
            "for repro scripts and tests that need a sandboxed vault — production "
            "callers should rely on the config file."
        ),
    )
    args = p.parse_args(argv)

    # Per pr-reviewer + pr-challenger on #195 (closer #194 of #193): when the
    # caller supplied `--content-root`, skip the .assistant.local.json
    # lookup entirely. Avoids the spurious "config fallback" warning + lets
    # repro scripts and tests sandbox the lint without touching shared
    # config files. The override is treated as an authoritative vault
    # (`config_source = "cli-override"`), not a fallback.
    if args.content_root:
        from dataclasses import replace
        # Construct a minimal config without invoking load_config (which
        # would otherwise emit the fallback warning).
        method_root_path = Path(__file__).resolve().parent.parent
        override = Path(args.content_root).expanduser().resolve()
        from _config import Config as _Config
        cfg = _Config(
            method_root=method_root_path,
            content_root=override,
            config_source="cli-override",
            config_path=method_root_path / ".assistant.local.json",
        )
    else:
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
