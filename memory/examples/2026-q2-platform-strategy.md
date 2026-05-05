---
id: mem-2026q2-platform-strategy-0001
source_uri: file:./raw/examples/2026-q2-platform-strategy.md
source_kind: doc
created_at: "2026-05-05T00:00:00Z"
expires_at: "2026-11-05T00:00:00Z"
kind: strategy
tags:
  - platform
  - integration
  - 2026q2
  - enterprise
title: 2026 Q2 platform strategy — thin integration spine
summary: Adopt OIDC+SCIM auth, single webhook event vocabulary, structured audit export; sunset custom integrations except for >$500K ARR enterprise tier.
---

## What was decided

Q2 2026 platform direction is a **thin integration spine**: one documented, narrow contract for auth + events + audit. Customer-side adapters become the integrator's responsibility outside the enterprise tier (>$500K ARR, 10 SE-days/quarter cap).

- Auth: OIDC + SCIM only. Custom SAML attribute mappings move to customer-implemented translation layers.
- Events: single webhook contract, stable vocabulary. Custom event vocabularies sunset over Q2.
- Audit: structured log export to customer-controlled bucket; no custom dashboards.

## Why it was decided

Q1 2026 data showed integration cost was 40–60% of pilot cycle time and was the cited blocker for the 3 of 7 pilots that did not convert. The 2025 H2 retrospective had framed this as an SE staffing problem; with Q1 data, that framing no longer holds — integration cost scales with pilot count, not engineer count.

## Load-bearing constraints

- **Migration window for legacy event vocabularies**: 90 days, with a one-time per-customer migration script capped at 8 engineering-days. Already-converted accounts (4) keep current contracts and migrate at renewal.
- **Spine versioning**: explicit v1 / v2 with 6-month deprecation. Spine team owns policy, pilot teams do not.
- **Out of scope for Q2**: pricing, contract structure, product configuration UX, retroactive enforcement on converted accounts.

## What to remember when this comes up later

- Decision date: 2026-04-30. Decision window has closed; this is a commitment, not a proposal.
- The 4/7 = 57% Q1 conversion baseline is the comparison point for whether the spine paid off. Re-baseline starts 2026-07-01.
- Falsifiers: cycle time hasn't dropped ≥25% on spine-adopting pilots; conversion below Q1 baseline; >1 enterprise churn citing the spine; SE staffing requirement hasn't decreased — by 2026-09-30. If any fire, revisit at Q3 platform review.
- Concrete operational deadlines: spine v1 contract 2026-05-15 (platform-spine team); top-3 migration scripts 2026-06-30 (SE team); SE allocation policy 2026-05-31 (revenue ops).

## Open questions deferred

- How to handle a pilot that requests v2-spine features before v2 ships? Deferred to spine team.
- Whether the >$500K ARR exception threshold is the right line. Revenue ops to revisit at Q3.
