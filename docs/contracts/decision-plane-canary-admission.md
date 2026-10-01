# Decision Plane Canary Admission v1

This slice adds a bounded admission gate after Decision Plane shadow/replay measurement. It does not activate model decisions.

## Readiness versus admission

Replay-derived readiness answers only whether a decision class has enough verified evidence to accept a canary request. REPLAY_PASS produces CANARY_REQUEST_ELIGIBLE.

Actual canary admission additionally requires exact replay evidence digest binding; explicit project, repository-path-prefix and task-kind scope; a bounded maximum number of canary decisions; explicit evaluation and expiry timestamps; and zero observed false routing in the admitted decision class.

A digest mismatch, non-passing replay assessment, observed false routing, or already-expired request produces CANARY_NOT_ELIGIBLE.

## Authority

Admission authority is CANARY_ADMISSION_ONLY.

It grants no activation authority and no execution authority. Current rollout remains SHADOW; the requested target is recorded as CANARY only for later bounded effect design. Fallback remains CURRENT_DECISION.

Permissions, release/deploy, secrets, exact-head PASS and HUMAN_REQUIRED remain outside Decision Plane authority. Terminal deterministic gates cannot be skipped.

## Staleness

The admission snapshot is derived and rebuildable. When the underlying replay evidence digest changes, the read-only dashboard reports binding_state STALE and the effective decision becomes CANARY_NOT_ELIGIBLE.

Expiry is evaluated from the explicit evaluated_at supplied at admission build time. There is no wall-clock-driven activation in this slice. Any future execution boundary must re-check expiry and evidence binding before an effect.

## Surfaces

- Web UI: /decision-plane
- CLI readiness: atlas decision-plane canary-readiness
- CLI admission read: atlas decision-plane canary-show
- CLI publish: atlas decision-plane canary-publish --request PATH
- MCP readiness: get_decision_canary_readiness
- MCP admission: get_decision_plane_canary
