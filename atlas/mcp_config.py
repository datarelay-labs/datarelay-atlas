"""Runtime configuration for the authenticated MCP resource server.

Design gate: ADR-0008. Secrets and TLS material stay in the environment or
operator-supplied files. Nothing in this module reads or writes Git.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from atlas.provenance import ValidationError

RESOURCE_URL_ENV = "ATLAS_MCP_RESOURCE_URL"
ISSUER_URL_ENV = "ATLAS_MCP_ISSUER_URL"
INTROSPECTION_URL_ENV = "ATLAS_MCP_INTROSPECTION_URL"
INTROSPECTION_CLIENT_ID_ENV = "ATLAS_MCP_INTROSPECTION_CLIENT_ID"
INTROSPECTION_CLIENT_SECRET_ENV = "ATLAS_MCP_INTROSPECTION_CLIENT_SECRET"
TLS_CERT_ENV = "ATLAS_MCP_TLS_CERT"
TLS_KEY_ENV = "ATLAS_MCP_TLS_KEY"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


@dataclass(frozen=True)
class McpServeConfig:
    data_root: Path
    bind_host: str
    port: int
    resource_url: str
    issuer_url: str
    introspection_url: str
    introspection_client_id: str
    introspection_client_secret: str
    tls_cert: Path
    tls_key: Path


def resolve_mcp_serve_config(
    *,
    data_root: Path,
    bind_host: str,
    port: int,
    resource_url: str | None,
    issuer_url: str | None,
    introspection_url: str | None,
    introspection_client_id: str | None,
    introspection_client_secret_file: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    environ: dict[str, str] | None = None,
) -> McpServeConfig:
    """Fill missing flags from the environment and fail closed before bind.

    The introspection client secret comes from the environment or from
    ``introspection_client_secret_file``. It is never taken from a raw flag.
    """
    env = os.environ if environ is None else environ
    resolved = {
        "resource_url": _flag_or_env(resource_url, env, RESOURCE_URL_ENV),
        "issuer_url": _flag_or_env(issuer_url, env, ISSUER_URL_ENV),
        "introspection_url": _flag_or_env(introspection_url, env, INTROSPECTION_URL_ENV),
        "introspection_client_id": _flag_or_env(
            introspection_client_id, env, INTROSPECTION_CLIENT_ID_ENV
        ),
        "introspection_client_secret": _resolve_introspection_client_secret(
            introspection_client_secret_file, env
        ),
        "tls_cert": _flag_or_env(tls_cert, env, TLS_CERT_ENV),
        "tls_key": _flag_or_env(tls_key, env, TLS_KEY_ENV),
    }
    missing = [name for name, value in resolved.items() if not value]
    if missing:
        names = ", ".join(missing)
        raise ValidationError(f"mcp serve configuration missing: {names}")

    host = bind_host.strip()
    if not host or "://" in host or "/" in host or " " in host:
        raise ValidationError("mcp bind host must be a hostname or IP address")
    if isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65535:
        raise ValidationError("mcp port must be an integer from 1 to 65535")

    resource_text = resolved["resource_url"].strip()
    resource = _require_https_url(resource_text, label="MCP resource URL")
    if resource.path.rstrip("/") != "/mcp":
        raise ValidationError("MCP resource URL path must be /mcp")
    issuer = _require_issuer_url(resolved["issuer_url"])
    introspection = _require_https_url(
        resolved["introspection_url"].strip(),
        label="introspection URL",
        allow_loopback_http=True,
    )
    cert = _require_file(resolved["tls_cert"], label="TLS certificate")
    key = _require_file(resolved["tls_key"], label="TLS key")
    return McpServeConfig(
        data_root=Path(data_root),
        bind_host=host,
        port=port,
        resource_url=resource_text.rstrip("/"),
        issuer_url=issuer,
        introspection_url=resolved["introspection_url"].strip(),
        introspection_client_id=resolved["introspection_client_id"],
        introspection_client_secret=resolved["introspection_client_secret"],
        tls_cert=cert,
        tls_key=key,
    )


def _resolve_introspection_client_secret(
    secret_file: str | None,
    env: dict[str, str],
) -> str:
    if secret_file is not None and secret_file.strip():
        path = Path(secret_file.strip())
        if not path.is_file():
            raise ValidationError("introspection client secret file is missing")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError("introspection client secret file is unreadable") from exc
        secret = text.rstrip("\r\n")
        if not secret or "\n" in secret or "\r" in secret or "\x00" in secret:
            raise ValidationError("introspection client secret file is invalid")
        return secret
    return env.get(INTROSPECTION_CLIENT_SECRET_ENV, "").strip()


def _flag_or_env(flag: str | None, env: dict[str, str], name: str) -> str:
    if flag is not None and flag.strip():
        return flag.strip()
    return env.get(name, "").strip()


def _require_https_url(value: str, *, label: str, allow_loopback_http: bool = False):
    parsed = urlsplit(value.strip())
    if parsed.username or parsed.password:
        raise ValidationError(f"{label} must not include userinfo")
    if parsed.query or parsed.fragment:
        raise ValidationError(f"{label} must not include a query or fragment")
    host = parsed.hostname or ""
    loopback_http = allow_loopback_http and parsed.scheme == "http" and host in _LOOPBACK_HOSTS
    if parsed.scheme != "https" and not loopback_http:
        raise ValidationError(f"{label} must use https")
    if not host:
        raise ValidationError(f"{label} must include a host")
    return parsed


def _require_issuer_url(value: str) -> str:
    text = value.strip()
    _require_https_url(text, label="issuer URL", allow_loopback_http=True)
    return text.rstrip("/")


def _require_file(value: str, *, label: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise ValidationError(f"{label} file is missing")
    return path
