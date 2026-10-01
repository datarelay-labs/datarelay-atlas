# Atlas Operations Readiness

This contract defines the read-only Phase 5 operations/release readiness surface exposed by Atlas.

## Truth boundary

The surface reports repository configuration and current local runtime observations. It never executes operational actions and never promotes configuration into release PASS.

States intentionally distinguish:
- `CONFIGURED`: a required repository contract or command exists;
- `OBSERVED_READY` / `OBSERVED_NOT_READY`: current Atlas data-root structural observation;
- `NOT_REQUIRED`: the current release profile does not require the gate;
- `REQUIRED_UNCONFIGURED`: a required gate lacks a valid configured contract;
- `UNKNOWN`: execution or review evidence is not available;
- `NOT_CLAIMED`: Atlas explicitly does not claim production release readiness.
## Exposed dimensions

- `.engineering/project.yaml` operations profile;
- health, backup, restore-test, upgrade, and rollback command configuration;
- declared runbooks and incident-response requirement;
- secret-free prod-atlas deployment contract and repository systemd-unit validation;
- release blockers and exact-HEAD requirement;
- public-smoke, operational-E2E, Surface Reconciliation, and Full User E2E configuration;
- dependency declarations and third-party record presence;
- SBOM, provenance, and artifact-hash policy state;
- standalone security-review truth remains UNKNOWN without explicit evidence.

The dependency declaration is not an SBOM. Atlas reports SBOM state as NOT_GENERATED unless an explicit SBOM bundle is supplied. VALIDATED_EVIDENCE requires the CycloneDX document and provenance evidence to match the current clean source and the current resolved Python runtime. A configured browser contract is not browser execution evidence. A valid systemd unit is not proof that production is deployed.
## Surfaces

- Web UI: `/operations`
- CLI: `python -m atlas ops readiness`
- Authenticated MCP: `get_operations_readiness` with `atlas.read`

Browser and MCP surfaces are read-only. They do not invoke backup, restore, upgrade, rollback, smoke, E2E, SBOM generation, provenance generation, or security-review actions. The operator CLI may generate an explicit SBOM bundle with atlas ops sbom and may bind that bundle into a read-only readiness query.
