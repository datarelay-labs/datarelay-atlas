"""MCP-facing retrieval/context surface (Atlas-owned library contract).

Tool semantics stay here. Streamable HTTP and OAuth resource-server transport
live in ``atlas.mcp_http`` (ADR-0008).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from atlas.provenance import Provenance, ValidationError, provenance_dict
from atlas.retrieval import Retriever, normalize_path
from atlas.security import READ_SCOPE, WRITE_SCOPE, authorize_tool, reject_canonical_mutation

WRITE_TOOL_NAMES = {"create_note"}  # placeholder non-canonical only; no Wiki writes


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any
    error: str | None = None


class AtlasContextTools:
    def __init__(
        self,
        retriever: Retriever | None = None,
        *,
        retriever_factory: Callable[[str], Retriever] | None = None,
    ) -> None:
        if (retriever is None) == (retriever_factory is None):
            raise ValueError("AtlasContextTools requires exactly one retriever source")
        self.retriever = retriever
        self._retriever_factory = retriever_factory

    def _retriever_for(self, project_id: str) -> Retriever:
        if self._retriever_factory is not None:
            return self._retriever_factory(project_id)
        assert self.retriever is not None
        return self.retriever

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

        if tool_name in {"search_project", "get_provenance"} and READ_SCOPE not in scopes:
            return ToolResult(ok=False, data=None, error="unauthorized")

        if tool_name == "search_project":
            try:
                project_id = _required_text(args, "project_id")
                query = _required_text(args, "query")
                limit = _positive_limit(args.get("limit", 8))
                retriever = self._retriever_for(project_id)
                hits = retriever.search(project_id, query, limit=limit)
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(
                ok=True,
                data=[
                    {
                        "project_id": h.project_id,
                        "path": h.path,
                        "identity": h.identity,
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
            try:
                project_id = _required_text(args, "project_id")
                identity = _optional_text(args, "identity")
                path = _optional_text(args, "path")
                if not identity and not path:
                    raise ValidationError("identity or path is required")
                retriever = self._retriever_for(project_id)
                found = _lookup_provenance(
                    retriever,
                    project_id,
                    identity=identity,
                    path=path,
                )
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            if isinstance(found, str):
                return ToolResult(ok=False, data=None, error=found)
            return ToolResult(ok=True, data=dict(provenance_dict(found)))

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


def _required_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{key} is required")
    return value


def _optional_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{key} is required")
    return value.strip()


def _positive_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("limit must be a positive integer")
    return value


def _lookup_provenance(
    retriever: Retriever,
    project_id: str,
    *,
    identity: str,
    path: str,
) -> Provenance | str:
    """Resolve provenance without collapsing projections that share a source path.

    ``identity`` is the projection key (``source_id@ref``). ``path`` is the
    user-visible source path. A shared source path is ambiguous unless the
    caller passes ``identity``.
    """
    table = retriever.provenance_by_path
    if identity:
        found = table.get((project_id, identity))
        return found if found is not None else "not_found"
    # Path lookup compares source_path only. The table key is source_id@ref and
    # must not satisfy a path that happens to equal another projection's identity.
    wanted = normalize_path(path)
    matches = [
        prov
        for (pid, _key), prov in table.items()
        if pid == project_id and normalize_path(prov.source_path) == wanted
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "ambiguous"
    return "not_found"
