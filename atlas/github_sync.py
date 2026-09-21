"""Authenticated GitHub canonical source ingestion (Atlas-owned)."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from atlas.provenance import CanonicalSource, ValidationError, validate_source


@dataclass(frozen=True)
class FetchedSource:
    content: str
    source_revision: str


FetchFn = Callable[[CanonicalSource, str | None], FetchedSource]


def fetch_github_file(
    source: CanonicalSource,
    token: str | None = None,
    *,
    opener: Callable[..., object] | None = None,
) -> FetchedSource:
    """Fetch a single file via the GitHub Contents API.

    Credentials come from the caller or GITHUB_TOKEN and never enter provenance.
    """
    validate_source(source)
    encoded_path = "/".join(urllib.request.quote(part) for part in source.source_path.split("/"))
    api_base = os.environ.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
    url = (
        f"{api_base}/repos/{source.repository}/contents/{encoded_path}"
        f"?ref={urllib.request.quote(source.ref)}"
    )
    auth = token if token is not None else os.environ.get("GITHUB_TOKEN", "").strip() or None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "datarelay-atlas-sync",
    }
    if auth:
        headers["Authorization"] = f"Bearer {auth}"

    request = urllib.request.Request(url, headers=headers)
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=30) as response:  # type: ignore[arg-type]
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValidationError(
            f"GitHub {source.repository}@{source.ref} returned HTTP {exc.code}"
        ) from exc

    if (
        payload.get("type") != "file"
        or payload.get("encoding") != "base64"
        or not payload.get("content")
        or not payload.get("sha")
    ):
        raise ValidationError(
            f"GitHub returned an unsupported content payload for {source.repository}"
        )

    raw = base64.b64decode(payload["content"].replace("\n", "")).decode("utf-8")
    return FetchedSource(content=raw, source_revision=str(payload["sha"]))
