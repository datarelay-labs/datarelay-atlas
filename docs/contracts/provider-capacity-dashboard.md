# Provider Capacity Dashboard Snapshot

This contract is a read-only display input for Atlas Provider Capacity Broker visibility.

The runtime file is `<data-root>/provider-capacity-snapshot.json`.

The snapshot contains planning inputs only:
- observation timestamp;
- required capability;
- broker strategy;
- trusted maximum evidence age policy used by the display projection;
- optional validated `approved_provider_route_set` copy for configuration visibility;
- validated provider route candidates.

It does **not** store a selected route or fallback order. Atlas recomputes the advisory broker plan from the candidates using the existing Provider Capacity Broker. When an approved route set is present, candidates are rebound through the existing strict route-config binder; provider/runtime/usage-mode/adapter/capability/rank disagreement fails closed.
## Authority boundary

- snapshot authority: derived/display input only;
- broker plan authority: `ADVISORY_ONLY`;
- no provider invocation;
- no credential access;
- no automatic provider/model switch;
- no session/process mutation;
- no transition authorization/effect;
- operational reset/health/latency facts influence display only in this slice and do not change broker eligibility/ranking.

Missing snapshot state is `UNKNOWN`, not an empty healthy provider set. Invalid or secret-bearing snapshots fail closed.
## Product surfaces

- Web UI: `/providers`
- CLI read: `python -m atlas providers show`
- CLI publish: `python -m atlas providers publish --candidate <file> ...`
- authenticated MCP: `get_provider_dashboard`

The CLI publish command validates 1–32 provider-route candidate files, optionally validates/rebinds them to `--route-set`, recomputes the broker plan before publication, and atomically writes only the derived local snapshot. The snapshot is a rebuildable display cache and is excluded from durable Atlas backups.

The dashboard also computes every currently supported broker strategy over the same candidate set for comparison. This is advisory comparison only.

Read-only failover preview is available through:
- Web UI GET form on `/providers`;
- CLI `providers transition-preview`;
- MCP `get_provider_transition_preview`.

Preview reuses the existing provider transition planner. It may return `TRANSITION_RECOMMENDED` or `HUMAN_REQUIRED`; it never grants effect authorization, seals an effect request, or invokes the provider transition effect port.

Web and MCP surfaces remain read-only and all surfaces use the same normalized provider dashboard model.
