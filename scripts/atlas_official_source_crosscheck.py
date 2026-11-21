#!/usr/bin/env python3
"""Offline public official-source citation crosscheck; no web/network calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas.official_source_crosscheck import (  # noqa: E402
    CrosscheckError, crosscheck, load_file,
)
from atlas.public_source_benchmark import read_json_file, BenchmarkError  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "evals/public-sources/questions-v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--evidence", help="optional public-only JSON evidence file")
    args = parser.parse_args()
    try:
        manifest = read_json_file(args.manifest)
        evidence = load_file(args.evidence) if args.evidence else {
            "schema_version": 1,
            "dataset_id": manifest["dataset_id"],
            "scope": "PUBLIC_ONLY",
            "observations": [],
        }
        report = crosscheck(manifest, evidence)
    except (OSError, ValueError, CrosscheckError, BenchmarkError) as exc:
        msg = (
            str(exc) if isinstance(exc, (CrosscheckError, BenchmarkError))
            else "public research evidence unavailable or invalid"
        )
        print(json.dumps({"state": "INVALID", "error": msg}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
