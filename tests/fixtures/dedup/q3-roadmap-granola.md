---
id: mem-fixture-q3-roadmap-granola
source_uri: file:./tests/fixtures/dedup/raw/q3-roadmap-granola.md
source_kind: granola_note
created_at: "2026-04-23T16:00:00Z"
expires_at: "2026-10-23T00:00:00Z"
kind: strategy
tags:
  - q3
  - roadmap
  - badas
  - atlas
  - acko
  - nuro
title: "Q3 Roadmap Review — meeting notes"
summary: "Captured Q3 priorities and decisions: BADAS production handoff, Atlas onboarding, Acko contingency, Nuro pilot kickoff."
---

## Discussion notes

### Q3 priorities

- Locked priority order: 1) BADAS production stability, 2) Atlas customer onboarding, 3) Acko launch contingency, 4) Nuro pilot delivery.
- BADAS 2.0 launched April 16 — clean traffic patterns, performance above prior iterations. Naftali takes API+UI ownership; recurring launch sync ends.

### Atlas

- Workspaces shipped functional; Atlas is now the daily iteration tool for client onboarding.
- Waymo expansion accelerating; sustainability concern raised — manual Nexar curation doesn't scale.
- Open: self-serve vs offshore service-model decision for scaling beyond Waymo.

### Acko

- BIS NOC certification still blocked from Indian government; April off the table.
- Realistic re-window: mid-to-late May. ODM (Pico) signaling production slowdown if cert slips further.
- Plan B contingency design assigned to platform team this week.

### Nuro

- SOW signed: pilot launching, $150 per clip on 1,000 clips ($150K), expansion option up to 9,000 additional at $80.50 each (TCV up to ~$874K if fully exercised).
- First delivery committed within 1 month of signature.

### IBM / workzones

- Demo Tuesday. Roles clarified: Nexar owns data pipeline and dashcam APIs; IBM owns UI customizations and permits.
- Investment ask flagged for scaling IBM while maintaining LA work.

## Action items

- Naftali — own BADAS API+UI; first weekly status next Monday.
- Platform team — Acko BIS NOC Plan B by 2026-04-30.
- SE team — first Nuro batch delivery target locked to SOW signature + 30d.

## Decisions (load-bearing)

- BADAS edge latency target <250ms still unmet; deployment gated.
- Q3 priority order locked.
- Atlas self-serve vs service-model is open but tracked.
