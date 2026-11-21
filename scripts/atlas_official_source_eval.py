#!/usr/bin/env python3
"""No-network Atlas official-source search benchmark (advisory only)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas.public_source_benchmark import (  # noqa: E402
    BenchmarkError,
    evaluate,
    read_json_file,
    read_observations,
)

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[1] / "evals" / "public-sources" / "questions-v1.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--observations", help="real provider observations in JSONL")
    args = parser.parse_args()
    try:
        manifest = read_json_file(args.manifest)
        observations = (
            read_observations(args.observations)
            if args.observations else []
        )
        report = evaluate(manifest, observations)
    except (OSError, BenchmarkError) as exc:
        # The exception type and generic validation message contain no input
        # body, URL, prompt or credential. Do not print raw exception context.
        code = str(exc) if isinstance(exc, BenchmarkError) else "benchmark file unavailable"
        print(json.dumps({"state": "INVALID", "error": code}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
