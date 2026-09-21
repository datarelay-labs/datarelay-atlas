# Athena Engineering Knowledge PoC Evidence Snapshot

Captured: 2026-09-21
Source host checkout: `/home/aella/athena-kb-poc`
Source branch: `feature/rick-kb-hardening`
Source HEAD: `f38e20ec4d4ea22c71f1457d9a5361da1e92773a`

## Purpose

This directory preserves non-secret PoC inputs that informed the DataRelay Atlas design. These files are historical evidence, not active production configuration.

## Source inventory

`source-inventory.json` is the previously ignored `.poc-full-sources.json` file used for broad PoC ingestion. It contains 25 source entries:

- 24 Data Relay Link OpenSpec current/archive documents
- 1 DP OS Upgrade architecture document

The refs and repository names in the snapshot reflect the PoC at the time of validation and may now be stale. Atlas must resolve current canonical refs when a project is registered rather than treating this snapshot as live configuration.

## Runtime observation

At capture time the PoC checkout matched the preserved commit above and the compose stack was still running. Athena MCP, indexer, dashboard, and PostgreSQL reported healthy through the PoC status path. The embeddings container reported `unhealthy` because of the known upstream shell healthcheck problem even though the embedding service had been usable during PoC validation.

Do not copy `.env.poc` or any runtime secret into Git.
