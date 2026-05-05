---
id: mem-de9fb233-bf21-4d55-ab7a-55c97e106952
source_uri: file:./raw/examples/2026-q2-platform-strategy.md
source_kind: doc
created_at: '2026-05-05T06:58:48Z'
expires_at: '2026-09-30T23:59:59Z'
kind: strategy
tags:
- platform
- q2-2026
- integration
- enterprise
- spine-architecture
title: 'Q2 2026 platform: adopt thin integration spine (auth/events/audit)'
summary: 'Q2 2026 decision to adopt a narrow auth+events+audit spine; customers implement adapters. Falsifier: pilot conversion
  drops below Q1 57% by 2026-09-30.'
---

## What was decided / what is true

- **Decision: adopt "thin integration spine" for Q2 2026.** Status: decided. Decision window closed 2026-04-30.
- **Auth**: OIDC + SCIM only. No custom SAML attribute mappings; customers implement translation layer on their side using published docs.
- **Events**: single webhook contract with stable event vocabulary. Custom per-customer event vocabularies sunset over Q2.
- **Audit**: structured log export to customer-controlled bucket with documented schema. No custom dashboards.
- Customer-side adapters become the integrator's responsibility, not platform engineering's.

## Why

- Q1 2026 data: integration cost (custom auth, custom event schemas, custom audit) was 40–60% of pilot cycle time, eclipsing product configuration.
- Pilot conversion: 4 of 7 (57%) converted to paid; all 4 absorbed integration cost. The 3 non-converters cited integration friction as primary blocker in exit interviews.
- 2025 H2 retro framed this as SE staffing; Q1 data invalidates that framing — integration cost scales with pilot count, not engineer count, and SE is at capacity.

## Load-bearing constraints

- **Legacy vocabulary migration**: 90-day window, capped at **8 person-days per pilot**.
- **Enterprise SE exception**: customers >$500K ARR retain **10 days/quarter** SE adapter budget. Below that tier: no exception.
- **Spine versioning**: explicit (v1, v2, ...) with **6-month deprecation window**. Spine team owns versioning policy, not pilot teams.
- **Already-converted 4 accounts**: keep custom contracts at current scope; migrate opportunistically at renewal. No retroactive enforcement.
- **Out of scope Q2**: pricing/contract changes; product configuration UX changes.

## Deadlines and owners

- 2026-05-15 — Spine v1 contract published. Owner: platform-spine team.
- 2026-05-31 — Pricing-tier-based SE allocation policy documented. Owner: revenue ops.
- 2026-06-30 — Migration scripts for 3 highest-volume legacy event vocabularies. Owner: SE team.
- 2026-07-01 — Re-baseline pilot conversion tracking begins.
- 2026-09-30 — Falsification check date; if fired, revisit at Q3 platform review.

## What to remember when this comes up later

- **Q1 2026 baseline = 4/7 = 57% pilot conversion.** This is the comparison anchor for whether the spine pays off.
- Strategy is **falsified** if by 2026-09-30 any of:
  - Pilot cycle time has not dropped ≥25% on spine-v1 pilots.
  - Pilot conversion rate falls below 57% Q1 baseline.
  - Enterprise churn citing the spine specifically exceeds 1 account.
  - SE staffing required for pilot pipeline has not decreased.
- The "customer implements adapters" stance is the load-bearing bet. Enterprise pushback above the $500K ARR line is anticipated and budgeted; pushback below that line is not negotiable.
- If asked whether to grant a custom auth/event/audit exception: default no, unless customer is >$500K ARR (then 10 SE days/quarter cap applies).

## Open questions deferred

- Pricing or contract restructuring to reflect new integration model: not decided in Q2.
- Product configuration UX changes: not decided in Q2.
- Specific migration sequencing for the 4 already-converted accounts beyond "opportunistic at renewal."
- What happens if spine v1 contract slips past 2026-05-15: not addressed in source.
