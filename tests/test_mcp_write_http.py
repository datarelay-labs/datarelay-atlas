"""Real MCP-over-HTTPS E2E for opt-in bounded knowledge/source writes.

Uses an ephemeral project registry, generated TLS certificate and synthetic
GitHub fixture. Never contacts production or stores credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn
from mcp.server.auth.provider import AccessToken

from atlas.github_sync import FetchedSource
from atlas.mcp_http import build_mcp_application
from atlas.mcp_write import SOURCE_WRITE_SCOPE
from atlas.security import READ_SCOPE, WRITE_SCOPE
from atlas.service import AtlasService
from test_mcp_http import MapVerifier, _config, _free_port, _list_tools, _seed, _tool


class McpBoundedWriteHttpTests(unittest.TestCase):
    def test_mcp_tools_roundtrip_scope_provenance_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            _seed(data)
            port = _free_port()
            resource = f"https://127.0.0.1:{port}/mcp"
            config = _config(root, port=port, resource_url=resource)
            tokens = {
                label: AccessToken(
                    token=label,
                    client_id="isolated-test-client",
                    scopes=scopes,
                    resource=resource,
                )
                for label, scopes in {
                    "read": [READ_SCOPE],
                    "memory": [READ_SCOPE, WRITE_SCOPE],
                    "source": [READ_SCOPE, SOURCE_WRITE_SCOPE],
                }.items()
            }
            env = {
                "ATLAS_MCP_MEMORY_WRITE_ENABLED": "1",
                "ATLAS_MCP_SOURCE_WRITE_ENABLED": "1",
            }
            with patch.dict(os.environ, env):
                app = build_mcp_application(
                    AtlasService(data), config, MapVerifier(tokens)
                )
                server = uvicorn.Server(uvicorn.Config(
                    app, host="127.0.0.1", port=port,
                    ssl_certfile=str(config.tls_cert),
                    ssl_keyfile=str(config.tls_key),
                    log_level="warning",
                ))
                thread = threading.Thread(target=server.run, daemon=True)
                thread.start()
                try:
                    for _ in range(100):
                        if server.started:
                            break
                        thread.join(timeout=0.05)
                    self.assertTrue(server.started)

                    tools = {x["name"]: x for x in asyncio.run(
                        _list_tools(resource, "read")
                    )}
                    for name, scope in (
                        ("ingest_memory_candidates", WRITE_SCOPE),
                        ("register_github_source", SOURCE_WRITE_SCOPE),
                    ):
                        self.assertIn(name, tools)
                        self.assertFalse(tools[name]["annotations"]["readOnlyHint"])
                        self.assertEqual(
                            tools[name]["_meta"]["securitySchemes"],
                            [{"type": "oauth2", "scopes": [scope]}],
                        )

                    arguments = {
                        "project_id": "alpha",
                        "input_kind": "RUN_SUMMARY",
                        "observed_at": "2026-10-16T01:00:00Z",
                        "candidates": [
                            {"candidate_class": "LESSON_LEARNED",
                             "content": "Use validated shared E2E browser session."}
                        ],
                        "workstream": "shared-e2e",
                    }
                    with self.assertRaises(AssertionError):
                        asyncio.run(_tool(
                            resource, "read", "ingest_memory_candidates", arguments
                        ))
                    self.assertFalse((data / "memory-candidates.json").exists())

                    result = json.loads(asyncio.run(_tool(
                        resource, "memory", "ingest_memory_candidates", arguments
                    )))
                    retry = json.loads(asyncio.run(_tool(
                        resource, "memory", "ingest_memory_candidates", arguments
                    )))
                    self.assertEqual(result["accepted"], 1)
                    self.assertEqual(retry["accepted"], 0)
                    self.assertEqual(retry["replayed"], 1)
                    self.assertFalse(result["canonical"])
                    observed = json.loads(asyncio.run(_tool(
                        resource, "read", "get_memory_candidates",
                        {"project_id": "alpha"},
                    )))
                    self.assertEqual(observed["count"], 1)
                    self.assertEqual(
                        observed["items"][0]["repository"], "datarelay-labs/alpha"
                    )

                    with self.assertRaises(AssertionError):
                        asyncio.run(_tool(
                            resource, "source", "ingest_memory_candidates", arguments
                        ))
                    with self.assertRaises(AssertionError):
                        asyncio.run(_tool(
                            resource, "memory", "ingest_memory_candidates",
                            {**arguments, "candidates": [
                                {"candidate_class": "RUN_SUMMARY",
                                 "content": "password=super-secret-string"},
                            ]},
                        ))
                    self.assertEqual(AtlasService(data).memory_candidates(
                        project_id="alpha"
                    )["count"], 1)

                    src = {
                        "project_id": "alpha",
                        "source_id": "chromium-e2e",
                        "source_path": "docs/runbooks/shared-chromium.md",
                        "title": "Shared Chromium E2E",
                    }
                    with self.assertRaises(AssertionError):
                        asyncio.run(_tool(
                            resource, "memory", "register_github_source", src
                        ))
                    self.assertEqual(len(AtlasService(data).list_sources("alpha")), 1)
                    with patch(
                        "atlas.mcp_write.fetch_github_file",
                        return_value=FetchedSource(
                            "Chromium operator guide alpha-new-session-mcp-search\n",
                            "b" * 40,
                        ),
                    ) as fetch:
                        created = json.loads(asyncio.run(_tool(
                            resource, "source", "register_github_source", src
                        )))
                        replay = json.loads(asyncio.run(_tool(
                            resource, "source", "register_github_source", src
                        )))
                    self.assertEqual(fetch.call_count, 2)
                    self.assertEqual(created["sync_state"], "success")
                    self.assertEqual(replay["sync_state"], "unchanged")
                    self.assertEqual(replay["state"], "UNCHANGED")
                    self.assertEqual(created["repository"], "datarelay-labs/alpha")
                    self.assertFalse(created["canonical"])
                    hits = json.loads(asyncio.run(_tool(
                        resource, "read", "search_project",
                        {"project_id": "alpha", "query": "alpha-new-session-mcp-search"},
                    )))
                    self.assertTrue(any(
                        x["path"] == src["source_path"] for x in hits
                    ))
                    detail = json.loads(asyncio.run(_tool(
                        resource, "read", "get_source_detail",
                        {"project_id": "alpha", "source_id": "chromium-e2e"},
                    )))
                    self.assertEqual(
                        detail["projection"]["provenance"]["source_revision"],
                        "b" * 40,
                    )
                    with self.assertRaises(AssertionError):
                        asyncio.run(_tool(
                            resource, "source", "register_github_source",
                            {**src, "source_path": ".github/workflows/exfil.md"},
                        ))
                    self.assertEqual(len(AtlasService(data).list_sources("alpha")), 2)
                    with self.assertRaises(AssertionError) as sensitive:
                        asyncio.run(_tool(
                            resource, "source", "register_github_source",
                            {**src, "source_path": "docs/../../password=LEAK-ME-NOT"},
                        ))
                    self.assertNotIn("LEAK-ME-NOT", str(sensitive.exception))
                finally:
                    server.should_exit = True
                    thread.join(timeout=6)
                    self.assertFalse(thread.is_alive())
