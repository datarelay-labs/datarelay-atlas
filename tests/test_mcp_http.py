"""Authenticated MCP resource server regressions.

HTTP protocol checks use the SDK app in-process. One test binds the real
``mcp serve`` process with a generated certificate that is not written to Git.
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import httpx2
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from atlas.github_sync import FetchedSource
from atlas.mcp_auth import (
    HttpxIntrospectionTransport,
    IntrospectionFailed,
    IntrospectionRequest,
    Rfc7662TokenVerifier,
)
from atlas.cli import build_parser
from atlas.mcp_config import (
    INTROSPECTION_CLIENT_SECRET_ENV,
    McpServeConfig,
    resolve_mcp_serve_config,
)
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.mcp_http import _transport_security, build_mcp_application
from atlas.provenance import ValidationError
from atlas.security import READ_SCOPE
from atlas.service import AtlasService

ROOT = Path(__file__).resolve().parents[1]
ISSUER = "https://issuer.example"
OTHER_RESOURCE = "https://other.example/mcp"
SECRET = "test-introspection-secret"


class MapVerifier:
    def __init__(self, tokens: dict[str, AccessToken | None]) -> None:
        self.tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        return self.tokens.get(token)


class ScriptedIntrospection:
    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = payloads
        self.requests: list[IntrospectionRequest] = []

    async def exchange(self, request: IntrospectionRequest) -> dict:
        self.requests.append(request)
        payload = self.payloads.get(request.token)
        if payload is None:
            raise IntrospectionFailed("unknown token")
        return payload


def _cert(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    return cert_path, key_path


def _config(directory: Path, *, port: int, resource_url: str) -> McpServeConfig:
    cert_path, key_path = _cert(directory)
    return McpServeConfig(
        data_root=directory / "data",
        bind_host="127.0.0.1",
        port=port,
        resource_url=resource_url,
        issuer_url=ISSUER,
        introspection_url="https://issuer.example/oauth/introspect",
        introspection_client_id="atlas-resource",
        introspection_client_secret=SECRET,
        tls_cert=cert_path,
        tls_key=key_path,
    )


def _seed(data_root: Path) -> None:
    service = AtlasService(data_root)
    service.register_project(project_id="alpha", repository="datarelay-labs/alpha")
    service.register_project(project_id="beta", repository="datarelay-labs/beta")
    service.add_source("alpha", source_id="charter", source_path="docs/charter.md")
    service.add_source("beta", source_id="notes", source_path="docs/notes.md")

    def fetch(source, token):  # noqa: ARG001
        if source.project_id == "alpha":
            return FetchedSource(content="alpha-mcp-quill charter", source_revision="rev-alpha")
        return FetchedSource(content="beta-mcp-quill notes", source_revision="rev-beta")

    service.sync_project("alpha", fetch=fetch)
    service.sync_project("beta", fetch=fetch)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class McpConfigTests(unittest.TestCase):
    def test_missing_configuration_does_not_echo_secret(self):
        with self.assertRaises(ValidationError) as caught:
            resolve_mcp_serve_config(
                data_root=Path("."),
                bind_host="127.0.0.1",
                port=8443,
                resource_url=None,
                issuer_url=None,
                introspection_url=None,
                introspection_client_id=None,
                introspection_client_secret_file=None,
                tls_cert=None,
                tls_key=None,
                environ={INTROSPECTION_CLIENT_SECRET_ENV: SECRET},
            )
        message = str(caught.exception)
        self.assertNotIn(SECRET, message)
        self.assertIn("resource_url", message)
        self.assertIn("tls_key", message)

    def test_resource_url_must_be_https_mcp_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = _cert(Path(tmp))
            with self.assertRaises(ValidationError):
                resolve_mcp_serve_config(
                    data_root=Path(tmp),
                    bind_host="127.0.0.1",
                    port=8443,
                    resource_url="http://127.0.0.1:8443/mcp",
                    issuer_url=ISSUER,
                    introspection_url="https://issuer.example/introspect",
                    introspection_client_id="atlas-resource",
                    introspection_client_secret_file=None,
                    tls_cert=str(cert_path),
                    tls_key=str(key_path),
                    environ={INTROSPECTION_CLIENT_SECRET_ENV: SECRET},
                )

    def test_secret_file_overrides_env_and_argv_secret_is_rejected(self):
        parser = build_parser()
        with self.assertRaises(SystemExit) as caught:
            parser.parse_args(["mcp", "serve", "--introspection-client-secret", SECRET])
        self.assertEqual(caught.exception.code, 2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_path = root / "introspection-secret"
            secret_path.write_text(SECRET + "\n", encoding="utf-8")
            os.chmod(secret_path, 0o640)
            cert_path, key_path = _cert(root)
            config = resolve_mcp_serve_config(
                data_root=root,
                bind_host="127.0.0.1",
                port=8443,
                resource_url="https://127.0.0.1:8443/mcp",
                issuer_url=ISSUER,
                introspection_url="https://issuer.example/introspect",
                introspection_client_id="atlas-resource",
                introspection_client_secret_file=str(secret_path),
                tls_cert=str(cert_path),
                tls_key=str(key_path),
                environ={INTROSPECTION_CLIENT_SECRET_ENV: "env-secret-not-used"},
            )
            self.assertEqual(config.introspection_client_secret, SECRET)
            from_env = resolve_mcp_serve_config(
                data_root=root,
                bind_host=None,
                port=None,
                resource_url=None,
                issuer_url=None,
                introspection_url=None,
                introspection_client_id=None,
                introspection_client_secret_file=None,
                tls_cert=None,
                tls_key=None,
                environ={
                    "ATLAS_MCP_RESOURCE_URL": "https://127.0.0.1:9443/mcp",
                    "ATLAS_MCP_ISSUER_URL": ISSUER,
                    "ATLAS_MCP_INTROSPECTION_URL": "https://issuer.example/introspect",
                    "ATLAS_MCP_INTROSPECTION_CLIENT_ID": "atlas-resource",
                    "ATLAS_MCP_INTROSPECTION_CLIENT_SECRET_FILE": str(secret_path),
                    "ATLAS_MCP_TLS_CERT": str(cert_path),
                    "ATLAS_MCP_TLS_KEY": str(key_path),
                    "ATLAS_MCP_BIND_HOST": "127.0.0.1",
                    "ATLAS_MCP_BIND_PORT": "9443",
                },
            )
            self.assertEqual(from_env.bind_host, "127.0.0.1")
            self.assertEqual(from_env.port, 9443)
            self.assertEqual(from_env.introspection_client_secret, SECRET)
            secret_path.write_text("\n", encoding="utf-8")
            with self.assertRaises(ValidationError) as invalid:
                resolve_mcp_serve_config(
                    data_root=root,
                    bind_host="127.0.0.1",
                    port=8443,
                    resource_url="https://127.0.0.1:8443/mcp",
                    issuer_url=ISSUER,
                    introspection_url="https://issuer.example/introspect",
                    introspection_client_id="atlas-resource",
                    introspection_client_secret_file=str(secret_path),
                    tls_cert=str(cert_path),
                    tls_key=str(key_path),
                    environ={},
                )
            self.assertNotIn(SECRET, str(invalid.exception))
            self.assertIn("invalid", str(invalid.exception))


class TransportSecurityTests(unittest.TestCase):
    def test_default_https_host_is_allowed_without_a_port(self):
        config = McpServeConfig(
            data_root=Path("data"),
            bind_host="127.0.0.1",
            port=8443,
            resource_url="https://mcp.atlas.datarelay.run/mcp",
            issuer_url=ISSUER,
            introspection_url="https://issuer.example/introspect",
            introspection_client_id="atlas-resource",
            introspection_client_secret=SECRET,
            tls_cert=Path("cert.pem"),
            tls_key=Path("key.pem"),
        )
        settings = _transport_security(config)
        self.assertIn("mcp.atlas.datarelay.run", settings.allowed_hosts)
        self.assertIn("mcp.atlas.datarelay.run:*", settings.allowed_hosts)
        self.assertIn("https://mcp.atlas.datarelay.run", settings.allowed_origins)


class IntrospectionVerifierTests(unittest.TestCase):
    def test_active_token_must_match_resource(self):
        resource = "https://atlas.example/mcp"
        scripted = ScriptedIntrospection(
            {
                "good": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": resource,
                    "sub": "owner",
                    "iss": ISSUER,
                },
                "other": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": OTHER_RESOURCE,
                },
                "inactive": {"active": False, "client_id": "chatgpt"},
                "expired": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": resource,
                    "exp": 1,
                },
            }
        )
        verifier = Rfc7662TokenVerifier(
            introspection_url="https://issuer.example/introspect",
            client_id="atlas-resource",
            client_secret=SECRET,
            resource_url=resource,
            issuer_url=ISSUER,
            transport=scripted,
        )

        async def check():
            good = await verifier.verify_token("good")
            other = await verifier.verify_token("other")
            inactive = await verifier.verify_token("inactive")
            expired = await verifier.verify_token("expired")
            return good, other, inactive, expired

        good, other, inactive, expired = asyncio.run(check())
        self.assertIsNotNone(good)
        assert good is not None
        self.assertEqual(good.scopes, ["atlas.read"])
        self.assertEqual(good.resource, resource)
        self.assertNotIn(SECRET, json.dumps(good.model_dump()))
        self.assertIsNone(other)
        self.assertIsNone(inactive)
        self.assertIsNone(expired)
        self.assertEqual(scripted.requests[0].client_secret, SECRET)

    def test_explicit_issuer_and_conflicting_resource_fail_closed(self):
        resource = "https://atlas.example/mcp"
        scripted = ScriptedIntrospection(
            {
                "wrong-issuer": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": resource,
                    "iss": "https://evil.example",
                },
                "omitted-issuer": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": resource,
                },
                "blank-issuer": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": resource,
                    "iss": "   ",
                },
                "empty-issuer": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": resource,
                    "iss": "",
                },
                "non-string-issuer": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": resource,
                    "iss": 1,
                },
                "correct-issuer": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": resource,
                    "iss": ISSUER,
                },
                "conflict": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": resource,
                    "resource": OTHER_RESOURCE,
                    "iss": ISSUER,
                },
                "aud-list": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": [resource, OTHER_RESOURCE],
                    "iss": ISSUER,
                },
                "aud-list-conflict": {
                    "active": True,
                    "client_id": "chatgpt",
                    "scope": "atlas.read",
                    "aud": [OTHER_RESOURCE],
                    "resource": resource,
                    "iss": ISSUER,
                },
            }
        )
        verifier = Rfc7662TokenVerifier(
            introspection_url="https://issuer.example/introspect",
            client_id="atlas-resource",
            client_secret=SECRET,
            resource_url=resource,
            issuer_url=ISSUER,
            transport=scripted,
        )

        async def check():
            return (
                await verifier.verify_token("wrong-issuer"),
                await verifier.verify_token("omitted-issuer"),
                await verifier.verify_token("blank-issuer"),
                await verifier.verify_token("empty-issuer"),
                await verifier.verify_token("non-string-issuer"),
                await verifier.verify_token("correct-issuer"),
                await verifier.verify_token("conflict"),
                await verifier.verify_token("aud-list"),
                await verifier.verify_token("aud-list-conflict"),
            )

        (
            wrong,
            omitted,
            blank,
            empty,
            non_string,
            correct,
            conflict,
            aud_list,
            aud_list_conflict,
        ) = asyncio.run(check())
        self.assertIsNone(wrong)
        self.assertIsNotNone(omitted)
        self.assertIsNone(blank)
        self.assertIsNone(empty)
        self.assertIsNone(non_string)
        self.assertIsNotNone(correct)
        assert correct is not None
        self.assertEqual(correct.claims, {"iss": ISSUER})
        self.assertIsNone(conflict)
        self.assertIsNotNone(aud_list)
        assert aud_list is not None
        self.assertEqual(aud_list.resource, resource)
        self.assertIsNone(aud_list_conflict)

    def test_aud_list_accepts_resource_url_plus_non_url_audience(self):
        resource = "https://mcp.atlas.datarelay.run/mcp"
        scripted = ScriptedIntrospection(
            {
                "prod-shape": {
                    "active": True,
                    "client_id": "atlas-cli",
                    "scope": "atlas.read",
                    "aud": [resource, "atlas-resource"],
                    "iss": ISSUER,
                },
                "identifier-only": {
                    "active": True,
                    "client_id": "atlas-cli",
                    "scope": "atlas.read",
                    "aud": ["atlas-resource"],
                    "iss": ISSUER,
                },
                "wrong-resource-plus-identifier": {
                    "active": True,
                    "client_id": "atlas-cli",
                    "scope": "atlas.read",
                    "aud": [OTHER_RESOURCE, "atlas-resource"],
                    "iss": ISSUER,
                },
                "non-url-string": {
                    "active": True,
                    "client_id": "atlas-cli",
                    "scope": "atlas.read",
                    "aud": "atlas-resource",
                    "iss": ISSUER,
                },
                "conflict-with-extra-audience": {
                    "active": True,
                    "client_id": "atlas-cli",
                    "scope": "atlas.read",
                    "aud": [resource, "atlas-resource"],
                    "resource": OTHER_RESOURCE,
                    "iss": ISSUER,
                },
                "malformed-url": {
                    "active": True,
                    "client_id": "atlas-cli",
                    "scope": "atlas.read",
                    "aud": [resource, "https://"],
                    "iss": ISSUER,
                },
                "malformed-bracket-url": {
                    "active": True,
                    "client_id": "atlas-cli",
                    "scope": "atlas.read",
                    "aud": [resource, "https://["],
                    "iss": ISSUER,
                },
            }
        )
        verifier = Rfc7662TokenVerifier(
            introspection_url="https://issuer.example/introspect",
            client_id="atlas-resource",
            client_secret=SECRET,
            resource_url=resource,
            issuer_url=ISSUER,
            transport=scripted,
        )

        async def check():
            return (
                await verifier.verify_token("prod-shape"),
                await verifier.verify_token("identifier-only"),
                await verifier.verify_token("wrong-resource-plus-identifier"),
                await verifier.verify_token("non-url-string"),
                await verifier.verify_token("conflict-with-extra-audience"),
                await verifier.verify_token("malformed-url"),
                await verifier.verify_token("malformed-bracket-url"),
            )

        accepted, identifier_only, wrong, non_url, conflict, malformed, bracket = asyncio.run(check())
        self.assertIsNotNone(accepted)
        assert accepted is not None
        self.assertEqual(accepted.resource, resource)
        self.assertIsNone(identifier_only)
        self.assertIsNone(wrong)
        self.assertIsNone(non_url)
        self.assertIsNone(conflict)
        self.assertIsNone(malformed)
        self.assertIsNone(bracket)

    def test_http_transport_posts_form_and_does_not_follow_redirects(self):
        hits = {"introspect": 0, "collected": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode()
                self.server.body = body  # type: ignore[attr-defined]
                self.server.authorization = self.headers.get("Authorization")  # type: ignore[attr-defined]
                if self.path.startswith("/redirect"):
                    hits["introspect"] += 1
                    self.send_response(302)
                    location = f"http://127.0.0.1:{self.server.server_address[1]}/collected"
                    self.send_header("Location", location)
                    self.end_headers()
                    return
                hits["collected"] += 1
                self.send_response(204)
                self.end_headers()

            def log_message(self, fmt: str, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            transport = HttpxIntrospectionTransport()

            async def exchange(path: str) -> dict:
                return await transport.exchange(
                    IntrospectionRequest(
                        url=f"http://127.0.0.1:{port}{path}",
                        token="presented-token",
                        client_id="atlas-resource",
                        client_secret=SECRET,
                    )
                )

            with self.assertRaises(IntrospectionFailed):
                asyncio.run(exchange("/redirect"))
            self.assertEqual(hits["introspect"], 1)
            self.assertEqual(hits["collected"], 0)
            self.assertIn("token=presented-token", server.body)  # type: ignore[attr-defined]
            self.assertIn("token_type_hint=access_token", server.body)  # type: ignore[attr-defined]
            self.assertTrue(server.authorization.startswith("Basic "))  # type: ignore[attr-defined]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class McpHttpTests(unittest.TestCase):
    def _app(self, tmp: Path, resource_url: str):
        data = tmp / "data"
        _seed(data)
        config = _config(tmp, port=8443, resource_url=resource_url)
        verifier = MapVerifier(
            {
                "good": AccessToken(
                    token="good",
                    client_id="chatgpt",
                    scopes=[READ_SCOPE],
                    resource=resource_url,
                ),
                "write-only": AccessToken(
                    token="write-only",
                    client_id="chatgpt",
                    scopes=["atlas.write"],
                    resource=resource_url,
                ),
                "other-resource": AccessToken(
                    token="other-resource",
                    client_id="chatgpt",
                    scopes=[READ_SCOPE],
                    resource=OTHER_RESOURCE,
                ),
            }
        )
        return build_mcp_application(AtlasService(data), config, verifier), data

    def test_auth_failures_and_protected_resource_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            resource = "https://127.0.0.1:8443/mcp"
            app, _data = self._app(Path(tmp), resource)
            with TestClient(app, base_url="http://127.0.0.1:8443") as client:
                metadata = client.get("/.well-known/oauth-protected-resource/mcp")
                self.assertEqual(metadata.status_code, 200)
                body = metadata.json()
                self.assertEqual(body["resource"], resource)
                self.assertEqual(body["authorization_servers"], [ISSUER])
                self.assertIn(READ_SCOPE, body["scopes_supported"])
                self.assertEqual(client.get("/authorize").status_code, 404)
                self.assertEqual(client.post("/token").status_code, 404)

                anonymous = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
                self.assertEqual(anonymous.status_code, 401)
                www = anonymous.headers["www-authenticate"]
                self.assertIn("resource_metadata=", www)
                self.assertIn("/.well-known/oauth-protected-resource/mcp", www)

                wrong_scope = client.post(
                    "/mcp",
                    headers={"Authorization": "Bearer write-only"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                )
                self.assertEqual(wrong_scope.status_code, 403)

                wrong_resource = client.post(
                    "/mcp",
                    headers={"Authorization": "Bearer other-resource"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                )
                self.assertEqual(wrong_resource.status_code, 401)

    def test_healthz_reports_readiness_without_canonical_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resource = "https://127.0.0.1:8443/mcp"
            app, data = self._app(root, resource)
            with TestClient(app, base_url="http://127.0.0.1:8443") as client:
                ready = client.get("/healthz")
                self.assertEqual(ready.status_code, 200)
                self.assertEqual(ready.json(), {"status": "ready"})
                body = ready.text
                self.assertNotIn("alpha", body)
                self.assertNotIn("quill", body)
                self.assertNotIn("rev-alpha", body)
                self.assertNotIn(SECRET, body)

                registry = data / "registry.json"
                registry.write_text('{"schema_version": 99, "projects": {}}\n', encoding="utf-8")
                unsupported = client.get("/healthz")
                self.assertEqual(unsupported.status_code, 503)
                self.assertEqual(unsupported.json(), {"status": "not_ready"})
                self.assertNotIn("99", unsupported.text)

                registry.write_text(
                    '{"schema_version": 1, "projects": {}}\n',
                    encoding="utf-8",
                )
                meta = data / "projections" / "projections.json"
                meta.write_text("{", encoding="utf-8")
                corrupt = client.get("/healthz")
                self.assertEqual(corrupt.status_code, 503)
                self.assertEqual(corrupt.json(), {"status": "not_ready"})

    def test_healthz_rejects_projection_digest_mismatch_without_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resource = "https://127.0.0.1:8443/mcp"
            app, data = self._app(root, resource)
            with TestClient(app, base_url="http://127.0.0.1:8443") as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
                meta_path = data / "projections" / "projections.json"
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
                for record in payload["projections"].values():
                    record["content_digest"] = "0" * 64
                meta_path.write_text(json.dumps(payload), encoding="utf-8")
                rejected = client.get("/healthz")
                self.assertEqual(rejected.status_code, 503)
                self.assertEqual(rejected.json(), {"status": "not_ready"})
                self.assertNotIn("0" * 64, rejected.text)
                self.assertNotIn("charter", rejected.text)
                self.assertNotIn("rev-alpha", rejected.text)

    def test_https_client_round_trip_is_project_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            port = _free_port()
            resource = f"https://127.0.0.1:{port}/mcp"
            app, data = self._app(root, resource)
            config = _config(root / "tls", port=port, resource_url=resource)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    ssl_certfile=str(config.tls_cert),
                    ssl_keyfile=str(config.tls_key),
                    log_level="warning",
                )
            )
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            try:
                self._wait_until(lambda: server.started, "MCP HTTPS server did not start")
                payload = asyncio.run(_search(resource, "good", "alpha", "alpha-mcp-quill"))
                self.assertEqual(payload[0]["provenance"]["source_revision"], "rev-alpha")
                self.assertEqual(payload[0]["path"], "docs/charter.md")
                provenance = asyncio.run(
                    _provenance(
                        resource,
                        "good",
                        project_id="alpha",
                        path=payload[0]["path"],
                        identity=payload[0]["identity"],
                    )
                )
                self.assertEqual(provenance["source_revision"], "rev-alpha")
                self.assertEqual(provenance["repository"], "datarelay-labs/alpha")
                empty = asyncio.run(_search(resource, "good", "alpha", "beta-mcp-quill"))
                self.assertEqual(empty, [])

                service = AtlasService(data)
                service.add_source("alpha", source_id="later", source_path="docs/later.md")

                def fetch(source, token):  # noqa: ARG001
                    if source.source_id == "later":
                        return FetchedSource(
                            content="zz-later-only-token",
                            source_revision="rev-later",
                        )
                    return FetchedSource(
                        content="alpha-mcp-quill charter",
                        source_revision="rev-alpha",
                    )

                service.sync_project("alpha", fetch=fetch)
                later = asyncio.run(_search(resource, "good", "alpha", "zz-later-only-token"))
                self.assertEqual(later[0]["identity"], "later@main")
                self.assertEqual(later[0]["provenance"]["source_revision"], "rev-later")
            finally:
                server.should_exit = True
                thread.join(timeout=5)

    def test_factory_sees_projection_added_after_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            service = AtlasService(data)
            service.register_project(project_id="alpha", repository="datarelay-labs/alpha")
            tools = AtlasContextTools(retriever_factory=service.project_retriever)
            service.add_source("alpha", source_id="charter", source_path="docs/charter.md")

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(content="factory-mcp-quill", source_revision="rev-factory")

            service.sync_project("alpha", fetch=fetch)
            result = tools.call(
                "search_project",
                {"project_id": "alpha", "query": "factory-mcp-quill"},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data[0]["provenance"]["source_revision"], "rev-factory")
            missing = tools.call(
                "search_project",
                {"project_id": "missing", "query": "factory-mcp-quill"},
                scopes=default_read_scopes(),
            )
            self.assertFalse(missing.ok)
            self.assertIn("unknown project_id", missing.error)

    def _wait_until(self, predicate, message: str) -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(message)


class McpHttpsSmokeTests(unittest.TestCase):
    def test_serve_process_speaks_https(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            _seed(data)
            os.chmod(data, 0o750)
            cert_path, key_path = _cert(root)
            port = _free_port()
            resource = f"https://127.0.0.1:{port}/mcp"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["ATLAS_MCP_RESOURCE_URL"] = resource
            env["ATLAS_MCP_ISSUER_URL"] = ISSUER
            env["ATLAS_MCP_INTROSPECTION_URL"] = "https://issuer.example/oauth/introspect"
            env["ATLAS_MCP_INTROSPECTION_CLIENT_ID"] = "atlas-resource"
            env["ATLAS_MCP_INTROSPECTION_CLIENT_SECRET"] = SECRET
            env["ATLAS_MCP_TLS_CERT"] = str(cert_path)
            env["ATLAS_MCP_TLS_KEY"] = str(key_path)
            log_path = root / "serve.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "atlas",
                        "--data-root",
                        str(data),
                        "mcp",
                        "serve",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            try:
                body = _wait_https(port)
                metadata = json.loads(body)
                self.assertEqual(metadata["resource"], resource)
                status = _https_status(port, "POST", "/mcp")
                self.assertEqual(status, 401)
                self.assertTrue(process.poll() is None)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn(SECRET, log_text)


def _wait_https(port: int) -> bytes:
    deadline = time.time() + 15
    context = ssl._create_unverified_context()
    last_error = "not ready"
    while time.time() < deadline:
        try:
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=1)
            conn.request("GET", "/.well-known/oauth-protected-resource/mcp")
            response = conn.getresponse()
            body = response.read()
            conn.close()
            if response.status == 200:
                return body
            last_error = f"status {response.status}"
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.1)
    raise AssertionError(f"HTTPS MCP server did not become ready: {last_error}")


def _https_status(port: int, method: str, path: str) -> int:
    context = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
    conn.request(method, path, body=b"{}", headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    response.read()
    conn.close()
    return response.status


async def _search(url: str, token: str, project_id: str, query: str):
    text = await _tool(
        url,
        token,
        "search_project",
        {"project_id": project_id, "query": query, "limit": 5},
    )
    return json.loads(text)


async def _provenance(url: str, token: str, **arguments):
    text = await _tool(url, token, "get_provenance", arguments)
    return json.loads(text)


async def _tool(url: str, token: str, name: str, arguments: dict) -> str:
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
        timeout=10.0,
    )
    transport = streamable_http_client(url, http_client=http)
    try:
        async with Client(transport, read_timeout_seconds=10) as client:
            result = await client.call_tool(name, arguments)
    finally:
        await http.aclose()
    if result.is_error:
        raise AssertionError(result)
    texts = [block.text for block in result.content if getattr(block, "text", None)]
    return "\n".join(texts)


if __name__ == "__main__":
    unittest.main()
