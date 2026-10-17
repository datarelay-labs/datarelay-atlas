"""Fail-closed Atlas protected-release runner qualification (never a deployer).

This code verifies the authority of the *environment it is called in*. The
canonical, SHA-pinned Engineering System reusable workflow separately verifies
the protected default branch and exact candidate ancestry, and selects the
restricted runner group. Neither is replaced by this module.

No production filesystem writes, service control, credentials or SSH.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Mapping
import urllib.request

REPOSITORY = "datarelay-labs/datarelay-atlas"
EXPECTED_REF = "refs/heads/main"
ISSUER = "https://auth.atlas.datarelay.run/realms/atlas"
MCP_HEALTH_URL = "https://mcp.atlas.datarelay.run/healthz"
AUTH_DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


class ReleaseBoundaryError(ValueError):
    """A trustworthy protected release execution boundary was not established."""


def verify_boundary(
    env: Mapping[str, str],
    *,
    actual_head: str,
    root: Path,
    execution_context: str,
    phase: str,
) -> dict[str, str]:
    """Validate values independently observed from GitHub and the local checkout.

    Inputs here are injectable for deterministic negative testing. Production
    CLI obtains the checkout HEAD using git, not a caller-authored receipt.
    """
    expected = env.get("EXPECTED_SHA", "")
    requirements = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": EXPECTED_REF,
        "GITHUB_REF_PROTECTED": "true",
        "RUNNER_ENVIRONMENT": "self-hosted",
        "ENGINEERING_RELEASE_CONTEXT": "protected-production",
        "AUTHORIZED_EXECUTION_CONTEXT": "protected-production",
        "RELEASE_PHASE": phase,
    }
    for key, value in requirements.items():
        if env.get(key, "") != value:
            raise ReleaseBoundaryError("BOUNDARY_" + key + "_INVALID")
    if phase not in {"qualify", "post-release"}:
        raise ReleaseBoundaryError("BOUNDARY_PHASE_INVALID")
    if SHA_PATTERN.fullmatch(expected) is None:
        raise ReleaseBoundaryError("CANDIDATE_SHA_INVALID")
    if actual_head != expected:
        raise ReleaseBoundaryError("CANDIDATE_HEAD_MISMATCH")
    if execution_context != "protected-production":
        raise ReleaseBoundaryError("RELEASE_PROFILE_CONTEXT_INVALID")
    expected_root = env.get("CANDIDATE_ROOT", "")
    if not expected_root or Path(expected_root).resolve() != root.resolve():
        raise ReleaseBoundaryError("CANDIDATE_ROOT_MISMATCH")
    return {
        "boundary": "PROTECTED_RUNNER_CONTEXT_VERIFIED",
        "candidate_sha": expected,
        "phase": phase,
        "production_deployed": "NOT_ASSERTED",
    }


def _checkout_context(root: Path) -> tuple[str, str]:
    candidate = (root / ".engineering" / "release.yaml")
    if not candidate.is_file() or candidate.is_symlink():
        raise ReleaseBoundaryError("RELEASE_PROFILE_UNAVAILABLE")
    profile_text = candidate.read_text(encoding="utf-8")
    if not re.search(r"(?m)^execution_context: protected-production\s*$", profile_text):
        raise ReleaseBoundaryError("RELEASE_PROFILE_CONTEXT_INVALID")
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip(), "protected-production"


def _read_json(url: str, *, limit: int = 32768) -> dict:
    # Public GETs only, standard certificate verification, no bearer secrets.
    request = urllib.request.Request(
        url, headers={"Accept": "application/json"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        if response.status != 200 or response.geturl() != url:
            raise ReleaseBoundaryError("PUBLIC_ENDPOINT_NOT_HTTP200")
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ReleaseBoundaryError("PUBLIC_RESPONSE_OVERSIZED")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ReleaseBoundaryError("PUBLIC_RESPONSE_NOT_OBJECT")
    return result


def public_smoke() -> dict[str, str]:
    """Observe two distinct HTTPS SNI hosts without treating health as deployment."""
    _read_json(MCP_HEALTH_URL)
    discovery = _read_json(AUTH_DISCOVERY_URL)
    if discovery.get("issuer") != ISSUER:
        raise ReleaseBoundaryError("KEYCLOAK_ISSUER_MISMATCH")
    for key in ("authorization_endpoint", "token_endpoint"):
        endpoint = discovery.get(key, "")
        if not isinstance(endpoint, str) or not endpoint.startswith(ISSUER + "/"):
            raise ReleaseBoundaryError("KEYCLOAK_METADATA_ORIGIN_MISMATCH")
    return {
        "public_mcp": "HTTP200",
        "public_auth": "HTTP200",
        "issuer": "EXPECTED",
        "operational_cutover": "NOT_ASSERTED",
        "native_mcp_write_acceptance": "NOT_TESTED",
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in {"preflight", "qualify", "public-smoke"}:
        print("usage: atlas_protected_release_gate.py preflight|qualify|public-smoke", file=sys.stderr)
        return 2
    task = args[0]
    phase = "post-release" if task == "public-smoke" else "qualify"
    try:
        root = Path.cwd().resolve()
        actual_head, execution_context = _checkout_context(root)
        evidence = verify_boundary(
            os.environ,
            actual_head=actual_head,
            root=root,
            execution_context=execution_context,
            phase=phase,
        )
        if task == "public-smoke":
            evidence.update(public_smoke())
        evidence["action"] = task.upper()
        evidence["authority"] = "BOUNDARY_ONLY_NOT_RELEASE_AUTHORIZATION"
        print(json.dumps(evidence, sort_keys=True))
        return 0
    except (ReleaseBoundaryError, OSError, ValueError, subprocess.SubprocessError) as exc:
        code = str(exc) if isinstance(exc, ReleaseBoundaryError) else type(exc).__name__
        print("ATLAS_PROTECTED_RELEASE_BLOCKED=" + code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
