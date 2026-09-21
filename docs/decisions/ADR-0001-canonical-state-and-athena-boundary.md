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
4. Athena is historical migration evidence only (ADR-0004). Atlas must not require the Athena repository at build, test, deploy, upgrade, restore, or runtime.
5. The existing Athena PoC remains immutable evidence/reusable implementation input, not the Atlas canonical product repository and not a maintained external dependency.

## Consequences

- Atlas must preserve repository/ref/path/revision provenance.
- Sync can safely replace derived pages because canonical state is elsewhere.
- Athena can be archived after ADR-0004 independence/retirement gates without renaming the product.
- Product-specific features live in Atlas; Engineering System remains a separate canonical repository.
