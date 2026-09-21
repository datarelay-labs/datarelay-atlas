"""MCP-facing retrieval/context surface (Atlas-owned library contract).

Transport/OAuth servers are intentionally out of scope for absorption; this
module defines the tool semantics agents call once a transport exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atlas.provenance import provenance_dict
from atlas.retrieval import Retriever
from atlas.security import READ_SCOPE, WRITE_SCOPE, authorize_tool, reject_canonical_mutation

WRITE_TOOL_NAMES = {"create_note"}  # placeholder non-canonical only; no Wiki writes


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any
    error: str | None = None


class AtlasContextTools:
    def __init__(self, retriever: Retriever) -> None:
        self.retriever = retriever

    def list_tools(self, scopes: list[str]) -> list[dict[str, str]]:
        tools = [
            {
                "name": "search_project",
                "description": "Project-scoped keyword/hybrid retrieval with provenance",
            },
            {
                "name": "get_provenance",
                "description": "Return provenance for a projected path in a project",
            },
        ]
        if authorize_tool("create_note", scopes, write_tools=WRITE_TOOL_NAMES):
            tools.append(
                {
                    "name": "create_note",
                    "description": "Create a derived note only; never mutates GitHub canonical state",
                }
            )
        return tools

    def call(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        scopes: list[str],
    ) -> ToolResult:
        if not authorize_tool(tool_name, scopes, write_tools=WRITE_TOOL_NAMES):
            return ToolResult(ok=False, data=None, error="unauthorized")

        if tool_name == "search_project":
            project_id = str(args.get("project_id", ""))
            query = str(args.get("query", ""))
            limit = int(args.get("limit", 8))
            hits = self.retriever.search(project_id, query, limit=limit)
            return ToolResult(
                ok=True,
                data=[
                    {
                        "project_id": h.project_id,
                        "path": h.path,
                        "title": h.title,
                        "content": h.content,
                        "match": h.match,
                        "score": h.score,
                        "provenance": h.provenance,
                    }
                    for h in hits
                ],
            )

        if tool_name == "get_provenance":
            project_id = str(args.get("project_id", ""))
            path = str(args.get("path", ""))
            prov = self.retriever.provenance_by_path.get((project_id, path))
            if prov is None:
                return ToolResult(ok=False, data=None, error="not_found")
            return ToolResult(ok=True, data=dict(provenance_dict(prov)))

        if tool_name == "create_note":
            reject_canonical_mutation(str(args.get("target", "derived")))
            if WRITE_SCOPE not in scopes:
                return ToolResult(ok=False, data=None, error="unauthorized")
            # Derived-only acknowledgment; durable note store is a later phase.
            return ToolResult(
                ok=True,
                data={
                    "derived": True,
                    "canonical": False,
                    "project_id": args.get("project_id"),
                    "note": args.get("text", ""),
                },
            )

        return ToolResult(ok=False, data=None, error=f"unknown_tool:{tool_name}")


def default_read_scopes() -> list[str]:
    return [READ_SCOPE]
