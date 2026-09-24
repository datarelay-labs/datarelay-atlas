"""RFC 7662 token introspection for the Atlas MCP resource server.

Atlas does not issue tokens. A configured authorization server introspects
bearer tokens, and this verifier maps an active response onto the MCP SDK
``AccessToken`` so resource and scope checks stay in the SDK middleware.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp.server.auth.provider import AccessToken
from pydantic import AnyHttpUrl, ValidationError as UrlValidationError

_MAX_RESPONSE_BYTES = 65_536
_TIMEOUT_SECONDS = 10.0


class IntrospectionFailed(Exception):
    """The introspection exchange did not yield a usable JSON object."""


@dataclass(frozen=True)
class IntrospectionRequest:
    url: str
    token: str
    client_id: str
    client_secret: str


class IntrospectionTransport(Protocol):
    async def exchange(self, request: IntrospectionRequest) -> dict[str, Any]:
        """Return the introspection JSON object or raise ``IntrospectionFailed``."""


class HttpxIntrospectionTransport:
    """POST ``application/x-www-form-urlencoded`` and refuse redirects."""

    async def exchange(self, request: IntrospectionRequest) -> dict[str, Any]:
        import httpx2

        try:
            async with httpx2.AsyncClient(
                timeout=_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    request.url,
                    data={"token": request.token, "token_type_hint": "access_token"},
                    auth=(request.client_id, request.client_secret),
                    headers={"Accept": "application/json"},
                )
        except httpx2.HTTPError as exc:
            raise IntrospectionFailed("introspection request failed") from exc
        if response.status_code != 200:
            raise IntrospectionFailed("introspection request failed")
        body = response.content
        if len(body) > _MAX_RESPONSE_BYTES:
            raise IntrospectionFailed("introspection response is too large")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise IntrospectionFailed("introspection response is not JSON") from exc
        if not isinstance(payload, dict):
            raise IntrospectionFailed("introspection response is not a JSON object")
        return payload


class Rfc7662TokenVerifier:
    """Resource-server verifier. Returns ``None`` for every inactive token."""

    def __init__(
        self,
        *,
        introspection_url: str,
        client_id: str,
        client_secret: str,
        resource_url: str,
        transport: IntrospectionTransport,
    ) -> None:
        self._introspection_url = introspection_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._resource_url = _canonical_resource(resource_url)
        self._transport = transport

    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not token.strip():
            return None
        request = IntrospectionRequest(
            url=self._introspection_url,
            token=token,
            client_id=self._client_id,
            client_secret=self._client_secret,
        )
        try:
            payload = await self._transport.exchange(request)
        except IntrospectionFailed:
            return None
        return _access_token_from_introspection(
            token,
            payload,
            expected_resource=self._resource_url,
        )


def _access_token_from_introspection(
    token: str,
    payload: dict[str, Any],
    *,
    expected_resource: str,
) -> AccessToken | None:
    if payload.get("active") is not True:
        return None
    client_id = payload.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        return None
    scopes = _scopes(payload.get("scope"))
    if scopes is None:
        return None
    now = int(time.time())
    expires_at = _optional_epoch(payload.get("exp"), now=now, reject_past=True)
    if expires_at is _REJECT:
        return None
    not_before = _optional_epoch(payload.get("nbf"), now=now, reject_past=False)
    if not_before is _REJECT or (isinstance(not_before, int) and not_before > now):
        return None
    if not _audience_matches(payload, expected_resource):
        return None
    subject = payload.get("sub")
    claims: dict[str, Any] = {}
    issuer = payload.get("iss")
    if isinstance(issuer, str) and issuer.strip():
        claims["iss"] = issuer.strip()
    return AccessToken(
        token=token,
        client_id=client_id.strip(),
        scopes=scopes,
        expires_at=expires_at if isinstance(expires_at, int) else None,
        resource=expected_resource,
        subject=subject.strip() if isinstance(subject, str) and subject.strip() else None,
        claims=claims or None,
    )


_REJECT = object()


def _scopes(value: object) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, str):
        return None
    return [part for part in value.split(" ") if part]


def _optional_epoch(value: object, *, now: int, reject_past: bool) -> int | None | object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return _REJECT
    if reject_past and value < now:
        return _REJECT
    return value


def _audience_matches(payload: dict[str, Any], expected_resource: str) -> bool:
    values: list[str] = []
    audience = payload.get("aud")
    if isinstance(audience, str):
        values.append(audience)
    elif isinstance(audience, list):
        if not all(isinstance(item, str) for item in audience):
            return False
        values.extend(audience)
    elif audience is not None:
        return False
    resource = payload.get("resource")
    if isinstance(resource, str):
        values.append(resource)
    elif resource is not None:
        return False
    if not values:
        return False
    expected = _canonical_resource(expected_resource)
    for value in values:
        try:
            if _canonical_resource(value) == expected:
                return True
        except ValueError:
            continue
    return False


def _canonical_resource(url: str) -> str:
    try:
        parsed = AnyHttpUrl(url)
    except UrlValidationError as exc:
        raise ValueError("resource URL is invalid") from exc
    parts = urlsplit(str(parsed))
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ValueError("resource URL is invalid")
    return str(parsed).removesuffix("/")
