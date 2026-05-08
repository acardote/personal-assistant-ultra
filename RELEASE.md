# Personal-assistant-ultra release policy

How `acardote/personal-assistant-ultra` ships versions. Adapted from [`bruno-method`'s policy](https://github.com/acardote/bruno-method/blob/main/RELEASE.md) to this repo's reality: one logical artifact (SKILL + tools + templates + docs + CI), no staging environment, no external publish gate, single-author.

## Versioning

`vMAJOR.MINOR.PATCH`. Pre-release: `vMAJOR.MINOR.PATCH-rc.N` (e.g., `v0.4.0-rc.1`).

What each bump means in this repo:

| Bump | Trigger |
|---|---|
| **MAJOR** | A **breaking change to a user's existing vault** that requires migration. Examples: memory-object schema rename, layer-3 KB shape change, `.assistant.local.json` schema bump, removal of a slash command users rely on, change to the `art://<uuid>` resolver semantics, dedup-config schema bump. |
| **MINOR** | An additive enhancement that an existing vault can ignore. Examples: new slash subcommand, new artefact kind, new harvest source, new lint check, new tool with no migration. |
| **PATCH** | A non-contract fix. Examples: bug fix in a tool, doc fix, regex tightening that doesn't change accept/reject behavior on existing data, internal refactor with no external surface change. |

When in doubt, classify up — a misclassified-down release is more disruptive to adopters than a misclassified-up one.

**Schema versions on data files are independent of the project version.** `.assistant.local.json` carries `$schema_version`, `dedup-config.json` carries `$schema_version`, backup manifests carry `schema_version`. A single project release MAY include zero or more schema bumps; each is documented in the release notes for that version.

## Surfaces in scope

A release ships these as a unit. A change to any of them is in scope of the next release:

- **SKILL** — `.claude/skills/personal-assistant/SKILL.md` (the skill body the user invokes).
- **Slash command surface** — `.claude/commands/personal-assistant.md` (operator-task router).
- **Tools** — everything under `tools/` (Python toolchain: `harvest.py`, `compress.py`, `route.py`, `assemble-kb.py`, `lint-provenance.py`, `project.py`, `bootstrap.py`, etc.).
- **Templates** — everything under `templates/` (routine prompts, launchd plists).
- **Method KB** — `kb/glossary.md` (canonical project terms shipped to every user of the skill).
- **KB templates** — everything under `kb-templates/` (scaffolding the user copies into their vault).
- **Method docs** — everything under `docs/` (ADRs, editorial rules, setup, MCP setup, schemas).
- **CI workflows** — everything under `.github/workflows/` (lint-docs, lint-provenance, bruno-close-gate, release-policy).
- **Config example** — `.assistant.local.json.example`.
- **Release artefacts** — `RELEASE.md`, `release-policy.yaml`, `scripts/check-release-policy.py`.

A version increment ships these together as a unit. `release-policy.yaml` is the machine-readable mirror of this list, validated by CI on every push and PR — drift between the two documents fails the build.

## The `latest` tag

A moving tag named `latest` always points at the most recent immutable release tag. It's force-updated on every release. Same convention as Docker images and bruno-method.

Adopters pick:

| What you want | How |
|---|---|
| Always-current stable | `git clone --branch latest <url>` (re-fetch tags + checkout `latest` to upgrade). |
| Exact reproducibility | `git clone --branch v0.3.0 <url>` (or any other immutable tag). |

`latest` and immutable version tags coexist — the `latest` mechanic doesn't remove or alter `vX.Y.Z` tags.

## Publish flow

For a normal release on the development line (`main`):

1. **Decide bump**: MAJOR / MINOR / PATCH per the table above. If unsure, classify up.
2. **Verify reconciled state**: no open in-flight Bruno child issues that belong to the next release (`gh issue list --label child --state open` returns nothing material). Address or accept any latent open work first.
3. **Verify lints clean**: `tools/lint-docs.py`, `tools/lint-provenance.py --require-vault`, `tests/test_lint_provenance_acceptance.py`, `tests/test_project_acceptance.py`, `scripts/check-release-policy.py` all pass.
4. **Tag the immutable version**:
   ```bash
   git tag v0.X.Y
   git push origin v0.X.Y
   ```
5. **Move `latest`**:
   ```bash
   git tag -f latest
   git push -f origin latest
   ```
6. **Update README status line** (housekeeping):
   - Bump the "v0.X shipped" line to reflect the new release.
   - Bump or remove the "v0.X+1 in flight" line as appropriate.
   - Commit, push.
7. **Sanity check**: `git fetch --tags origin` from a fresh clone, `git checkout v0.X.Y`, `tools/bootstrap.py` against a fresh content vault. Should run end-to-end.

For a pre-release (RC) before a MAJOR or MINOR ships, replace step 4's tag with `v0.X.Y-rc.N` and DO NOT update `latest` — RCs are explicit opt-in.

## Hotfix handling

For an urgent fix to the active released MINOR:

1. Fix on `main` (or a `hotfix/*` branch if `main` has moved past the released MINOR). Land via the normal Bruno cycle.
2. Bump PATCH from the released MINOR: `v0.X.Y+1` (e.g., released `v0.3.0` → hotfix `v0.3.1`).
3. Steps 4–7 of the publish flow above.
4. If the hotfix lives on a side branch, cherry-pick or backmerge to `main` so the next normal release inherits it.

## Branch protection

Out of scope of this policy. Currently single-author; no PR protection on `main`. If/when a teammate adopts this repo, file a follow-up parent to enable required reviews + immutable tags via GitHub branch protection rules.

## Initial state

The first three releases are tagged retrospectively at the merge commits that closed each parent's Move 5 reconciliation:

| Tag | Parent | Notes |
|---|---|---|
| `v0.1.0` | [#1](https://github.com/acardote/personal-assistant-ultra/issues/1) | Three-layer memory architecture + harvest pipeline + freshness check + slash command surface. The first state where a user can clone, configure a vault, and run the full read-side flow. |
| `v0.2.0` | [#76](https://github.com/acardote/personal-assistant-ultra/issues/76) | Agent-output capture: ADR-0003 taxonomy, KB editorial rules, work-execution procedure in SKILL.md, lint-provenance with CI enforcement. The system can now produce knowledge + artefacts with verifiable back-references. |
| `v0.3.0` | [#88](https://github.com/acardote/personal-assistant-ultra/issues/88) | PA projects: ADR-0003 Amendment 1, vault `projects/` tier, `tools/project.py` (8 subcommands), project-aware Phase 3 routing, lint extension for `art://`+`project_id`, archival sweep, cross-machine resume verification. Multi-session work containers with start/resume mechanics. |

`latest` initially points at `v0.3.0`. Follow-up patch-level work (`#87`, `#98`, `#99`) closed after the v0.3 baseline; the next tag will be either `v0.3.1` (if cut as a hotfix) or roll into `v0.4.0` (if folded into the next MINOR's release notes).
