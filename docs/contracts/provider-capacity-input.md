# Provider Capacity Input v1

Status: P0 read-only contract for roadmap #76
Consumer boundary: future Capacity Broker (#55)
Current adapter: Cursor Usage Events CSV

Machine-checkable schema:
[`provider-capacity-input.schema.json`](provider-capacity-input.schema.json)

Example fixture:
[`fixtures/provider-capacity-input.example.json`](fixtures/provider-capacity-input.example.json)

## Purpose

This contract carries provider-authoritative usage/capacity evidence into Atlas
without turning unavailable facts into estimates. It is an input contract, not
a routing, scoring, admission, billing-attribution, or session-control policy.

The first supported scope is `ACCOUNT_AGGREGATE`. Cursor Usage Events CSV
proves aggregate usage events and token counts, but it does not prove remaining
quota, reset time, Cursor Models / Other Models pool state, or active inference
WIP. Those signals therefore remain explicit `UNKNOWN`.
## Contract

Top-level fields:

| Field | Meaning |
|---|---|
| `schema_version` | Integer contract version; v1 is `1`. |
| `kind` | Always `provider_capacity_input`. |
| `provider` | Provider identifier, e.g. `cursor`. |
| `scope` | v1 is `ACCOUNT_AGGREGATE`. |
| `evidence` | Bounded provider-authoritative observation facts. |
| `signals` | Capacity facts, each explicitly `OBSERVED` or `UNKNOWN`. |

Evidence fields:

- `source_kind`: bounded source identifier such as `usage_events_csv`;
- `window_start` / `window_end`: UTC observation range, or null for zero events.
  The broker treats `window_end` as the observation instant for a positive
  `OBSERVED` `remaining_capacity`. A null window cannot prove freshness.
- `event_count`: exact imported provider event count;
- `total_tokens`: exact sum of provider-exposed token parts.

The Cursor adapter uses the existing strict Usage Events CSV parser, including
row bounds, schema checks, exact token-part totals, and fail-closed malformed
input handling.
## Signal semantics

`UNKNOWN` carries only `{"status":"UNKNOWN"}`. It may not contain a guessed
value.

`OBSERVED` is reserved for evidence a provider source actually exposes:

- `remaining_capacity`: decimal string plus unit;
- `reset_at`: canonical UTC timestamp;
- `capacity_pool`: bounded provider pool label;
- `active_inference_wip`: non-negative integer.

The current Cursor Usage Events CSV adapter intentionally emits all four as
`UNKNOWN`.

## Safety and authority

- No prompts, transcripts, tool output, source code, credentials, cookies, or raw CSV rows are retained in this contract.
- Token totals do not imply remaining quota or pool consumption.
- Model labels, Auto selection, `Kind`, `Cost`, and timestamps do not establish pool exhaustion.
- No Work Packet or session billing attribution is made from temporal overlap.
- Resident worker count is not active inference WIP.
- This contract contains no route recommendation, score, provider selection, model selection, CLEAR, YIELD, or destructive action.
- #55 may consume these facts later, but eligibility and routing remain outside this contract.

## CLI

```bash
PYTHONPATH=. python3 -m atlas usage capacity-input --csv usage-events.csv
```

The command emits only the normalized provider-capacity object and performs no
network access or provider mutation.
