"""Security regressions absorbed from PoC hardening (Atlas-owned)."""

from __future__ import annotations

from atlas.provenance import ValidationError, validate_project_id, validate_source_path

READ_SCOPE = "atlas.read"
WRITE_SCOPE = "atlas.write"


def require_project_scope(project_id: str) -> None:
    validate_project_id(project_id)


def assert_safe_source_path(source_path: str) -> None:
    validate_source_path(source_path)


def authorize_tool(tool_name: str, scopes: list[str], *, write_tools: set[str]) -> bool:
    """Return False when a write tool is requested without write scope."""
    if tool_name in write_tools and WRITE_SCOPE not in scopes:
        return False
    if READ_SCOPE not in scopes and WRITE_SCOPE not in scopes:
        return False
    return True


def reject_canonical_mutation(target: str) -> None:
    """Derived layer must never claim to mutate canonical GitHub artifacts."""
    if target in {"github", "canonical", "repository"}:
        raise ValidationError("Atlas must not mutate canonical repository state via derived tools")
