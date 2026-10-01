# Decision Plane Shadow / Replay v1

This contract implements the first provider-neutral roadmap #56 product slice.

Supported decision classes:
- `OPTIONAL_CONTEXT_SELECTION`
- `FOCUSED_CHECK_SELECTION`

Atlas prepares the candidate set and marks required candidates. A decision-model choice is accepted only as evidence. It never replaces the current deterministic execution choice in this slice.
## Safety boundary

- rollout state is always `SHADOW`;
- activation authority is always `NO_ACTIVATION_AUTHORITY`;
- effective execution choice is always `current_choice_ids`;
- required candidates cannot be omitted by a valid model choice;
- out-of-set or empty model choices are INVALID;
- SHADOW observations cannot claim a model execution outcome or cost/time deltas;
- REPLAY may record verified current/model outcomes and measured deltas;
- permissions, release/deploy, secrets, exact-head PASS and HUMAN_REQUIRED are not supported decision classes;
- terminal CI/release gates cannot be skipped by this contract.

The local ledger is rebuildable experiment evidence and is excluded from Atlas durable backups.
## Replay visibility

Atlas aggregates:
- valid / invalid choices;
- shadow / replay counts;
- verified replay cases;
- current and model verified-success rates;
- cost, wall-time, frontier-call and retry deltas.

Replay is assessed independently for each supported decision class. A class reaches `REPLAY_PASS` only when it has at least five verified replay cases, model verified-success count is not lower than the current-decision count, aggregate cost/wall-time/frontier-call evidence shows at least one improvement, and aggregate retry delta is non-positive. Otherwise the class remains `INSUFFICIENT_EVIDENCE` or becomes `REPLAY_FAIL`.

Even `REPLAY_PASS` is evidence only. Rollout remains `SHADOW` and activation authority remains `NO_ACTIVATION_AUTHORITY`.

## Candidate preparation

Atlas exposes two bounded candidate generators:

- optional-context candidates preserve `AGENTS.md` and `.engineering/project.yaml` as mandatory repository context when present;
- focused-check candidates map changed paths through `.engineering/tests.yaml`, select scenarios triggered by affected domains, and return `release_gate` scenarios separately as `terminal_required_ids` so model selection cannot remove terminal release obligations.

## Surfaces

- Web UI: `/decision-plane`
- CLI read: `python -m atlas decision-plane show`
- CLI append: `python -m atlas decision-plane append --observation <json>`
- authenticated MCP read: `get_decision_plane`

## Canary request readiness

Atlas derives decision_plane_canary_readiness from the same validated replay ledger. REPLAY_PASS makes a class CANARY_REQUEST_ELIGIBLE; other replay states remain CANARY_REQUEST_NOT_ELIGIBLE.

Readiness adds no execution or activation authority. A separate bounded canary request must bind the exact replay evidence digest, project/path/task scope, decision budget and expiry before CANARY_ELIGIBLE can be produced. See decision-plane-canary-admission.md.
