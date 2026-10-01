# Atlas Core Product E2E Contract

Status: Canonical deterministic qualification contract

## Purpose

`python3 scripts/prod-qualification.py operational-e2e --mode core-local` proves that the integrated Atlas Core planes can complete one deterministic, attributable journey on one exact code candidate.

This mode is a **non-production qualification**. It never sets `production_claim=true` and never substitutes component/unit evidence for human-equivalent browser release evidence.

## Required journey

The mode must exercise the shipped Atlas implementations, not parallel test-only product logic:

1. register two projects and synchronize attributable canonical sources;
2. observe Engineering System metadata from a validated projection;
3. perform attributable keyword retrieval and semantic retrieval through the existing `EmbeddingConfig` / injected `EmbeddingClient` contract;
4. perform explicitly scoped cross-project engineering search;
5. prove that Human UI project search and MCP `search_project` / `get_provenance` expose the same source identity and revision;
6. show lifecycle evidence without inventing missing facts and preserve independent `OBSERVED`, `STALE`, and `UNKNOWN` states in one deterministic snapshot;
7. expose bounded derived intelligence including an ADR backlink, explicit contradiction evidence, and a configured-source knowledge gap;
8. reload the durable data root in a fresh Python process and prove attributable retrieval plus derived state survive;
9. backup/restore and upgrade/rollback without losing the validated Core state;
10. prove Surface Reconciliation and Full User E2E remain external browser gates rather than synthesized PASS results.

## Semantic qualification boundary

`core-local` uses a deterministic harness-only embedding client injected through the same semantic retrieval interface used by the product. It does not add a production embedding provider and it does not contact a model or external embedding service.

The test must prove that a semantic-only query can select the intended validated projection while preserving normal provenance.

## Human UI / MCP agreement

The Human UI and MCP comparison is based on the same Atlas durable state and requires exact agreement on projection identity and source revision for the fixture hit.

A missing MCP result, missing UI result, or provenance mismatch is a qualification failure. Neither surface becomes canonical authority.

## Evidence privacy

The qualification result is bounded metadata. It must not emit fixture document bodies, query marker strings, contradiction text, or source revision values. Detailed source material remains in the temporary qualification data root only.

## Release boundary

A `core-local` PASS means the deterministic Core integration journey passed. It does **not** mean:

- production deployment passed;
- a public/remote Human UI is deployed;
- Surface Reconciliation passed;
- Full User E2E passed;
- a release is authorized.

For a user-facing Atlas release, the active Engineering System exact-candidate release contract, actual-browser Surface Reconciliation, and Full User E2E remain separately required.
