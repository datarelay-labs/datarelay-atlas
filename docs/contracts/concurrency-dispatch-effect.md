# Concurrency One-shot Multi-node Dispatch Effect v1

This contract consumes only a current exact-plan multi-node dispatch authorization. The effect boundary is provider-neutral: a caller must inject a dispatch port, and Atlas calls that port exactly once for each authorized assignment.

Before any external call, Atlas validates current authorization binding and the trusted authorization digest, then records an IN_PROGRESS reservation keyed by effect id and authorization digest. This reservation blocks duplicate effects and also prevents unsafe replay after a process crash where some assignments may already have been dispatched.

Each effect call carries the exact authorized node repository/issue/branch/HEAD and slot/worker/provider/runtime/route/evidence attribution plus a deterministic per-node replay key. Atlas never substitutes another route, slot, worker, or provider and has no automatic retry loop.

Per-assignment outcomes are DISPATCHED, REFUSED, HUMAN_REQUIRED, or ERROR. The aggregate receipt is DISPATCHED only when every assignment dispatched, HUMAN_REQUIRED when any assignment requires a human, PARTIAL when at least one assignment dispatched and another did not, otherwise FAILED.

The receipt authority is DISPATCH_EFFECT_RECEIPT_ONLY. It has no join authority and no PASS authority. Completion and reconciliation remain separate measured concurrency run evidence.

The local effect ledger is runtime controller state whose replay reservations must survive backup and restore. It is therefore preserved with Atlas controller state rather than treated as a discardable derived cache. Terminal receipts and their digests are revalidated whenever the ledger is read; malformed or tampered state fails closed.
