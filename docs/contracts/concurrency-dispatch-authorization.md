# Concurrency Multi-node Dispatch Authorization v1

This contract sits between provider-neutral concurrency admission and any future worker dispatch effect.

Authorization requires the exact current admission plan digest and an explicit evaluation time. Atlas reloads and revalidates the admission snapshot, then rejects authorization unless the graph is READY, at least two graph-selected nodes are admitted, every selected node has exactly one assignment, no selected node is admission-blocked, nodes/slots/workers are unique, global and project WIP limits remain satisfied, assigned slots and the snapshot remain fresh, and admitted nodes have no overlapping resource ownership.

The output authority is DISPATCH_AUTHORIZATION_ONLY. Dispatch effect authority is NONE. No worker, session, or process is spawned; GitHub is not mutated; providers are not invoked; there is no retry loop or join/final-PASS authority.

A persisted authorization is a rebuildable derived cache. If the current admission plan changes, the dashboard reports STALE.
