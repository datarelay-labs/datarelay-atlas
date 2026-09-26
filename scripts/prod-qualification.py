"""Operator entry for the prod-atlas qualification harness (ADR-0013)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atlas.qualification import main

if __name__ == "__main__":
    raise SystemExit(main())
