#!/usr/bin/env python3
"""Opt-in keyless Parallel MCP public-search PoC. Never installed as Atlas runtime."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas.parallel_public_poc import (  # noqa: E402
    PARALLEL_MCP_URL, PublicMcpError, public_query,
    summarize_run, summarize_search_result, utc_now,
)
from atlas.public_source_benchmark import read_json_file, manifest_questions  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evals/public-sources/questions-v1.json"


async def run_real_public(manifest: dict, limit: int) -> dict:
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    questions = list(manifest_questions(manifest))[:limit]
    results = []
    # Only one low-rate session, exactly one public search tool. No API key,
    # OAuth token, fallback provider or internal input is ever transmitted.
    with anyio.fail_after(min(1300, max(120, limit * 38))):
        async with streamable_http_client(PARALLEL_MCP_URL) as (reader, writer):
            async with ClientSession(
                reader, writer, read_timeout_seconds=35
            ) as client:
                await client.initialize()
                tools = await client.list_tools()
                if {tool.name for tool in tools.tools} != {"web_search"}:
                    raise PublicMcpError("unexpected third-party tool surface")
                consecutive_errors = 0
                for qid in questions:
                    start = time.monotonic()
                    print("PUBLIC_SEARCH", qid, file=sys.stderr, flush=True)
                    try:
                        with anyio.fail_after(35):
                            response = await client.call_tool(
                                "web_search",
                                arguments=public_query(manifest, qid),
                            )
                        duration = min(round((time.monotonic() - start) * 1000), 120000)
                        record = summarize_search_result(
                            manifest, qid,
                            structured=getattr(response, "structured_content", None),
                            latency_ms=duration,
                            retrieved_at=utc_now(),
                            is_error=bool(response.is_error),
                        )
                        consecutive_errors = 0
                    except Exception:
                        # Raw provider errors and metadata may contain untrusted
                        # content. No exception body is persisted or returned.
                        duration = min(round((time.monotonic() - start) * 1000), 120000)
                        record = {
                            "question_id": qid,
                            "provider": "parallel_mcp",
                            "status": "ERROR",
                            "retrieved_at": utc_now(),
                            "latency_ms": duration,
                            "results_count": 0,
                            "official_rank1": False,
                            "official_hit": False,
                            "official_citations": [],
                            "official_citation_sha256": [],
                            "source_content_saved": False,
                            "answer_verified": False,
                            "cost_usd": None,
                            "cash_cost_authority": "UNKNOWN",
                        }
                        consecutive_errors += 1
                    results.append(record)
                    if consecutive_errors >= 3:
                        print("STOP_AFTER_3_CONSECUTIVE_ERRORS", file=sys.stderr)
                        break
                    await anyio.sleep(0.35)
    return {"summary": summarize_run(manifest, results), "records": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute-public-poc", action="store_true",
        help="explicitly send only the committed public 30-question manifest to anonymous Parallel MCP"
    )
    parser.add_argument(
        "--limit", type=int, default=30,
        help="1 to 30 questions. The complete roadmap PoC uses 30"
    )
    args = parser.parse_args()
    try:
        manifest = read_json_file(str(MANIFEST))
        question_count = len(manifest_questions(manifest))
        if not 1 <= args.limit <= question_count:
            raise PublicMcpError("invalid bounded public question limit")
        if args.execute_public_poc:
            import anyio
            report = anyio.run(run_real_public, manifest, args.limit)
        else:
            report = {
                "summary": summarize_run(manifest, []),
                "records": [],
                "network_effect": "NONE",
                "next_action": "Explicit --execute-public-poc required for public-only network demo",
            }
    except Exception as exc:
        # Do not expose raw provider payloads, transport errors, credentials
        # or stack traces in this public evaluation CLI.
        error = str(exc) if isinstance(exc, PublicMcpError) else "public PoC failed or validation unavailable"
        print(json.dumps({"state": "INVALID", "error": error}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
