#!/usr/bin/env python3
"""CLI: sync configured canonical sources into a local projection store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from atlas.projection import ProjectionStore
from atlas.provenance import CanonicalSource


def load_sources(path: Path) -> list[CanonicalSource]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = []
    for item in raw["sources"]:
        sources.append(
            CanonicalSource(
                source_id=item["source_id"],
                project_id=item["project_id"],
                provider=item.get("provider", "github"),
                repository=item["repository"],
                ref=item["ref"],
                source_path=item["source_path"],
                enabled=item.get("enabled", True),
                media_type=item.get("media_type", "text/markdown"),
                title=item.get("title"),
            )
        )
    return sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    args = parser.parse_args(argv)
    store = ProjectionStore(args.store)
    records = store.sync_all(load_sources(args.config))
    for record in records:
        print(f"{record.source_id}\t{record.sync_state}\t{record.source_revision}")
    return 0 if all(r.sync_state in {"ok", "disabled"} for r in records) else 2


if __name__ == "__main__":
    sys.exit(main())
