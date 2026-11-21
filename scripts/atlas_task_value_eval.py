#!/usr/bin/env python3
"""Evaluate declared Atlas-vs-canonical task comparisons without network calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas.task_value_benchmark import TaskValueError, evaluate, load_study  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", help="local metadata-only actual task study JSON file")
    args = parser.parse_args()
    try:
        study = load_study(args.study) if args.study else {
            "schema_version": 1,
            "study_id": "atlas_task_value_empty",
            "scope": "METADATA_ONLY",
            "observations": [],
        }
        result = evaluate(study)
    except (TaskValueError, OSError) as exc:
        msg = str(exc) if isinstance(exc, TaskValueError) else "study file unavailable"
        print(json.dumps({"state": "INVALID", "error": msg}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
