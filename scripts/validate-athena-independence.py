"""Independence gate: Atlas must not require datarelay-labs/athena."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Paths that may mention Athena as migration/historical evidence only.
ALLOW_MENTION_PREFIXES = (
    "docs/",
    "evidence/",
    "integrations/athena/",
    "THIRD_PARTY.md",
    "README.md",
    "README.ko.md",
    "AGENTS.md",
    "docs/migration/",
)

FORBIDDEN_RUNTIME_PATTERNS = [
    re.compile(r"github\.com[/:]datarelay-labs/athena(?![\w-])"),
    re.compile(r"git@github\.com:datarelay-labs/athena\.git"),
    re.compile(r"submodule.*athena", re.I),
]

FORBIDDEN_IN_SCRIPTS = [
    re.compile(r"datarelay-labs/athena"),
    re.compile(r"jannismilz/athena"),
]


def iter_text_files(root: Path):
    skip_dirs = {".git", "__pycache__", ".venv", "node_modules", ".atlas-data"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pyc"}:
            continue
        yield path


def is_allowed_mention(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in ALLOW_MENTION_PREFIXES)


def main() -> int:
    errors: list[str] = []

    gitmodules = ROOT / ".gitmodules"
    if gitmodules.exists() and "athena" in gitmodules.read_text(encoding="utf-8").lower():
        errors.append(".gitmodules references athena")

    # Build/test/deploy scripts must not require Athena repo.
    for rel_dir in ("scripts", ".github/workflows", "atlas", "tests"):
        base = ROOT / rel_dir
        if not base.exists():
            continue
        for path in iter_text_files(base):
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            text = path.read_text(encoding="utf-8", errors="replace")
            for pat in FORBIDDEN_IN_SCRIPTS:
                if pat.search(text):
                    # Independence validator itself may mention the string as a detection target.
                    if rel == "scripts/validate-athena-independence.py":
                        continue
                    if rel.startswith("tests/") and "independence" in rel:
                        continue
                    errors.append(f"{rel}: forbidden Athena repo reference in runtime/test path")

    # Public contracts must not expose Wiki.js/Athena identity as required fields.
    contract = ROOT / "docs/contracts/source-provider-provenance.schema.json"
    if contract.exists():
        text = contract.read_text(encoding="utf-8")
        for bad in ("wiki_path", "wiki.js", "athena_page", "page_id"):
            if bad in text:
                errors.append(f"public schema exposes {bad}")

    # No vendored Athena tree.
    for candidate in ("vendor/athena", "third_party/athena", "athena"):
        if (ROOT / candidate).exists() and candidate != "integrations/athena":
            # integrations/athena is metadata only; 'athena' dir at root would be bad if it's source tree
            if candidate == "athena":
                errors.append("root athena/ directory present; Athena tree must not be vendored")

    if (ROOT / "integrations/athena/source-lock.yaml").exists():
        lock = (ROOT / "integrations/athena/source-lock.yaml").read_text(encoding="utf-8")
        if "vendor_source_into_atlas_repo: true" in lock:
            errors.append("source-lock still authorizes vendoring Athena into Atlas")

    if errors:
        print("ATHENA_INDEPENDENCE=FAIL")
        for err in errors:
            print(f"- {err}")
        return 1

    print("ATHENA_INDEPENDENCE=PASS")
    print("no submodule/runtime/script dependency on datarelay-labs/athena detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
