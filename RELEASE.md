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
- **Templates** — everything under `templates/` (routine prompts, launchd plists, vault git hooks per [#253](https://github.com/acardote/personal-assistant-ultra/issues/253)).
- **Method KB** — `kb/glossary.md` (canonical project terms shipped to every user of the skill).
- **KB templates** — everything under `kb-templates/` (scaffolding the user copies into their vault).
- **Method docs** — everything under `docs/` (ADRs, editorial rules, setup, MCP setup, schemas).
- **CI workflows** — everything under `.github/workflows/` (lint-docs, lint-provenance, bruno-close-gate, release-policy).
- **Config example** — `.assistant.local.json.example`.
- **Release artefacts** — `RELEASE.md`, `release-policy.yaml`, `scripts/check-release-policy.py`.
- **Helpers** — `scripts/pa-session` (per-project session helper, #214 — vault worktree + scaffold + launch wrapper).
- **Claude settings** — `.claude/settings.json` (project-level MCP tool allowlist for harvest-routine interactive runs, #216 — allow-only / no-deny / no-defaultMode to preserve scheduled-fire non-regression).

A version increment ships these together as a unit. `release-policy.yaml` is the machine-readable mirror of this list, validated by CI on every push and PR — drift between the two documents fails the build.

## Vault desync recovery runbook (per [#249](https://github.com/acardote/personal-assistant-ultra/issues/249))

The May-28 desync class: `refs/heads/main` advances behind a frozen working tree, the next `git merge`-class operation captures the gap as staged "deletions" via `.git/AUTO_MERGE`, and the operator sees a working tree apparently gutted of files that exist in HEAD.

### How to tell you're in this state

Any of:

- `pa-session doctor` reports `FAIL: vault desync detected`.
- `git status` in the vault shows many staged-as-deleted files you didn't `git rm`.
- `.git/AUTO_MERGE` exists alongside no `.git/MERGE_HEAD`.
- A `pre-commit` hook (installed by `pa-session doctor`) refuses a commit with `[pa-vault pre-commit] REFUSED: vault is in the desync class (#249)`.
- `tools/vault-desync-probe.py <vault-path>` exits non-zero.

### Recovery

```bash
tools/vault-desync-recover.py <vault-path>      # interactive, asks before mutating
tools/vault-desync-recover.py <vault-path> --yes  # for scripts (CI / automation)
tools/vault-desync-recover.py <vault-path> --dry-run  # show plan, no changes
```

The recovery:
1. Probes the vault. On clean → `nothing to recover` (idempotent and safe).
2. Restores HEAD-tracked files absent from the working tree.
3. Skips D-set paths that hold operator-authored working-tree content (preserved for manual resolution).
4. Removes `.git/AUTO_MERGE`. Does NOT touch `MERGE_HEAD` (that signals a legitimate in-flight merge).
5. Re-probes. If still firing → exit 1 with diagnostic; manual investigation needed.

User-uncommitted edits (staged modifications, unstaged modifications, untracked files) are NOT touched.

### Worked example: 2026-05-28 incident

State observed:
- 233 staged "deletions" against `HEAD = 45ffd5e` (the agentic-org-doctor merge).
- 13 staged "modifications" — turned out to be byte-identical to a prior commit `15a42b8` (the May 25 frozen base).
- `.git/AUTO_MERGE` present, `.git/MERGE_HEAD` absent.
- HEAD reflog had a 3-day gap; `refs/heads/main` reflog showed two ref mutations that bypassed the HEAD-aware path.

Recovery executed:
1. `git diff --cached --diff-filter=D --name-only HEAD | xargs -0 git checkout HEAD --` (manual equivalent of recover step 2 — the recovery tool didn't exist yet).
2. `rm .git/AUTO_MERGE`.
3. Working tree restored to HEAD's state. The 13 "stale modifications" turned out to be artifactual (no real local content to preserve) — confirmed by `git diff <base> -- <path>` returning empty for all 8.

If the recovery tool had existed: `tools/vault-desync-recover.py /Users/acardote/Projects/acardote-pa-vault --yes`.

### Bypass

The vault `pre-commit` hook can be bypassed for legitimate edge cases (e.g., committing a known-safe change while the desync is being repaired):

```bash
PA_VAULT_HOOK_DISABLE=1 git commit -m "..."
```

The bypass emits a banner to stderr (`[pa-vault pre-commit] BYPASS: PA_VAULT_HOOK_DISABLE=1 — desync guard skipped.`) so the override is visible in logs and to post-hoc auditors. Use sparingly; the probe-based preflights in `pa-session`, `live-commit-push.sh`, and `scheduled-harvest.py` still cover the canonical write paths.

### Prevention

- `pa-session new` and `pa-session doctor` install the vault pre-commit hook (`templates/git-hooks/pre-commit`) idempotently. Run `pa-session doctor` periodically on a long-lived vault to keep the hook in place.
- Don't mutate `refs/heads/main` from outside the main worktree's HEAD-aware command path. `git branch -f main ...`, `git update-ref refs/heads/main ...`, and `gh pr merge` + manual sync are all candidate vectors (per #250's "unrecoverable verdict" the specific command remains unidentified, but the class is "ref mutation bypassing the working tree update").

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

**`v0.3.0` is the first tagged release.** The release-policy machinery (this document, `release-policy.yaml`, `scripts/check-release-policy.py`, `.github/workflows/release-policy.yml`) ships in the v0.3.0 tree itself, so it's the earliest commit where running `scripts/check-release-policy.py` against the tagged tree exits cleanly.

Pre-policy history (annotation only — not tags):

| Milestone | Parent | Notes |
|---|---|---|
| Three-layer memory + harvest + slash command | [#1](https://github.com/acardote/personal-assistant-ultra/issues/1) | The first state where a user could clone, configure a vault, and run the full read-side flow. Predates the policy machinery; not tagged. |
| Agent-output capture | [#76](https://github.com/acardote/personal-assistant-ultra/issues/76) | ADR-0003 taxonomy, KB editorial rules, work-execution procedure, lint-provenance with CI. Predates the policy machinery; not tagged. |
| PA projects (v0.3.0 tag) | [#88](https://github.com/acardote/personal-assistant-ultra/issues/88) | ADR-0003 Amendment 1, vault `projects/` tier, `tools/project.py`, project-aware Phase 3 routing, archival sweep, cross-machine resume verification. **First tagged release.** |

If you need to reference a pre-v0.3 commit, use `git log` against the parent issue's reconciliation comment for the corresponding merge SHA. We don't retro-tag because the policy says a tag's tree must include the policy bundle, and pre-v0.3 trees don't.

`latest` will initially point at `v0.3.0` once [#113](https://github.com/acardote/personal-assistant-ultra/issues/113) fires. Follow-up patch-level work (`#87`, `#98`, `#99`) closed after the v0.3 baseline; the next tag will be either `v0.3.1` (if cut as a hotfix) or roll into `v0.4.0` (if folded into the next MINOR's release notes).
