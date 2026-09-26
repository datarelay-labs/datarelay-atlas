"""Service configuration and runtime readiness.

Design gate: ADR-0009. Quiesced backup and restore verification are ADR-0010.
Upgrade and rollback schema checks live in atlas.schema_compat. This module
does not copy the data root and does not rewrite schema.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from atlas.mcp_config import (
    BIND_HOST_ENV,
    BIND_PORT_ENV,
    INTROSPECTION_CLIENT_ID_ENV,
    INTROSPECTION_CLIENT_SECRET_ENV,
    INTROSPECTION_CLIENT_SECRET_FILE_ENV,
    INTROSPECTION_URL_ENV,
    ISSUER_URL_ENV,
    McpServeConfig,
    RESOURCE_URL_ENV,
    SECRET_SOURCE_ENV_FILE,
    SECRET_SOURCE_FLAG_FILE,
    TLS_CERT_ENV,
    TLS_KEY_ENV,
    resolve_mcp_serve_config,
)
from atlas.projection import ProjectionStore
from atlas.projection_retrieval import INDEXABLE_SYNC_STATES, build_keyword_retriever
from atlas.provenance import ValidationError
from atlas.registry import REGISTRY_SCHEMA_VERSION
from atlas.semantic_retrieval import EmbeddingConfig, validate_embedding_config
from atlas.work_controller import CONTROLLER_SCHEMA_VERSION

ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
UNIT_NAME = "datarelay-atlas.service"

_REQUIRED_KEYS = (
    "ATLAS_DATA_ROOT",
    RESOURCE_URL_ENV,
    ISSUER_URL_ENV,
    INTROSPECTION_URL_ENV,
    INTROSPECTION_CLIENT_ID_ENV,
    TLS_CERT_ENV,
    TLS_KEY_ENV,
)
_ALLOWED_KEYS = frozenset(
    _REQUIRED_KEYS
    + (
        BIND_HOST_ENV,
        BIND_PORT_ENV,
        INTROSPECTION_CLIENT_SECRET_ENV,
        INTROSPECTION_CLIENT_SECRET_FILE_ENV,
        "GITHUB_TOKEN",
        "ATLAS_EMBEDDING_ENDPOINT",
        "ATLAS_EMBEDDING_MODEL",
        "ATLAS_EMBEDDING_QUERY_PREFIX",
        "ATLAS_EMBEDDING_DOCUMENT_PREFIX",
        "ATLAS_EMBEDDING_TIMEOUT",
    )
)
_SECRET_KEYS = frozenset({INTROSPECTION_CLIENT_SECRET_ENV, "GITHUB_TOKEN"})
_SEMANTIC_KEYS = (
    "ATLAS_EMBEDDING_ENDPOINT",
    "ATLAS_EMBEDDING_MODEL",
    "ATLAS_EMBEDDING_QUERY_PREFIX",
    "ATLAS_EMBEDDING_DOCUMENT_PREFIX",
    "ATLAS_EMBEDDING_TIMEOUT",
)


def unit_source_path() -> Path:
    return Path(__file__).resolve().parents[1] / "deploy" / "systemd" / UNIT_NAME


def stage_unit(dest_dir: Path) -> Path:
    """Copy the systemd unit into ``dest_dir`` without invoking systemd."""
    source = unit_source_path()
    text = source.read_text(encoding="utf-8")
    validate_unit_text(text)
    destination = Path(dest_dir)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / UNIT_NAME
    target.write_text(text, encoding="utf-8")
    os.chmod(target, 0o644)
    return target


def validate_unit_text(text: str) -> None:
    required = (
        "User=atlas\n",
        "Group=atlas\n",
        "EnvironmentFile=/etc/datarelay-atlas/service.env\n",
        "ExecStartPre=/opt/datarelay-atlas/.venv/bin/python -m atlas ops check --env-file /etc/datarelay-atlas/service.env\n",
        "ExecStart=/opt/datarelay-atlas/.venv/bin/python -m atlas mcp serve\n",
        "NoNewPrivileges=true\n",
        "WantedBy=multi-user.target\n",
    )
    missing = [line.strip() for line in required if line not in text]
    if missing or "User=root" in text or "\nUser=0\n" in text:
        raise ValidationError("systemd unit does not match the non-root service contract")


def data_root_runtime_ready(data_root: Path) -> bool:
    """True when the data root can be read by this code.

    The result is a boolean. Callers must not attach file contents, project
    ids, or exception text to an unauthenticated response.
    """
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        return False
    registry = root / "registry.json"
    if registry.exists() and not _registry_file_ready(registry):
        return False
    controller = root / "work-controller.json"
    if controller.exists() and not _controller_file_ready(controller):
        return False
    metadata = root / "projections" / "projections.json"
    if metadata.exists() and not _projection_metadata_ready(metadata):
        return False
    return True


def require_ready_to_bind(
    config: McpServeConfig,
    environ: dict[str, str] | None = None,
) -> None:
    """Refuse to bind when resolved service configuration is not ready.

    This is the serve-path equivalent of ``ops check``. It assesses the
    configuration the process will actually use. The systemd unit also runs
    ``ops check --env-file`` in ``ExecStartPre`` so a world-accessible env file
    or unknown key never reaches this process.
    """
    env = os.environ if environ is None else environ
    source = {
        "ATLAS_DATA_ROOT": str(config.data_root),
        RESOURCE_URL_ENV: config.resource_url,
        ISSUER_URL_ENV: config.issuer_url,
        INTROSPECTION_URL_ENV: config.introspection_url,
        INTROSPECTION_CLIENT_ID_ENV: config.introspection_client_id,
        TLS_CERT_ENV: str(config.tls_cert),
        TLS_KEY_ENV: str(config.tls_key),
    }
    if (
        config.introspection_client_secret_source == SECRET_SOURCE_FLAG_FILE
        and config.introspection_client_secret_file is not None
    ):
        # The explicit flag already won. Do not rebuild a conflict from the
        # ambient inline secret that resolution discarded.
        secret_file = str(config.introspection_client_secret_file)
        inline_secret = ""
    elif (
        config.introspection_client_secret_source == SECRET_SOURCE_ENV_FILE
        and config.introspection_client_secret_file is not None
    ):
        secret_file = str(config.introspection_client_secret_file)
        inline_secret = env.get(INTROSPECTION_CLIENT_SECRET_ENV, "").strip()
    else:
        secret_file = env.get(INTROSPECTION_CLIENT_SECRET_FILE_ENV, "").strip()
        inline_secret = env.get(INTROSPECTION_CLIENT_SECRET_ENV, "").strip()
    if secret_file and inline_secret:
        source[INTROSPECTION_CLIENT_SECRET_FILE_ENV] = secret_file
        source[INTROSPECTION_CLIENT_SECRET_ENV] = inline_secret
    elif secret_file:
        source[INTROSPECTION_CLIENT_SECRET_FILE_ENV] = secret_file
    else:
        source[INTROSPECTION_CLIENT_SECRET_ENV] = config.introspection_client_secret
    report = assess_service_environment(source)
    if report["status"] != "ready":
        raise ValidationError("mcp serve configuration is not ready")


def assess_service_environment(
    environ: dict[str, str] | None = None,
    *,
    env_file: Path | None = None,
) -> dict:
    """Return a secret-free readiness report for one service environment."""
    if env_file is not None:
        path = Path(env_file)
        if not path.is_file() or path.is_symlink():
            raise ValidationError("service env file is missing")
        parsed = _parse_env_file(path)
        source = parsed.values
        file_mode_invalid = _world_accessible(path)
    else:
        parsed = None
        source = dict(os.environ if environ is None else environ)
        file_mode_invalid = False

    missing: list[str] = []
    invalid: list[str] = []
    unknown: list[str] = []
    duplicate: list[str] = []
    if parsed is not None:
        if parsed.syntax_invalid:
            invalid.append("env_syntax")
        unknown.extend(parsed.unknown_keys)
        duplicate.extend(parsed.duplicate_keys)
        if file_mode_invalid:
            invalid.append("env_file_permissions")

    for key in _REQUIRED_KEYS:
        if not source.get(key, "").strip():
            missing.append(key)
    secret = source.get(INTROSPECTION_CLIENT_SECRET_ENV, "").strip()
    secret_file = source.get(INTROSPECTION_CLIENT_SECRET_FILE_ENV, "").strip()
    if secret and secret_file:
        invalid.append("introspection_secret_conflict")
    elif not secret and not secret_file:
        missing.append(INTROSPECTION_CLIENT_SECRET_ENV)

    if secret_file and "introspection_secret_conflict" not in invalid:
        secret_path = Path(secret_file)
        if not secret_path.is_file():
            invalid.append("secret_file_missing")
        elif _world_accessible(secret_path):
            invalid.append("secret_file_permissions")
    key_path = Path(source.get(TLS_KEY_ENV, "").strip() or "/missing-tls-key")
    if source.get(TLS_KEY_ENV, "").strip():
        if not key_path.is_file():
            invalid.append("tls_key_missing")
        elif _world_accessible(key_path):
            invalid.append("tls_key_permissions")
    cert_path = source.get(TLS_CERT_ENV, "").strip()
    if cert_path:
        cert = Path(cert_path)
        if not cert.is_file():
            invalid.append("tls_cert_missing")
        elif _world_writable(cert):
            invalid.append("tls_cert_permissions")

    data_root = source.get("ATLAS_DATA_ROOT", "").strip()
    root_path = Path(data_root) if data_root else None
    if data_root:
        if not root_path.is_absolute() or root_path.is_symlink() or not root_path.is_dir():
            invalid.append("data_root")
        elif _world_accessible(root_path):
            invalid.append("data_root_permissions")

    semantic = _semantic_state(source, invalid)
    github = "present" if source.get("GITHUB_TOKEN", "").strip() else "absent"

    structural = missing or invalid or unknown or duplicate
    if not structural and root_path is not None:
        try:
            resolve_mcp_serve_config(
                data_root=root_path,
                bind_host=None,
                port=None,
                resource_url=None,
                issuer_url=None,
                introspection_url=None,
                introspection_client_id=None,
                introspection_client_secret_file=None,
                tls_cert=None,
                tls_key=None,
                environ=source,
            )
        except ValidationError:
            invalid.append("mcp_configuration")
        else:
            if not data_root_runtime_ready(root_path):
                invalid.append("runtime_not_ready")

    report = {
        "duplicate_keys": duplicate,
        "github_sync_credential": github,
        "invalid": invalid,
        "missing": missing,
        "semantic": semantic,
        "status": "not_ready" if (missing or invalid or unknown or duplicate) else "ready",
        "unknown_keys": unknown,
    }
    return _redact_report(report, source)


def _semantic_state(source: dict[str, str], invalid: list[str]) -> str:
    present = {key: source.get(key, "").strip() for key in _SEMANTIC_KEYS}
    if not any(present.values()):
        return "disabled"
    endpoint = present["ATLAS_EMBEDDING_ENDPOINT"]
    model = present["ATLAS_EMBEDDING_MODEL"]
    if not endpoint or not model:
        invalid.append("semantic_configuration")
        return "configured"
    timeout_text = present["ATLAS_EMBEDDING_TIMEOUT"] or "30"
    try:
        timeout = float(timeout_text)
    except ValueError:
        invalid.append("semantic_configuration")
        return "configured"
    try:
        validate_embedding_config(
            EmbeddingConfig(
                endpoint=endpoint,
                model=model,
                query_prefix=present["ATLAS_EMBEDDING_QUERY_PREFIX"],
                document_prefix=present["ATLAS_EMBEDDING_DOCUMENT_PREFIX"],
                timeout_seconds=timeout,
            )
        )
    except ValidationError:
        invalid.append("semantic_configuration")
    return "configured"


def _redact_report(report: dict, source: dict[str, str]) -> dict:
    encoded = json.dumps(report, sort_keys=True)
    for key in _SECRET_KEYS:
        value = source.get(key, "").strip()
        if len(value) >= 4 and value in encoded:
            return {
                "duplicate_keys": [],
                "github_sync_credential": "absent",
                "invalid": ["report_redacted"],
                "missing": [],
                "semantic": "disabled",
                "status": "not_ready",
                "unknown_keys": [],
            }
    return report


class _ParsedEnv:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.unknown_keys: list[str] = []
        self.duplicate_keys: list[str] = []
        self.syntax_invalid = False


def _parse_env_file(path: Path) -> _ParsedEnv:
    parsed = _ParsedEnv()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError("service env file is unreadable") from exc
    if "\x00" in text:
        parsed.syntax_invalid = True
        return parsed
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            parsed.syntax_invalid = True
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_RE.fullmatch(key):
            parsed.syntax_invalid = True
            continue
        if key in parsed.values:
            parsed.duplicate_keys.append(key)
        parsed.values[key] = value.strip()
        if key not in _ALLOWED_KEYS:
            parsed.unknown_keys.append(key)
    parsed.duplicate_keys = sorted(set(parsed.duplicate_keys))
    parsed.unknown_keys = sorted(set(parsed.unknown_keys))
    return parsed


def _world_accessible(path: Path) -> bool:
    mode = path.stat().st_mode
    return bool(mode & (stat.S_IRWXO))


def _world_writable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IWOTH)


def _registry_file_ready(path: Path) -> bool:
    data = _read_json_object(path)
    if data is None:
        return False
    return (
        data.get("schema_version") == REGISTRY_SCHEMA_VERSION
        and isinstance(data.get("projects"), dict)
    )


def _controller_file_ready(path: Path) -> bool:
    data = _read_json_object(path)
    if data is None:
        return False
    return (
        data.get("schema_version") == CONTROLLER_SCHEMA_VERSION
        and isinstance(data.get("workstreams"), dict)
    )


def _projection_metadata_ready(path: Path) -> bool:
    """True when indexable records are acceptable to the serving retriever.

    Non-indexable records are ignored, matching ``build_keyword_retriever``.
    A projections object alone is not enough: malformed indexable records,
    missing documents, and digest mismatches are not ready.
    """
    data = _read_json_object(path)
    if data is None or not isinstance(data.get("projections"), dict):
        return False
    project_ids: set[str] = set()
    for meta in data["projections"].values():
        if not isinstance(meta, dict):
            return False
        if meta.get("sync_state") not in INDEXABLE_SYNC_STATES:
            continue
        project_id = meta.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            return False
        project_ids.add(project_id)
    if not project_ids:
        return True
    store = ProjectionStore(path.parent)
    try:
        for project_id in sorted(project_ids):
            build_keyword_retriever(store, project_id)
    except ValidationError:
        return False
    return True


def _read_json_object(path: Path) -> dict | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data
