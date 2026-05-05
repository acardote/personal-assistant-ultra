---
id: mem-fixture-q3-roadmap-gmail
source_uri: file:./tests/fixtures/dedup/raw/q3-roadmap-gmail.eml
source_kind: gmail_thread
created_at: "2026-04-23T18:30:00Z"
expires_at: "2026-10-23T00:00:00Z"
kind: strategy
tags:
  - q3
  - roadmap
  - badas
  - atlas
  - acko
  - nuro
title: "Q3 roadmap review — decisions summary"
summary: "Q3 priorities locked: BADAS 2.0 in production, Atlas Waymo onboarding, Acko BIS NOC contingency plan, Nuro SOW kickoff."
---

## What was decided / what is true

- Q3 2026 priority order locked: BADAS production stability, Atlas customer onboarding, Acko launch contingency, Nuro pilot delivery.
- BADAS 2.0 launched cleanly on April 16; Naftali takes ownership of API and UI; recurring launch sync wound down.
- Atlas workspaces declared functional; Waymo expansion is now the daily client iteration tool.
- Acko Pico launch BIS NOC certification slipping past April; mid-to-late May is the next realistic window. Plan B contingency design assigned to platform team.
- Nuro SOW signed: 1,000 safety-critical clips at $150 per clip ($150K commit), expansion option to 9,000 additional clips at $80.50 each. First delivery one month from signature.
- IBM workzones partnership demo Tuesday; roles clarified — Nexar owns data pipeline and dashcam APIs, IBM owns UI customizations and permits.

## Why

- Production-spike resourcing on BADAS justifies pulling Naftali in fully; existing API team capacity is loaded.
- Acko BIS NOC delay is the top blocker carry-over from Q2; production slowdown by ODM unless Plan B locks before mid-May.
- Nuro pilot timing is locked to the SOW; first-delivery-in-a-month is a hard external commitment.

## Load-bearing constraints

- BADAS edge latency target <250ms still unmet; gates connected-camera deployment.
- Acko BIS NOC mid-to-late May window is the cutoff before ODM production slowdown.
- Nuro first-delivery deadline: one month from signature.
- IBM workzones demo Tuesday — roles split locked: pipeline+APIs on us, UI+permits on IBM.

## What to remember when this comes up later

- Q3 priorities are 1-2-3-4 in this order; Atlas is item 2 not 3.
- Atlas Waymo scaling is becoming a labor tax; self-serve or service-model shift is on the radar but not yet committed.
- Naftali is the new API+UI owner for BADAS; route operational asks there.
