#!/usr/bin/env python3
"""Validate Atlas source/provider/provenance contract fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/source-provider-provenance.schema.json"
FIXTURE = ROOT / "docs/contracts/fixtures/source-provider-provenance.example.json"


def main() -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(fixture), key=lambda err: list(err.path))
    if errors:
        for err in errors:
            path = "/".join(str(p) for p in err.path) or "<root>"
            print(f"INVALID {path}: {err.message}", file=sys.stderr)
        return 1

    # Guardrails that JSON Schema alone should not silently weaken.
    source_path = fixture["source"]["source_path"]
    if source_path.startswith("/") or ".." in source_path.split("/"):
        print("INVALID source_path: must be relative without '..'", file=sys.stderr)
        return 1
    if fixture["derived_record"]["canonical"] is not False:
        print("INVALID derived_record.canonical must be false", file=sys.stderr)
        return 1
    if fixture["retrieval_hit"]["canonical"] is not False:
        print("INVALID retrieval_hit.canonical must be false", file=sys.stderr)
        return 1
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

    print("ATLAS-CONTRACT-001 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
