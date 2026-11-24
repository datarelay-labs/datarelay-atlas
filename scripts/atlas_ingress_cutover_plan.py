"""Non-executable Atlas ingress cutover order and rollback contract validator.

Validates a fixed reviewed transaction description and shipped systemd units.
It never invokes systemctl, SSH, sudo, writes production files or executes
the plan. PASS is static contract evidence ONLY, not deployment authority.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping

PLAN = Path("docs/contracts/ATLAS_INGRESS_CUTOVER_PLAN.json")
INGRESS_UNIT = Path("deploy/systemd/datarelay-atlas-ingress.service")
INGRESS_SOCKET = Path("deploy/systemd/datarelay-atlas-ingress.socket")
MCP_UNIT = Path("deploy/systemd/datarelay-atlas.service")

REQUIRED_PREFLIGHT = (
    "CURRENT_OPERATIONAL_STATE_READ_ONLY_VERIFIED",
    "IMMUTABLE_TRACKED_MANIFEST_VERIFIED",
    "ORIGINAL_CODE_BACKUP_VERIFIED",
    "ORIGINAL_ENV_BACKUP_VERIFIED",
    "DURABLE_DATA_BACKUP_RESTORE_TEST_PASS",
    "CANDIDATE_ROOT_0755_AND_PARENT_TRAVERSAL_VERIFIED",
    "DYNAMICUSER_SYSTEMD_SANDBOX_EXEC_PASS",
    "CANDIDATE_SERVICE_ENV_ROOT_ATLAS_0640_READY",
    "OPS_CHECK_PROD_PASS",
    "UPGRADE_AND_OLD_CODE_ROLLBACK_COMPATIBLE",
    "PRODUCTION_CHANGE_AUTHORIZED",
)

FORWARD = (
    "STOP_INGRESS_SOCKET",
    "STOP_INGRESS_SERVICE",
    "STOP_ATLAS_WEB",
    "STOP_ATLAS_MCP",
    "SWAP_VERIFIED_CODE",
    "SWAP_VERIFIED_ENV",
    "START_ATLAS_MCP",
    "VERIFY_LOOPBACK_MCP",
    "START_ATLAS_WEB",
    "VERIFY_LOOPBACK_WEB",
    "RESET_FAILED_INGRESS_UNITS",
    "START_INGRESS_SOCKET",
    "START_INGRESS_SERVICE",
    "VERIFY_ALL_FIVE_UNITS",
    "VERIFY_PUBLIC_MCP_SNI",
    "VERIFY_PUBLIC_KEYCLOAK_SNI_AND_ISSUER",
    "VERIFY_LIVE_TRACKED_MANIFEST",
    "VERIFY_STARTUP_PERSISTENCE",
)

ROLLBACK = (
    "STOP_INGRESS_SOCKET",
    "STOP_INGRESS_SERVICE",
    "STOP_ATLAS_WEB",
    "STOP_ATLAS_MCP",
    "RESTORE_ORIGINAL_CODE",
    "RESTORE_ORIGINAL_ENV",
    "START_ATLAS_MCP",
    "VERIFY_LOOPBACK_MCP",
    "START_ATLAS_WEB",
    "VERIFY_LOOPBACK_WEB",
    "RESET_FAILED_INGRESS_UNITS",
    "START_INGRESS_SOCKET",
    "START_INGRESS_SERVICE",
    "VERIFY_ALL_FIVE_UNITS",
    "VERIFY_PUBLIC_MCP_SNI",
    "VERIFY_PUBLIC_KEYCLOAK_SNI_AND_ISSUER",
    "VERIFY_OLD_CODE_IDENTITY",
    "VERIFY_STARTUP_PERSISTENCE",
)

EXPECTED_FIELDS = {
    "contract_version": 1,
    "purpose": "INGRESS_AWARE_CUTOVER_REVIEW_ONLY",
    "source_identity_policy": "EXACT_AUTHORIZED_MAIN_SHA_WITH_TRACKED_MANIFEST",
    "execution": "NOT_EXECUTABLE",
    "failure_policy": "ANY_MANDATORY_FAILURE_ROLLBACK_THEN_REQUIRE_BOTH_PUBLIC_SNI",
    "rollback_failure_state": "INCIDENT_UNRECOVERED_NEVER_PASS",
    "native_mcp_write_acceptance": "OUT_OF_SCOPE_REQUIRES_SEPARATE_REAL_CHATGPT_TEST",
}


class CutoverPlanError(ValueError):
    """A mandatory non-executable production sequencing invariant is absent."""


def _read_file(root: Path, relative: Path) -> str:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise CutoverPlanError("REQUIRED_FILE_UNAVAILABLE:" + str(relative))
    return path.read_text(encoding="utf-8")


def _require_assignment(text: str, key: str, value: str, label: str) -> None:
    # Anchored systemd assignment to avoid matching a comment or a substring.
    regex = r"(?m)^" + re.escape(key) + r"\s*=\s*" + re.escape(value) + r"\s*$"
    if not re.search(regex, text):
        raise CutoverPlanError("UNIT_CONTRACT_MISMATCH:" + label)


def verify_systemd_units(root: Path) -> None:
    ingress = _read_file(root, INGRESS_UNIT)
    socket = _read_file(root, INGRESS_SOCKET)
    mcp = _read_file(root, MCP_UNIT)
    _require_assignment(ingress, "DynamicUser", "yes", "DYNAMICUSER")
    _require_assignment(ingress, "ProtectSystem", "strict", "PROTECT_SYSTEM")
    _require_assignment(ingress, "ProtectHome", "true", "PROTECT_HOME")
    _require_assignment(ingress, "NoNewPrivileges", "true", "NO_NEW_PRIVILEGES")
    _require_assignment(ingress, "Requires", "datarelay-atlas.service", "REQUIRES_ATLAS")
    _require_assignment(ingress, "After", "datarelay-atlas.service keycloak.service", "INGRESS_ORDER")
    _require_assignment(ingress, "Wants", "keycloak.service", "KEYCLOAK_DEPENDENCY")
    _require_assignment(ingress, "Environment", "PYTHONPATH=/opt/datarelay-atlas", "INGRESS_IMPORT_PATH")
    _require_assignment(
        ingress, "ExecStart",
        "/opt/datarelay-atlas/.venv/bin/python -m atlas.sni_ingress",
        "INGRESS_EXECUTABLE",
    )
    _require_assignment(socket, "ListenStream", "0.0.0.0:443", "PUBLIC_PORT")
    _require_assignment(
        socket, "Service", "datarelay-atlas-ingress.service", "SOCKET_ACTIVATION"
    )
    _require_assignment(mcp, "EnvironmentFile", "/etc/datarelay-atlas/service.env", "MCP_ENV")
    _require_assignment(
        mcp, "ExecStartPre",
        "/opt/datarelay-atlas/.venv/bin/python -m atlas ops check --prod --env-file /etc/datarelay-atlas/service.env",
        "PRODUCTION_PREFLIGHT",
    )


def validate_plan(plan: Mapping[str, Any], *, root: Path) -> dict[str, Any]:
    if set(plan) != {*EXPECTED_FIELDS, "required_preflight", "forward", "rollback"}:
        raise CutoverPlanError("PLAN_FIELDS_CHANGED")
    for key, value in EXPECTED_FIELDS.items():
        if type(plan[key]) is not type(value) or plan[key] != value:
            raise CutoverPlanError("PLAN_VALUE_MISMATCH:" + key)
    for key, expected in (
        ("required_preflight", REQUIRED_PREFLIGHT),
        ("forward", FORWARD),
        ("rollback", ROLLBACK),
    ):
        value = plan[key]
        if not isinstance(value, list) or value != list(expected):
            raise CutoverPlanError("PLAN_SEQUENCE_MISMATCH:" + key)
        if len(set(value)) != len(value):
            raise CutoverPlanError("DUPLICATE_STEP:" + key)
    verify_systemd_units(root)
    return {
        "static_cutover_plan": "PASS",
        "forward_steps": len(FORWARD),
        "rollback_steps": len(ROLLBACK),
        "required_preflight_gates": len(REQUIRED_PREFLIGHT),
        "production_effect": "NONE",
        "candidate_manifest_verification": "NOT_EXECUTED",
        "dynamicuser_sandbox_test": "NOT_EXECUTED",
        "rollback_execution": "NOT_EXECUTED",
        "native_mcp_write_acceptance": "NOT_TESTED",
        "release_authority": "NONE",
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["check"]:
        print("usage: atlas_ingress_cutover_plan.py check", file=sys.stderr)
        return 2
    try:
        root = Path.cwd().resolve()
        plan = json.loads(_read_file(root, PLAN))
        if not isinstance(plan, dict):
            raise CutoverPlanError("PLAN_MUST_BE_OBJECT")
        print(json.dumps(validate_plan(plan, root=root), sort_keys=True))
        return 0
    except (CutoverPlanError, ValueError, OSError) as exc:
        reason = str(exc) if isinstance(exc, CutoverPlanError) else type(exc).__name__
        print("ATLAS_STATIC_CUTOVER_PLAN_BLOCKED=" + reason, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
