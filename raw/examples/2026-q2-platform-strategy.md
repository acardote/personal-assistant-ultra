# Platform 2026 Q2 strategy review (synthetic example)

> This is a **synthetic worked example** for the memory-object schema. It is not a real strategy document. It demonstrates the kind of artifact the assistant's compression pipeline ([#3](https://github.com/acardote/personal-assistant-ultra/issues/3)) will receive as input — a long-form planning doc with a mix of decisions, framing, and operational detail. The corresponding memory object lives at `memory/examples/2026-q2-platform-strategy.md` and links back here via `source_uri`.

Owner: synthetic / acardote
Reviewers: synthetic team
Decision window: closes 2026-04-30
Status: **decided**

## Context

Through Q1 2026, the platform team observed a pattern across enterprise pilots: integration cost (custom auth flows, custom event schemas, custom audit) accounted for 40–60% of pilot cycle time, eclipsing actual product configuration. Pilots that converted to paid (4 of 7) all involved customers willing to absorb that cost; those that did not (3 of 7) cited integration friction as the primary blocker in exit interviews.

This is a recurring pattern. The 2025 H2 retrospective surfaced it but framed it as a sales-engineering staffing problem. With Q1 data, that framing no longer holds: integration cost scales with pilot count, not with engineer count, and we are out of pilots we can absorb at current SE staffing without compromising existing accounts.

## Decision

**Adopt a "thin integration spine" architecture for Q2**: a documented, narrow contract for auth + events + audit that the platform exposes once, and that all enterprise integrations conform to. Customer-side adapters become the integrator's responsibility, not platform engineering's.

Concretely:
- Auth: OIDC + SCIM. No custom SAML attribute mappings; if the customer needs them, we publish a documented translation layer they implement on their side.
- Events: a single webhook contract with a stable event vocabulary. Custom event vocabularies — currently a per-customer thing — get sunset over Q2.
- Audit: structured log export to a customer-controlled bucket with a documented schema. No custom dashboards.

## Risks and mitigations

- **Risk**: existing pilots depending on custom event vocabularies churn.
  - **Mitigation**: lock the legacy vocabulary at current state; provide a 90-day migration window with a per-customer translation script (one-time engineering cost capped at 8 person-days per pilot).
- **Risk**: large enterprise customers reject the "customer implements adapters" stance.
  - **Mitigation**: enterprise-tier (>$500K ARR) retains a 10-day-per-quarter SE adapter budget. Below that tier, no exception.
- **Risk**: the spine itself ossifies and we lose flexibility.
  - **Mitigation**: spine versioning is explicit (v1, v2, ...) with a 6-month deprecation window. The spine team owns versioning policy, not pilot teams.

## Out of scope for Q2

- No changes to pricing or contract structure.
- No changes to product configuration UX.
- No retroactive enforcement on the 4 already-converted accounts. They keep custom contracts at current scope and will be migrated opportunistically as renewals come up.

## Operational follow-through

1. Spine v1 contract published by 2026-05-15. Owner: platform-spine team.
2. Migration scripts for the 3 highest-volume legacy event vocabularies by 2026-06-30. Owner: SE team.
3. Pricing-tier-based SE allocation policy documented by 2026-05-31. Owner: revenue ops.
4. Re-baseline pilot conversion rate tracking from 2026-07-01 — comparing against the Q1 baseline (4/7 = 57%) is the falsifier for whether the spine pays off.

## Falsification criteria

The strategy is wrong if any of the following are observed by 2026-09-30:

- Pilot cycle time has not dropped by ≥25% on pilots that adopted spine v1.
- Pilot conversion rate has not stayed at or above the Q1 57% baseline.
- Enterprise churn from existing accounts citing the spine specifically exceeds 1 account.
- SE staffing required to support the pilot pipeline has not decreased.

If any of these fire, revisit the strategy at the 2026 Q3 platform review.
