"""Ephemeral isolated storage for MCP write-capability probes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from atlas.provenance import ValidationError

MAX_VALUE_BYTES = 512
TTL_SECONDS = 900


class WriteProbeStore:
    """File-backed probe store intentionally outside the Atlas data root."""

    def __init__(self, resource_url: str) -> None:
        identity = hashlib.sha256(resource_url.encode("utf-8")).hexdigest()[:16]
        self.root = Path(tempfile.gettempdir()) / f"datarelay-atlas-write-probes-{identity}"

    def create(self, value: str) -> dict[str, object]:
        text = value.strip()
        if not text:
            raise ValidationError("write probe value is required")
        if len(text.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ValidationError("write probe value exceeds 512 bytes")
        self._prepare()
        self._purge()
        probe_id = uuid.uuid4().hex
        created_at = int(time.time())
        payload = {
            "kind": "atlas_mcp_write_probe",
            "probe_id": probe_id,
            "value": text,
            "created_at": created_at,
            "expires_at": created_at + TTL_SECONDS,
            "canonical": False,
            "derived_knowledge": False,
            "isolated": True,
        }
        target = self._path(probe_id)
        fd, tmp_name = tempfile.mkstemp(prefix=".probe-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return payload

    def get(self, probe_id: str) -> dict[str, object]:
        self._validate_id(probe_id)
        self._prepare()
        self._purge()
        path = self._path(probe_id)
        if not path.is_file():
            raise ValidationError("write probe not found")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("write probe is unreadable") from exc

    def delete(self, probe_id: str) -> dict[str, object]:
        self._validate_id(probe_id)
        self._prepare()
        self._purge()
        path = self._path(probe_id)
        if not path.is_file():
            raise ValidationError("write probe not found")
        path.unlink()
        return {
            "kind": "atlas_mcp_write_probe_delete",
            "probe_id": probe_id,
            "deleted": True,
            "canonical": False,
            "derived_knowledge": False,
            "isolated": True,
        }

    def _prepare(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _path(self, probe_id: str) -> Path:
        return self.root / f"{probe_id}.json"

    @staticmethod
    def _validate_id(probe_id: str) -> None:
        if len(probe_id) != 32 or any(ch not in "0123456789abcdef" for ch in probe_id):
            raise ValidationError("invalid write probe id")

    def _purge(self) -> None:
        now = int(time.time())
        for path in self.root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if int(payload.get("expires_at", 0)) <= now:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
