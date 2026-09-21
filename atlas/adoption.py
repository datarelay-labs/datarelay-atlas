"""Engineering System adoption metadata reader (Atlas observes, does not redefine)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlas.provenance import ValidationError, validate_source_path

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised when PyYAML absent
    yaml = None


@dataclass(frozen=True)
class AdoptionMetadata:
    project_name: str | None
    engineering_system_version: str | None
    engineering_system_baseline: str | None
    engineering_system_mode: str | None
    engineering_system_ci_mode: str | None
    raw: dict[str, Any]
    source_path: str


def _parse_yaml(text: str) -> dict[str, Any]:
    if yaml is not None:
        loaded = yaml.safe_load(text)
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValidationError("engineering metadata must be a mapping")
        return loaded
    return _minimal_yaml_mapping(text)


def _minimal_yaml_mapping(text: str) -> dict[str, Any]:
    """Parse a constrained subset of YAML mappings without PyYAML.

    Sufficient for `.engineering/project.yaml` style documents used by Atlas.
    Rejects ambiguous nested structures rather than guessing.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if "\t" in raw_line.split(":")[0]:
            raise ValidationError(f"tabs not allowed in engineering metadata line {lineno}")
        if ":" not in raw_line:
            raise ValidationError(f"unsupported engineering metadata line {lineno}")
        key, _, value = raw_line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            raise ValidationError(f"empty key in engineering metadata line {lineno}")
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            nested: dict[str, Any] = {}
            parent[key] = nested
            stack.append((indent, nested))
        else:
            parent[key] = value
    return root


def parse_adoption_yaml(text: str, *, source_path: str) -> AdoptionMetadata:
    validate_source_path(source_path)
    raw = _parse_yaml(text)
    eng = raw.get("engineering_system") or {}
    if eng is not None and not isinstance(eng, dict):
        raise ValidationError("engineering_system must be a mapping when present")
    eng = eng or {}
    project = raw.get("project") or {}
    if project is not None and not isinstance(project, dict):
        raise ValidationError("project must be a mapping when present")
    project = project or {}
    return AdoptionMetadata(
        project_name=project.get("name"),
        engineering_system_version=eng.get("version"),
        engineering_system_baseline=eng.get("baseline"),
        engineering_system_mode=eng.get("mode"),
        engineering_system_ci_mode=eng.get("ci_mode"),
        raw=raw,
        source_path=source_path,
    )


def read_adoption_file(path: Path, *, source_path: str | None = None) -> AdoptionMetadata:
    text = Path(path).read_text(encoding="utf-8")
    rel = source_path or Path(path).name
    return parse_adoption_yaml(text, source_path=rel)


def assert_adoption_project_consistency(
    adoption: AdoptionMetadata,
    *,
    project_id: str,
) -> None:
    """Fail closed when adoption metadata names a conflicting project identity."""
    name = adoption.project_name
    if name is None:
        return
    if name != project_id:
        raise ValidationError(
            f"engineering metadata project.name mismatch: "
            f"metadata={name} project_id={project_id}"
        )
