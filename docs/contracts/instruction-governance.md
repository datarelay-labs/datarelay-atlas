# Model-Aware Instruction Governance v1

Atlas implements roadmap #57 as a governance/evidence layer over the canonical Engineering System. It does not define an independent prompting methodology.

Canonical external inputs for the first slice are:
- Engineering System `ai/AGENT_BASE.md`;
- Engineering System `evals/behavior/scenarios.yaml`.

The operator/coordinator supplies immutable copies plus an Engineering System revision and SHA-256 digests. Atlas verifies the supplied bytes against the profile. Atlas does not claim that it fetched or authenticated GitHub itself in this runtime path.
## Managed surface inventory

Atlas discovers bounded repository-owned surfaces including:
- `AGENTS.md`;
- `.engineering/project.yaml`, tests, and release policy;
- `.cursor/commands/*`, `.cursor/rules/*`, and `.cursorignore` compatibility surfaces;
- AI Work Packet template;
- MCP tool-description code;
- completion hooks;
- skill contract files;
- future repository `prompts/`, `ai/`, and `skills/` files.

Each surface is represented by path, category, byte size, and SHA-256 only. A preflight requires the managed surfaces to be clean so the inventory is attributable to the exact target HEAD.
## Audit identity and duplicate no-op

The audit identity binds:
- target repository and exact HEAD;
- managed-surface inventory digest;
- Engineering System revision and the exact AGENT_BASE / behavior-scenarios digests;
- trigger kind/revision;
- provider/model/profile;
- harness id/revision.

The ledger is a derived local evidence cache and is excluded from durable Atlas backups. Because that local cache is mutable evidence, preflight never lets a stored audit suppress a fresh external behavior evaluation. `DUPLICATE_NOOP` is decided only at record time, after a fresh result has been validated and rebound to the current preflight. The cache retains exactly one fully validated fresh audit; a drifted, corrupt, or multi-entry legacy cache is rebuilt from the fresh result. Read-only routing also fails closed on multi-entry or invalid cache state instead of selecting an audit by mutable timestamps.
## Candidate diff and behavior evidence

Candidate changes contain only:
- managed surface path;
- current/before SHA-256;
- candidate/after SHA-256.

Atlas does not retain candidate prompt text in the governance ledger.

Behavior results are bound to scenario ids from the supplied canonical Engineering System scenario file. Mandatory scenarios must be present.

Outcome normalization:
- any `FAIL` → `REJECTED`;
- any `UNKNOWN` or missing mandatory scenario → `HUMAN_REQUIRED`;
- all mandatory PASS + at least one candidate change → `CANARY_READY`;
- all mandatory PASS + no change → `NO_CHANGE`.

`CANARY_READY` is advisory. It does not edit a file, create a PR, change a default branch, or promote adoption.
## Workflow

Build a profile:

```bash
python -m atlas instruction-governance profile-build \
  --engineering-system-revision <sha> \
  --agent-base AGENT_BASE.md \
  --behavior-scenarios scenarios.yaml \
  --trigger-kind MODEL_CHANGE \
  --trigger-revision <revision> \
  --model-provider <provider> --model-name <model> --model-profile <profile> \
  --harness-id <harness> --harness-revision <revision>
```

Then run `preflight`. If it returns `AUDIT_REQUIRED`, execute the canonical behavior evaluation externally, build any digest-only candidate changes with `candidate-change`, and record the result with `record`.

Read-only product surfaces:
- Web: `/instruction-governance`;
- CLI: `instruction-governance show`;
- MCP: `get_instruction_governance`.
