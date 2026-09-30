# ADR-0016: Human-equivalent user release testing

Status: Accepted
Date: 2026-09-30

## Context

Conventional deterministic testing remains essential, but it answers a narrower question than a real user release test.

Unit, component, API, contract, generated matrix, and backend E2E tests are strong at proving implementation invariants. They do not fully reproduce:

- the user's actual environment and primary public interface;
- what the user can discover without source-code knowledge;
- the exact order in which a user clicks, types, navigates, reloads, edits, cancels, and confirms;
- state-dependent controls that appear only after creation, failure, role change, or runtime transition;
- cross-page continuity and deep-link context;
- realistic mistakes and the quality of visible recovery guidance;
- whether a browser-visible success corresponds to persisted/effective runtime truth;
- whether real delivery/output occurred;
- whether failure diagnosis and recovery are possible from the public product surface;
- destructive impact, cancellation, and cleanup/orphan truth.

A release process that stops at machine/internal validation can therefore report a technically correct implementation while still missing a broken or incomplete user journey.

Data Relay Link exposed the same class of gap through its CLI product surface: command/function correctness alone was insufficient without Feature ↔ CLI ↔ Scenario reconciliation and real Full User E2E. Data Relay Control applies the equivalent idea to a browser-first product.

## Minimal design gate

1. **Goal** — Preserve the rationale and Atlas lifecycle-intelligence model for human-equivalent user testing when the canonical Engineering System or a stricter project release contract requires it.
2. **Non-goals** — Replacing deterministic CI, making browser automation the only testing method, inventing one universal scenario catalog for every product, or allowing AI judgment to replace exact evidence.
3. **Two gate classes to recognize** — When required by canonical release authority, Surface Reconciliation plus Full User E2E are distinct gates and must not be collapsed into machine qualification.
4. **Actual public surface** — Browser products use a real Chromium/Chrome process; CLI products use the actual public CLI; desktop/mobile products use their supported public UI/runtime surface.
5. **Automation boundary** — Playwright or an equivalent driver may control a real browser. Headless Chromium/Chrome is still a real browser. jsdom/component rendering, static DOM inspection, API-only flows, and CI contract validation are not execution substitutes.
6. **Exact candidate** — Both gates bind to the same exact release candidate and are invalidated by relevant product/public-surface/harness changes.
7. **Evidence** — Retain run identity, candidate identity, user actions, actual outcomes, failure/recovery evidence, and cleanup truth.
8. **Atlas role** — Atlas observes, indexes, explains, and flags lifecycle readiness from canonical repository/GitHub evidence. Atlas does not invent a PASS or execute release authority merely because it knows the policy.
9. **Engineering System authority** — The canonical methodology belongs in `datarelay-labs/engineering-system`; Atlas mirrors/uses that methodology without forking it.
10. **Acceptance** — Atlas roadmap and lifecycle-intelligence design recognize both gates distinctly from machine qualification and do not collapse static contract presence into execution PASS.

## Decision

Engineering System remains the canonical methodology authority. This ADR defines how Atlas stores the rationale and interprets canonical project evidence; it does not impose release policy on other repositories.

When the canonical Engineering System or a stricter project-specific release contract declares human-equivalent testing required, Atlas recognizes and reports this release sequence rather than defining it independently:

```text
exact-head machine qualification
→ Surface Reconciliation
→ Full User E2E
→ owner/manual acceptance when required
→ release authorization
```

### Gate 1 — Surface Reconciliation

Purpose: exhaustive breadth across the current public product surface.

It reconciles:

```text
product capability
→ public page/menu/command/control
→ discoverability
→ valid/invalid state behavior
→ persistence/effective truth
→ real scenario
→ safety/recovery/cleanup disposition
```

For a browser product this means driving a real browser through state-aware surfaces, not only reading source routes or mounting components in jsdom.

Typical defects found here include:

- feature exists but no usable public control exists;
- visible button has no current product capability behind it;
- a control is undiscoverable or only reachable by memorized URL;
- controls differ across empty/populated/error/RBAC states and one state is untested;
- save/toast succeeds but persisted or effective runtime state disagrees;
- API fallback hides a broken browser action;
- destructive action has misleading impact/cancel semantics;
- current product exposes legacy or out-of-scope surfaces.

### Gate 2 — Full User E2E

Purpose: integrated depth across complete user missions.

A real user mission should include the applicable lifecycle:

```text
first use
→ configure/create
→ validate/preview
→ run/deploy
→ prove real outcome
→ daily operation
→ realistic mistake/failure
→ diagnose from public surface
→ recover from public surface
→ prove recovery
→ edit live state
→ stop/start or equivalent lifecycle
→ destructive cleanup
→ zero owned residue
```

Full User E2E includes realistic user mistakes, reload/new-context persistence, failure/recovery, and actual end result. It is not a long API integration test with screenshots added afterward.

## Browser-product execution rule

For browser products:

```text
ACTUAL_BROWSER_PROCESS_REQUIRED=YES
BROWSER_ENGINE=CHROMIUM_OR_CHROME
PLAYWRIGHT_REAL_BROWSER_DRIVER_ALLOWED=YES
HEADLESS_REAL_BROWSER_ALLOWED=YES
JSDOM_COMPONENT_TEST_SUBSTITUTE=NO
API_ONLY_SUBSTITUTE=NO
```

The executor must be able to prove that a real browser process was launched and record the engine/version/mode where practical.

## Relationship to machine tests

Machine tests remain earlier, cheaper gates:

```text
static/unit/component/API/integration/matrix
→ machine release qualification
→ human-equivalent user gates
```

A static CI check may prove that the two contracts exist and are configured. It must never be promoted into execution PASS.

Likewise, a machine Full Regression PASS cannot replace a failed/missing user-surface action.

## Failure semantics

Required applicable scenarios use explicit terminal states such as:

```text
PASS
FAIL
PARTIAL
BLOCKED
NOT_APPLICABLE
```

Mandatory PARTIAL or BLOCKED does not become release PASS.

One finding must not hide unrelated findings: preserve evidence, continue safe independent scenarios, then remediate after the audit is exhausted and cleanup is complete.

## Atlas lifecycle-intelligence implications

Atlas should eventually normalize and surface, when canonical project evidence exposes them:

- whether the project is user-facing;
- primary user surface;
- Surface Reconciliation required/status/run ID/evidence HEAD;
- Full User E2E required/status/run ID/evidence HEAD;
- same-candidate consistency;
- actual-user-surface execution evidence;
- browser engine/version for browser products when recorded;
- unresolved release-blocking findings;
- invalidation when later product/public-surface/harness changes move the candidate.

Atlas must distinguish:

```text
CONTRACT_CONFIGURED
EXECUTION_PASS
EXECUTION_FAIL
EXECUTION_BLOCKED
STALE_DIFFERENT_HEAD
UNKNOWN
```

Contract presence alone is not execution evidence.

## Consequences

Positive:

- catches defects that are invisible to API/component/matrix tests;
- aligns release evidence with real user success;
- improves discoverability, recovery, destructive safety, and end-to-end outcome quality;
- gives Atlas a stronger lifecycle model for user-facing release readiness.

Cost:

- release qualification becomes slower and more operationally involved;
- browser products require a real browser runtime and isolated test environment;
- product-specific scenario contracts must be maintained as public capabilities evolve.

That cost is intentional: these are release gates, not ordinary PR checks.
