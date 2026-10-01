"""Read-only Phase 5 operations/release readiness projection."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from atlas.ops import (
    data_root_runtime_ready,
    prod_launch_contract,
    validate_ingress_service_text,
    validate_ingress_socket_text,
    validate_unit_text,
)
from atlas.provenance import ValidationError
from atlas.sbom import validate_sbom_bundle

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


def _mapping(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise ValidationError("PyYAML is required for operations readiness")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValidationError("operations readiness metadata is unavailable") from exc
    if not isinstance(loaded, dict):
        raise ValidationError("operations readiness metadata must be a mapping")
    return loaded


def _bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{field} must be boolean")
    return value


def _command_state(value: object) -> dict[str, object]:
    configured = isinstance(value, str) and bool(value.strip())
    return {
        "state": "CONFIGURED" if configured else "NOT_CONFIGURED",
        "configured": configured,
        "command": value.strip() if configured else None,
    }


def _required_gate(required: bool, command: object) -> dict[str, object]:
    configured = isinstance(command, str) and bool(command.strip())
    if not required:
        state = "NOT_REQUIRED"
    elif configured:
        state = "CONFIGURED"
    else:
        state = "REQUIRED_UNCONFIGURED"
    return {
        "state": state,
        "required": required,
        "configured": configured,
        "command": command.strip() if configured else None,
        "execution": "UNKNOWN",
    }


def _safe_runbook(root: Path, value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value.strip():
        return {"path": "", "state": "INVALID"}
    rel = Path(value)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        return {"path": value, "state": "INVALID"}
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root.resolve()):
        return {"path": value, "state": "INVALID"}
    return {
        "path": value,
        "state": "PRESENT" if candidate.is_file() else "MISSING",
    }


def _dependency_inventory(
    root: Path,
    *,
    sbom_bundle: Path | None = None,
) -> dict[str, object]:
    requirements = root / "requirements.txt"
    third_party = root / "THIRD_PARTY.md"
    dependencies: list[str] = []
    if requirements.is_file():
        for raw in requirements.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                dependencies.append(line)

    sbom_state = "NOT_GENERATED"
    sbom_evidence: dict[str, object] | None = None
    detail = (
        "requirements and third-party records are declarations; "
        "they are not an SBOM"
    )
    if sbom_bundle is not None:
        try:
            sbom_evidence = validate_sbom_bundle(
                Path(sbom_bundle),
                repo_root=root,
                require_current_source=True,
                require_current_runtime=True,
            )
        except ValidationError:
            sbom_state = "INVALID_EVIDENCE"
            detail = (
                "explicit SBOM evidence is invalid, stale, or does not "
                "match the current clean source"
            )
        else:
            sbom_state = "VALIDATED_EVIDENCE"
            detail = (
                "explicit CycloneDX SBOM and provenance evidence validate "
                "against the current clean source"
            )

    return {
        "state": "DECLARED" if requirements.is_file() else "UNAVAILABLE",
        "requirements_path": "requirements.txt",
        "declared_dependency_count": len(dependencies),
        "declared_dependencies": dependencies,
        "third_party_path": "THIRD_PARTY.md",
        "third_party_state": "PRESENT" if third_party.is_file() else "MISSING",
        "sbom_state": sbom_state,
        "sbom_evidence": sbom_evidence,
        "detail": detail,
    }


def _deployment_contract(root: Path) -> dict[str, object]:
    units = (
        ("datarelay-atlas.service", validate_unit_text),
        ("datarelay-atlas-ingress.socket", validate_ingress_socket_text),
        ("datarelay-atlas-ingress.service", validate_ingress_service_text),
    )
    rows = []
    all_valid = True
    for name, validator in units:
        path = root / "deploy" / "systemd" / name
        state = "MISSING"
        try:
            text = path.read_text(encoding="utf-8")
            validator(text)
        except (OSError, UnicodeError, ValidationError):
            all_valid = False
            if path.is_file():
                state = "INVALID"
        else:
            state = "CONFIGURED"
        rows.append({"unit": name, "state": state})
    contract = prod_launch_contract()
    return {
        "state": "CONFIGURED" if all_valid else "UNAVAILABLE",
        "production_evidence": bool(contract.get("production_evidence")),
        "contract": contract,
        "units": rows,
    }


def operations_readiness(
    data_root: Path,
    *,
    sbom_bundle: Path | None = None,
) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[1]
    project = _mapping(repo_root / ".engineering" / "project.yaml")
    release = _mapping(repo_root / ".engineering" / "release.yaml")
    operations = project.get("operations")
    if not isinstance(operations, dict):
        raise ValidationError("operations metadata is missing")
    release_blockers = release.get("blockers")
    if not isinstance(release_blockers, dict):
        raise ValidationError("release blockers metadata is missing")

    production_oriented = _bool(
        operations.get("production_oriented"),
        field="operations.production_oriented",
    )
    runbook_required = _bool(
        operations.get("runbook_required"),
        field="operations.runbook_required",
    )
    incident_required = _bool(
        operations.get("incident_response_required"),
        field="operations.incident_response_required",
    )
    persistent_state = _bool(
        operations.get("persistent_state"),
        field="operations.persistent_state",
    )

    raw_runbooks = operations.get("runbook_paths", [])
    if not isinstance(raw_runbooks, list):
        raise ValidationError("operations.runbook_paths must be a list")
    runbooks = [_safe_runbook(repo_root, value) for value in raw_runbooks]

    runtime_ready = data_root_runtime_ready(Path(data_root))
    operation_commands = {
        "health": _command_state(operations.get("health_command")),
        "backup": _command_state(operations.get("backup_command")),
        "restore_test": _command_state(operations.get("restore_test_command")),
        "upgrade": _command_state(operations.get("upgrade_command")),
        "rollback": _command_state(operations.get("rollback_command")),
    }
    deployment = _deployment_contract(repo_root)
    dependencies = _dependency_inventory(
        repo_root,
        sbom_bundle=sbom_bundle,
    )

    public_smoke_required = _bool(
        release.get("public_smoke_required"),
        field="release.public_smoke_required",
    )
    operational_e2e_required = _bool(
        release.get("operational_e2e_required"),
        field="release.operational_e2e_required",
    )
    human_required = _bool(
        release.get("human_equivalent_user_tests_required"),
        field="release.human_equivalent_user_tests_required",
    )
    sbom_required = _bool(release.get("sbom_required"), field="release.sbom_required")
    provenance_required = _bool(
        release.get("provenance_required"),
        field="release.provenance_required",
    )
    artifact_hash_required = _bool(
        release.get("artifact_hash_required"),
        field="release.artifact_hash_required",
    )
    exact_head_required = _bool(
        release.get("exact_head_required"),
        field="release.exact_head_required",
    )

    human = release.get("human_equivalent_user_tests")
    if not isinstance(human, dict):
        human = {}
    surface = human.get("surface_reconciliation")
    full_e2e = human.get("full_user_e2e")
    surface = surface if isinstance(surface, dict) else {}
    full_e2e = full_e2e if isinstance(full_e2e, dict) else {}
    surface_contract = _safe_runbook(repo_root, surface.get("contract"))
    full_e2e_contract = _safe_runbook(repo_root, full_e2e.get("contract"))

    release_gates = {
        "public_smoke": _required_gate(
            public_smoke_required,
            release.get("public_smoke_command"),
        ),
        "operational_e2e": _required_gate(
            operational_e2e_required,
            release.get("operational_e2e_command"),
        ),
        "sbom": _required_gate(sbom_required, release.get("sbom_command")),
        "provenance": _required_gate(
            provenance_required,
            release.get("provenance_command"),
        ),
        "artifact_hash": _required_gate(
            artifact_hash_required,
            release.get("artifact_hash_command"),
        ),
        "surface_reconciliation": {
            "state": "CONFIGURED"
            if human_required and surface.get("mandatory") is True and surface_contract["state"] == "PRESENT"
            else ("NOT_REQUIRED" if not human_required else "REQUIRED_UNCONFIGURED"),
            "required": human_required and surface.get("mandatory") is True,
            "configured": surface_contract["state"] == "PRESENT",
            "contract": surface_contract["path"],
            "contract_state": surface_contract["state"],
            "execution": "UNKNOWN",
        },
        "full_user_e2e": {
            "state": "CONFIGURED"
            if human_required and full_e2e.get("mandatory") is True and full_e2e_contract["state"] == "PRESENT"
            else ("NOT_REQUIRED" if not human_required else "REQUIRED_UNCONFIGURED"),
            "required": human_required and full_e2e.get("mandatory") is True,
            "configured": full_e2e_contract["state"] == "PRESENT",
            "contract": full_e2e_contract["path"],
            "contract_state": full_e2e_contract["state"],
            "execution": "UNKNOWN",
        },
    }

    runbook_state = (
        "CONFIGURED"
        if runbook_required and runbooks and all(row["state"] == "PRESENT" for row in runbooks)
        else ("NOT_REQUIRED" if not runbook_required else "REQUIRED_UNAVAILABLE")
    )
    command_ready = all(value["configured"] for value in operation_commands.values())
    configuration_ready = command_ready and runbook_state == "CONFIGURED" and incident_required
    phase_state = "PRODUCTION_CONFIGURED" if production_oriented else "DEVELOPMENT"

    return {
        "state": phase_state,
        "production_oriented": production_oriented,
        "runtime": {
            "state": "OBSERVED_READY" if runtime_ready else "OBSERVED_NOT_READY",
            "data_root_ready": runtime_ready,
        },
        "deployment": deployment,
        "dependencies": dependencies,
        "operations": {
            "persistent_state": persistent_state,
            "runbook_required": runbook_required,
            "runbook_state": runbook_state,
            "incident_response_required": incident_required,
            "configuration_ready": configuration_ready,
            "commands": operation_commands,
            "runbooks": runbooks,
        },
        "release": {
            "exact_head_required": exact_head_required,
            "full_e2e_passes": release.get("full_e2e_passes"),
            "blockers": dict(release_blockers),
            "gates": release_gates,
        },
        "security_controls": {
            "state": deployment["state"],
            "detail": "systemd service/ingress hardening contract validates in repository" if deployment["state"] == "CONFIGURED" else "deployment security-control contract is unavailable",
        },
        "security_review": {
            "state": "UNKNOWN",
            "detail": "no standalone security-review PASS evidence is modeled by this surface",
        },
        "release_readiness": {
            "state": "NOT_CLAIMED",
            "detail": (
                "production_oriented is false; runtime/configuration observations do not establish production release readiness"
                if not production_oriented
                else "production-oriented configuration alone does not establish release PASS"
            ),
        },
    }
