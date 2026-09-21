# ADR-0001: Canonical Engineering State and Knowledge Engine Boundary

Status: Accepted
Date: 2026-09-21

## Context

DataRelay Atlas is evolving from an Athena-based engineering knowledge PoC into a product that combines Engineering System methodology, project/lifecycle state, searchable knowledge, and AI context.

Upstream Athena treats Wiki.js as the knowledge truth. Data Relay Labs Engineering System instead treats GitHub and canonical repository artifacts as normative.

## Decision

1. GitHub/repository canonical artifacts remain the source of truth.
2. Atlas knowledge projections and synthesis are derived, attributable, and rebuildable.
3. DataRelay Atlas is the product identity and public architecture boundary.
4. Athena is a replaceable integration/runtime dependency and must not become a required public product concept.
5. The existing Athena PoC is evidence/reusable implementation input, not the Atlas canonical product repository.

## Consequences

- Atlas must preserve repository/ref/path/revision provenance.
- Sync can safely replace derived pages because canonical state is elsewhere.
- Athena can be upgraded, forked minimally, or replaced without renaming the product.
- Product-specific features should live in Atlas unless a change is genuinely generic and belongs upstream.
