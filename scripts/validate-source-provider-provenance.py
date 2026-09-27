#!/usr/bin/env python3
"""Validate Atlas source/provider/provenance contract fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/source-provider-provenance.schema.json"
FIXTURES = (
    ROOT / "docs/contracts/fixtures/source-provider-provenance.example.json",
    ROOT / "docs/contracts/fixtures/personal-markdown-snapshot.example.json",
)


def _errors(validator: Draft202012Validator, payload: dict) -> list:
    return sorted(validator.iter_errors(payload), key=lambda err: list(err.path))


def _guard(fixture: dict) -> str | None:
    source_path = fixture["source"]["source_path"]
    if source_path.startswith("/") or ".." in source_path.split("/"):
        return "INVALID source_path: must be relative without '..'"
    if fixture["derived_record"]["canonical"] is not False:
        return "INVALID derived_record.canonical must be false"
    if fixture["retrieval_hit"]["canonical"] is not False:
        return "INVALID retrieval_hit.canonical must be false"
    if fixture["source"]["provider"] == "local-markdown":
        hit = fixture["retrieval_hit"]
        if hit.get("source_class") != "personal" or hit.get("engineering_authority") is not False:
            return "INVALID personal retrieval must be non-authoritative"
    forbidden_keys = {"token", "access_token", "secret", "password", "authorization", "api_key"}

    def walk(obj, path: str = "") -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_l = str(key).lower()
                if key_l in forbidden_keys or key_l.endswith("_token") or key_l.endswith("_secret"):
                    raise SystemExit(f"INVALID fixture key looks credential-like: {path}/{key}")
                walk(value, f"{path}/{key}")
        elif isinstance(obj, list):
            for idx, value in enumerate(obj):
                walk(value, f"{path}[{idx}]")

    walk(fixture)
    return None


def main() -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for path in FIXTURES:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        errors = _errors(validator, fixture)
        if errors:
            for err in errors:
                location = "/".join(str(p) for p in err.path) or "<root>"
                print(f"INVALID {path.name} {location}: {err.message}", file=sys.stderr)
            return 1
        guard = _guard(fixture)
        if guard:
            print(f"INVALID {path.name}: {guard}", file=sys.stderr)
            return 1

    github = json.loads(FIXTURES[0].read_text(encoding="utf-8"))
    github["source"]["source_class"] = "personal"
    if not _errors(validator, github):
        print("INVALID schema accepted github source_class=personal", file=sys.stderr)
        return 1
    personal = json.loads(FIXTURES[1].read_text(encoding="utf-8"))
    personal["retrieval_hit"]["engineering_authority"] = True
    if not _errors(validator, personal):
        print("INVALID schema accepted personal engineering authority", file=sys.stderr)
        return 1
    mismatched = json.loads(FIXTURES[1].read_text(encoding="utf-8"))
    mismatched["provider"] = {"provider": "github", "auth_mode": "installation_token"}
    if not _errors(validator, mismatched):
        print("INVALID schema accepted local-markdown source with github auth", file=sys.stderr)
        return 1
    reverse = json.loads(FIXTURES[0].read_text(encoding="utf-8"))
    reverse["provider"] = {"provider": "local-markdown", "auth_mode": "none"}
    if not _errors(validator, reverse):
        print("INVALID schema accepted github source with local auth", file=sys.stderr)
        return 1

    print("ATLAS-CONTRACT-001 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
