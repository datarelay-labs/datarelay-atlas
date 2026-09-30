# Provider Capacity Dashboard Snapshot

This contract is a read-only display input for Atlas Provider Capacity Broker visibility.

The runtime file is `<data-root>/provider-capacity-snapshot.json`.

The snapshot contains planning inputs only:
- observation timestamp;
- required capability;
- broker strategy;
- trusted maximum evidence age policy used by the display projection;
- validated provider route candidates.

It does **not** store a selected route or fallback order. Atlas recomputes the advisory broker plan from the candidates using the existing Provider Capacity Broker.
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

The CLI publish command validates 1–32 provider-route candidate files, recomputes the broker plan before publication, and atomically writes only the derived local snapshot. The snapshot is a rebuildable display cache and is excluded from durable Atlas backups.

Web and MCP surfaces remain read-only and all surfaces use the same normalized provider dashboard model.
