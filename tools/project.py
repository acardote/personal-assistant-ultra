#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""PA project management — slugs, scaffolding, promote, copy-artefact, archive.

Per ADR-0003 Amendment 1 (parent issue #88, child #92).

Subcommands:
  new <short-name> "<intent>"   — scaffold a new project, set active.
  list [--include-archived]     — list projects.
  resume <slug-or-shortname>    — set the project as active. Print paths.
  archive <slug>                — flip status to archived.
  promote <art-uuid> <slug>     — move flat artefact into project.
  copy-artefact <art-uuid> <dest-slug>
                                — copy artefact across, mint fresh id.
  clear                         — clear active project.
  status                        — print active project info.

Active-project state file: <content_root>/.pa-active-project.json
Schema: {"slug": "<slug>", "set_at": "<iso8601>"}
Staleness threshold: 4 hours (per ADR-0003 Amendment 1).

Exit codes: 0 ok, 1 user error (collision, missing file, ambiguous name), 2 config error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_config

SLUG_RE = re.compile(r"^\d{8}-[a-z0-9-]+-[0-9a-f]{4}$")
# Short-name must start AND end alphanumeric, hyphens only between them — no
# `-foo`, `foo-`, `--foo--`, etc. (S2 from PR #93 review).
SHORT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")
ART_FILENAME_RE = re.compile(r"^art-(?P<uuid>[\w-]+)\.")
STATE_TTL_HOURS = 4

VALID_KINDS = {"analysis", "plan", "draft", "report", "export", "memo"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")


def state_file(content_root: Path) -> Path:
    return content_root / ".pa-active-project.json"


def read_state(content_root: Path) -> dict | None:
    p = state_file(content_root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        # Wrong-shape JSON (array, scalar) — treat as no state. S1 fix.
        return None
    return data


def write_state(content_root: Path, slug: str) -> None:
    state_file(content_root).write_text(
        json.dumps({"slug": slug, "set_at": now_iso()}) + "\n",
        encoding="utf-8",
    )


def clear_state(content_root: Path) -> None:
    p = state_file(content_root)
    if p.is_file():
        p.unlink()


def state_age_hours(state: dict) -> float | None:
    set_at = state.get("set_at")
    if not isinstance(set_at, str):
        return None
    try:
        # Handle Z suffix and +00:00 forms
        normalized = set_at.replace("Z", "+00:00")
        ts = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600.0


def projects_dir(content_root: Path) -> Path:
    return content_root / "projects"


def project_dir(content_root: Path, slug: str) -> Path:
    return projects_dir(content_root) / slug


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (top-level-scalars-dict, body-string). Tolerates absent frontmatter.

    NOTE: this parser is lossy by design — it only surfaces top-level scalars
    for read-only inspection (`project status`, `list`, etc.). Do NOT use it
    as a round-trip for editing files that may contain nested blocks (e.g.
    artefacts with `produced_by:` — destroys provenance). For that path use
    `surgical_update_frontmatter` below."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    body = text[end + 4:].lstrip("\n")
    fm: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(" "):
            continue  # nested — read-only parser ignores
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    return fm, body


_TOP_LEVEL_KEY_RE = re.compile(r"^([\w-]+):\s*(.*)$")


def surgical_update_frontmatter(text: str, updates: dict) -> str:
    """Update top-level YAML scalars in frontmatter line-by-line WITHOUT
    touching nested blocks. Adds new keys at the end of the frontmatter block.

    This preserves nested maps/lists like `produced_by: { session_id, query,
    model, sources_cited }` verbatim — the lossy parse_frontmatter would drop
    them, destroying provenance on every promote/copy/archive."""
    if not text.startswith("---\n"):
        # No frontmatter — synthesize one. Updates are flat by contract.
        out = ["---"]
        for k, v in updates.items():
            out.append(f"{k}:" if v == "" else f"{k}: {v}")
        out.append("---")
        return "\n".join(out) + "\n" + text

    end = text.find("\n---", 4)
    if end == -1:
        return text  # malformed; leave alone (will fail downstream lints)

    block = text[4:end]
    closing_and_body = text[end:]  # starts at "\n---..."

    lines = block.split("\n")
    out_lines: list[str] = []
    keys_remaining = dict(updates)

    i = 0
    while i < len(lines):
        line = lines[i]
        # Lines that are blank, indented (nested), or comments → preserve verbatim.
        if not line or line.startswith(" ") or line.startswith("\t") or line.lstrip().startswith("#"):
            out_lines.append(line)
            i += 1
            continue

        match = _TOP_LEVEL_KEY_RE.match(line)
        if not match:
            out_lines.append(line)
            i += 1
            continue

        key = match.group(1)
        existing_val = match.group(2)

        if key in keys_remaining:
            new_val = keys_remaining.pop(key)
            out_lines.append(f"{key}:" if new_val == "" else f"{key}: {new_val}")
            i += 1
            # If the existing value was empty AND followed by indented lines,
            # those were the OLD nested block — drop them (we replaced with a
            # scalar). For the use cases in this tool (overwriting project_id,
            # last_active, status, archived_at, derived_from, id), the new
            # value is always scalar so dropping the old nested block is safe.
            if existing_val == "":
                while i < len(lines) and lines[i].startswith((" ", "\t")):
                    i += 1
        else:
            out_lines.append(line)
            i += 1

    # Append any keys that didn't already exist.
    for k, v in keys_remaining.items():
        out_lines.append(f"{k}:" if v == "" else f"{k}: {v}")

    return "---\n" + "\n".join(out_lines) + closing_and_body


def render_frontmatter(fm: dict) -> str:
    """Round-trip frontmatter back to YAML. Preserves insertion order; values are
    written verbatim (caller is responsible for quoting)."""
    out = ["---"]
    for k, v in fm.items():
        if v == "" or v is None:
            out.append(f"{k}:")
        elif isinstance(v, list):
            out.append(f"{k}:")
            for item in v:
                out.append(f"  - {item}")
        elif "\n" in str(v):
            out.append(f"{k}: |")
            for line in str(v).splitlines():
                out.append(f"  {line}")
        else:
            out.append(f"{k}: {v}")
    out.append("---")
    return "\n".join(out) + "\n"


def gen_slug(short_name: str) -> str:
    if not SHORT_NAME_RE.match(short_name):
        raise SystemExit(
            f"short-name must match {SHORT_NAME_RE.pattern} "
            f"(got {short_name!r})"
        )
    return f"{today_utc()}-{short_name}-{secrets.token_hex(2)}"


def cmd_new(args, cfg) -> int:
    short = args.short_name
    intent = args.intent

    slug = gen_slug(short)
    target = project_dir(cfg.content_root, slug)
    if target.exists():
        # Astronomically unlikely (4hex collision on same day). Mint a fresh slug once.
        slug = gen_slug(short)
        target = project_dir(cfg.content_root, slug)
        if target.exists():
            print(f"slug collision on retry ({slug}) — refusing to overwrite", file=sys.stderr)
            return 1

    target.mkdir(parents=True)
    (target / "artefacts").mkdir()
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    # Write project.md directly (don't substitute against the annotated template
    # — its inline `# MUST equal …` comments would leak into substituted values).
    # The template at projects/.template/project.md is a reference for users
    # reading the layout, not a substitution source for `new`.
    fm = {
        "id": slug,
        "title": short,
        "intent": json.dumps(intent),
        "status": "active",
        "started_at": today,
        "last_active": today,
    }
    project_md = target / "project.md"
    project_md.write_text(
        render_frontmatter(fm)
        + "\n## Sources\n\n"
        "Canonical references this project draws from. Use `kb#<heading>`, "
        "`mem://<id>`, `art://<uuid>`, or `https://...`.\n\n"
        "## Decisions\n\n"
        "Project-local choices that aren't KB-worthy.\n\n"
        "## What's next\n\n"
        "What the assistant should pick up on the next session.\n",
        encoding="utf-8",
    )

    write_state(cfg.content_root, slug)

    print(f"slug={slug}")
    print(f"path={target}")
    print(f"export PA_PROJECT_ID={slug}")
    return 0


def project_summary(project_md: Path) -> dict:
    """Read frontmatter scalars from project.md."""
    if not project_md.is_file():
        return {}
    text = project_md.read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(text)
    return fm


def cmd_list(args, cfg) -> int:
    pdir = projects_dir(cfg.content_root)
    if not pdir.is_dir():
        print("no projects/ directory yet")
        return 0
    entries: list[tuple[str, dict]] = []
    for child in sorted(pdir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        fm = project_summary(child / "project.md")
        entries.append((child.name, fm))

    if not entries:
        print("no projects yet")
        return 0

    include_archived = args.include_archived
    rows = []
    for slug, fm in entries:
        status = fm.get("status", "active")
        if status == "archived" and not include_archived:
            continue
        rows.append((slug, fm.get("title", "?"), status, fm.get("last_active", "?")))

    if not rows:
        print("no active projects (use --include-archived to see archived)")
        return 0

    width = max(len(r[0]) for r in rows)
    for slug, title, status, last_active in rows:
        marker = " [A]" if status == "archived" else ""
        print(f"{slug:<{width}}  {last_active}  {title}{marker}")
    return 0


def find_slug(content_root: Path, query: str) -> str | None:
    """Resolve a slug or short-name to a full slug. Returns None if ambiguous or
    not found; prints diagnostic to stderr in those cases."""
    pdir = projects_dir(content_root)
    if not pdir.is_dir():
        return None

    # Exact match first
    if (pdir / query).is_dir():
        return query

    # Short-name match: find slugs whose middle component matches query.
    matches = []
    for child in pdir.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        # slug shape: YYYYMMDD-<short-name>-<4hex>. The short-name can contain
        # hyphens, so split off the date prefix and 4hex suffix and the rest is
        # the short-name.
        parts = child.name.split("-")
        if len(parts) < 3:
            continue
        short_name = "-".join(parts[1:-1])
        if short_name == query:
            matches.append(child.name)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"ambiguous: {len(matches)} projects match short-name {query!r}:", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
        return None
    return None


def cmd_resume(args, cfg) -> int:
    slug = find_slug(cfg.content_root, args.slug_or_shortname)
    if not slug:
        print(f"no project matching {args.slug_or_shortname!r}", file=sys.stderr)
        return 1

    target = project_dir(cfg.content_root, slug)
    project_md = target / "project.md"
    notes_md = target / "notes.md"

    write_state(cfg.content_root, slug)

    # Print machine-readable info first, then human-readable.
    print(f"slug={slug}")
    print(f"path={target}")
    print(f"export PA_PROJECT_ID={slug}")
    print()
    print(f"--- {project_md.relative_to(cfg.content_root)} ---")
    if project_md.is_file():
        sys.stdout.write(project_md.read_text(encoding="utf-8"))
    if notes_md.is_file():
        print()
        print(f"--- {notes_md.relative_to(cfg.content_root)} ---")
        sys.stdout.write(notes_md.read_text(encoding="utf-8"))

    # Manifest of artefacts
    art_dir = target / "artefacts"
    if art_dir.is_dir():
        print()
        print("--- artefact manifest ---")
        for kind_dir in sorted(art_dir.iterdir()):
            if not kind_dir.is_dir():
                continue
            for f in sorted(kind_dir.iterdir()):
                if f.name.startswith("art-") and not f.name.endswith(".provenance.json"):
                    print(f"  {kind_dir.name}/{f.name}")
    return 0


def cmd_clear(args, cfg) -> int:
    clear_state(cfg.content_root)
    print("cleared")
    return 0


def cmd_status(args, cfg) -> int:
    state = read_state(cfg.content_root)
    if not state:
        print("no project active")
        return 0
    slug = state.get("slug", "?")
    age = state_age_hours(state)
    fm = project_summary(project_dir(cfg.content_root, slug) / "project.md")
    print(f"active: {slug}")
    if age is not None:
        stale = " (STALE — re-resume to confirm)" if age > STATE_TTL_HOURS else ""
        print(f"age: {age:.1f}h{stale}")
    print(f"title: {fm.get('title', '?')}")
    print(f"last_active: {fm.get('last_active', '?')}")
    return 0


def cmd_archive(args, cfg) -> int:
    slug = find_slug(cfg.content_root, args.slug)
    if not slug:
        print(f"no project matching {args.slug!r}", file=sys.stderr)
        return 1

    project_md = project_dir(cfg.content_root, slug) / "project.md"
    if not project_md.is_file():
        print(f"{project_md} not found", file=sys.stderr)
        return 1

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    text = project_md.read_text(encoding="utf-8")
    project_md.write_text(
        surgical_update_frontmatter(text, {"status": "archived", "archived_at": today}),
        encoding="utf-8",
    )

    # If this was the active project, clear state.
    state = read_state(cfg.content_root)
    if state and state.get("slug") == slug:
        clear_state(cfg.content_root)

    print(f"archived: {slug}")
    return 0


def find_artefact(content_root: Path, art_uuid: str) -> Path | None:
    """Locate an artefact by UUID. Walks projects/*/artefacts/<kind>/ + flat
    artefacts/<kind>/. Returns the body file (not the sidecar) if found. None
    if not found OR if multiple matches (invariant violation per ADR-0003)."""
    matches: list[Path] = []
    # Project-scoped first
    pdir = content_root / "projects"
    if pdir.is_dir():
        for proj in pdir.iterdir():
            if not proj.is_dir() or proj.name.startswith("."):
                continue
            art_root = proj / "artefacts"
            if not art_root.is_dir():
                continue
            for kind_dir in art_root.iterdir():
                if not kind_dir.is_dir():
                    continue
                for f in kind_dir.iterdir():
                    if not f.is_file():
                        continue
                    if f.name.startswith(f"art-{art_uuid}.") and not f.name.endswith(".provenance.json"):
                        matches.append(f)
    # Flat
    flat = content_root / "artefacts"
    if flat.is_dir():
        for kind_dir in flat.iterdir():
            if not kind_dir.is_dir():
                continue
            for f in kind_dir.iterdir():
                if not f.is_file():
                    continue
                if f.name.startswith(f"art-{art_uuid}.") and not f.name.endswith(".provenance.json"):
                    matches.append(f)

    if len(matches) > 1:
        print(f"art:// invariant violation — multiple matches for {art_uuid}:", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
        return None
    return matches[0] if matches else None


def sibling_files(body_file: Path) -> list[Path]:
    """All `art-<uuid>.*` files in the same dir (body + sidecar(s))."""
    art_match = ART_FILENAME_RE.match(body_file.name)
    if not art_match:
        return [body_file]
    uuid_part = art_match.group("uuid")
    return sorted(body_file.parent.glob(f"art-{uuid_part}.*"))


def touch_last_active(project_md: Path) -> None:
    """Set last_active = today on a project.md, preserving any nested blocks."""
    if not project_md.is_file():
        return
    text = project_md.read_text(encoding="utf-8")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    project_md.write_text(
        surgical_update_frontmatter(text, {"last_active": today}),
        encoding="utf-8",
    )


def update_artefact_frontmatter(art_md: Path, updates: dict) -> None:
    """In-place frontmatter update for a Markdown artefact. Surgical — preserves
    nested blocks like `produced_by:` (B1 fix from #92)."""
    text = art_md.read_text(encoding="utf-8")
    art_md.write_text(surgical_update_frontmatter(text, updates), encoding="utf-8")


def update_sidecar(sidecar: Path, updates: dict) -> None:
    """In-place JSON sidecar update for an export artefact's provenance."""
    if not sidecar.is_file():
        return
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data.update(updates)
    sidecar.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def cmd_promote(args, cfg) -> int:
    art_uuid = args.art_uuid
    slug = find_slug(cfg.content_root, args.slug)
    if not slug:
        print(f"no project matching {args.slug!r}", file=sys.stderr)
        return 1

    art_path = find_artefact(cfg.content_root, art_uuid)
    if not art_path:
        print(f"no artefact found with uuid {art_uuid}", file=sys.stderr)
        return 1

    # Refuse if already in a project
    flat_root = cfg.content_root / "artefacts"
    try:
        rel = art_path.relative_to(flat_root)
    except ValueError:
        print(f"artefact {art_uuid} is not flat (already in projects/) — promote requires a flat artefact", file=sys.stderr)
        return 1
    kind = rel.parts[0]
    if kind not in VALID_KINDS:
        print(f"artefact in unknown kind directory: {kind!r}", file=sys.stderr)
        return 1

    dest_dir = project_dir(cfg.content_root, slug) / "artefacts" / kind
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Move all sibling files (body + sidecar(s)).
    moved: list[Path] = []
    for f in sibling_files(art_path):
        dest = dest_dir / f.name
        shutil.move(str(f), str(dest))
        moved.append(dest)

    # Update frontmatter on the body file.
    body_file = next((m for m in moved if m.suffix == ".md"), None)
    if body_file:
        update_artefact_frontmatter(body_file, {"project_id": slug})
    # Update sidecar JSON if export.
    sidecar = next((m for m in moved if m.name.endswith(".provenance.json")), None)
    if sidecar:
        update_sidecar(sidecar, {"project_id": slug})

    touch_last_active(project_dir(cfg.content_root, slug) / "project.md")

    print(f"promoted art-{art_uuid} → {slug}")
    for m in moved:
        print(f"  {m.relative_to(cfg.content_root)}")
    return 0


def cmd_copy_artefact(args, cfg) -> int:
    art_uuid = args.art_uuid
    dest_slug = find_slug(cfg.content_root, args.dest_slug)
    if not dest_slug:
        print(f"no project matching {args.dest_slug!r}", file=sys.stderr)
        return 1

    src = find_artefact(cfg.content_root, art_uuid)
    if not src:
        print(f"no artefact found with uuid {art_uuid}", file=sys.stderr)
        return 1

    # Determine kind from the src path (last segment of artefacts/<kind>/...).
    parts = src.parts
    try:
        idx = parts.index("artefacts")
    except ValueError:
        print(f"src {src} not under an artefacts/ tree", file=sys.stderr)
        return 1
    kind = parts[idx + 1]
    if kind not in VALID_KINDS:
        print(f"unknown kind {kind!r}", file=sys.stderr)
        return 1

    dest_kind_dir = project_dir(cfg.content_root, dest_slug) / "artefacts" / kind
    dest_kind_dir.mkdir(parents=True, exist_ok=True)

    new_uuid = str(uuid.uuid4())
    new_id = f"art-{new_uuid}"

    # Copy each sibling file with renamed prefix.
    src_siblings = sibling_files(src)
    new_paths: list[Path] = []
    for s in src_siblings:
        # Replace the art-<old-uuid> prefix in the filename
        old_prefix = f"art-{art_uuid}"
        if not s.name.startswith(old_prefix):
            continue
        new_name = new_id + s.name[len(old_prefix):]
        dest = dest_kind_dir / new_name
        shutil.copy2(str(s), str(dest))
        new_paths.append(dest)

    # Update frontmatter on the body copy.
    body = next((p for p in new_paths if p.suffix == ".md"), None)
    if body:
        update_artefact_frontmatter(body, {
            "id": new_id, "project_id": dest_slug,
            "derived_from": f"art-{art_uuid}",
        })
    # Update sidecar JSON if export.
    sidecar = next((p for p in new_paths if p.name.endswith(".provenance.json")), None)
    if sidecar:
        update_sidecar(sidecar, {
            "project_id": dest_slug,
            "derived_from": f"art-{art_uuid}",
        })

    touch_last_active(project_dir(cfg.content_root, dest_slug) / "project.md")

    print(f"copied art-{art_uuid} → {new_id} in {dest_slug}")
    for p in new_paths:
        print(f"  {p.relative_to(cfg.content_root)}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="scaffold a new project")
    p_new.add_argument("short_name")
    p_new.add_argument("intent")
    p_new.set_defaults(func=cmd_new)

    p_list = sub.add_parser("list", help="list projects")
    p_list.add_argument("--include-archived", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_resume = sub.add_parser("resume", help="set project as active + print contents")
    p_resume.add_argument("slug_or_shortname")
    p_resume.set_defaults(func=cmd_resume)

    p_archive = sub.add_parser("archive", help="flip status to archived")
    p_archive.add_argument("slug")
    p_archive.set_defaults(func=cmd_archive)

    p_promote = sub.add_parser("promote", help="move flat artefact into project")
    p_promote.add_argument("art_uuid")
    p_promote.add_argument("slug")
    p_promote.set_defaults(func=cmd_promote)

    p_copy = sub.add_parser("copy-artefact", help="copy artefact into another project")
    p_copy.add_argument("art_uuid")
    p_copy.add_argument("dest_slug")
    p_copy.set_defaults(func=cmd_copy_artefact)

    p_clear = sub.add_parser("clear", help="clear active project")
    p_clear.set_defaults(func=cmd_clear)

    p_status = sub.add_parser("status", help="print active project info")
    p_status.set_defaults(func=cmd_status)

    args = p.parse_args(argv)

    cfg = load_config(require_explicit_content_root=True)
    return args.func(args, cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
