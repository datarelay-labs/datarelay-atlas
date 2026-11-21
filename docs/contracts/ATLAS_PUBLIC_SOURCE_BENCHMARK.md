# Atlas public-source research comparison — advisory contract v1

Status: EXPERIMENTAL_OPTIONAL / LOCAL_EVALUATION_ONLY
Parent roadmap: Atlas issue #10, tooling intake 2026-11-22.
No production service, MCP endpoint, persistent state schema or release gate changes.

## Minimal design

**Goal:** assess whether a future approved parallel-MCP public-search provider reduces factual omissions/errors, latency or cost versus a baseline, without disclosing private material or reducing provenance quality.

**Non-goals:** provider execution or installation, credentials/OAuth, arbitrary crawling, unattended web indexing, internal GitHub content, model-based grading, autonomous coding, authoritative source writes, or production activation.

**Contract:** versioned 30-question PUBLIC_ONLY JSON manifest, optional JSONL observations, and a network-free CLI producing only aggregate evidence. All input is untrusted. No local persistent state, migrations or new runtime dependency.

**Security:** actual external A/B requires explicit provider-provenance and privacy/legal review (terms, training, retention, embedding and commercial usage). Send only the manifest's public questions; never send private repo documents, customer logs, private Work Packets or tokens. Results are untrusted and never authorize tool calls, external writes or releases. The submitted PUBLIC_ONLY flag is not an independent guarantee.

**Acceptance:** >=30 identical questions on two separately authorized providers, complete metadata, independently corroborated official-source agreement, wrong/omitted answers, official-domain hits, latency, rate limits, fallbacks, cost and actual operator benefit. Synthetic fixtures are TEST ONLY, not real evidence.

## Dataset and baseline

`evals/public-sources/questions-v1.json` contains 30 public questions in security-advisory, vendor-documentation and API-changelog categories and official hostname constraints. It contains no invented official answer or reference URL.

    python3 scripts/atlas_official_source_eval.py

Expected state: `NO_DATA`, `benchmark_pass: NOT_ESTABLISHED`, with no rates or fabricated cost/time benefit.

## Real observation JSONL

One JSON object per provider/question. Fields are exact:

| Field | Meaning |
| --- | --- |
| `question_id`, `provider` | Match manifest; providers `baseline` and `parallel_mcp` |
| `status` | `SUCCESS`, `TIMEOUT`, `RATE_LIMIT`, `ERROR` |
| `accessed_at` | UTC `Z` timestamp |
| `latency_ms` | Actual nonnegative milliseconds |
| `cost_usd` | Actual decimal charge (string); do not turn unknown cost into zero |
| `result_url` | Representative HTTPS URL or `null`; nonofficial URLs lower official hit rate |
| `fallback_used` | Observed Boolean |
| `public_only` | Literal `true`; a declared restriction, not verification |
| `verdict` | `AGREES`, `CONTRADICTS`, `OMITTED`, `UNVERIFIED` |
| `review` | `null` if unverified; otherwise object with `reviewer_id`, `reviewed_at`, `primary_url`, where primary URL is an official manifest hostname |

No raw prompts, web bodies, tokens, credentials, private errors, or customer-specific values may appear. The evaluator returns neither prompts nor source URLs or reviewer IDs. Independent-review declarations are **not** cryptographically authenticated.

    python3 scripts/atlas_official_source_eval.py --observations /path/to/approved-public-evidence.jsonl

Only local read access is performed, bounded at 1 MiB per file. Errors are generic and fail closed. Incomplete cohorts have no aggregate claimed cost/latency/declared accuracy; complete self-declared cohorts still return `primary_source_agreement_attestation: NOT_ATTESTED` and `benchmark_pass: NOT_ESTABLISHED`.

## Promotion gate

A separately validated, actual 30-question benchmark with independent primary-source review, current provider terms, privacy approval and measured real cost/latency/error/omission/fallback is required before a provider can be selected. There is no automatic winner, connection, credential, plugin installation, indexing, canonical source mutation or production promotion.

Open external dependencies: approved second public search provider; its legal/retention terms; two real comparison runs and independent fact-checking. Until then, evaluation-only means no effect on normal Atlas workflows.
