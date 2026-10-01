"""Deterministic CycloneDX SBOM and Atlas source-provenance evidence."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from importlib.metadata import distributions
from pathlib import Path
from urllib.parse import quote

from atlas.provenance import ValidationError
from atlas.secrets import contains_unsafe_secret

SCHEMA_VERSION = 1
PROVENANCE_KIND = "atlas_sbom_provenance"
AUTHORITY = "EVIDENCE_ONLY"
BOM_FORMAT = "CycloneDX"
BOM_SPEC_VERSION = "1.6"
BOM_FILENAME = "bom.cdx.json"
PROVENANCE_FILENAME = "provenance.json"
GENERATOR = "atlas.sbom.v1"
REPOSITORY = "datarelay-labs/datarelay-atlas"
_MAX_COMPONENTS = 2048
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_~-]{0,127}$")
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LICENSE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_KNOWN_SPDX_IDS = frozenset({
    "0BSD",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "MIT",
    "MPL-2.0",
    "PSF-2.0",
    "Unlicense",
})
_REQ_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_CANON_SEP = re.compile(r"[-_.]+")


def _reject(message: str) -> None:
    raise ValidationError(message)


def _canonical_json_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("SBOM payload is not canonical JSON") from exc

def _artifact_json_bytes(payload: object) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError("SBOM artifact is not serializable JSON") from exc


def _digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest_payload(payload: object) -> str:
    return _digest_bytes(_canonical_json_bytes(payload))


def _canonical_name(value: object, *, label: str = "package name") -> str:
    if not isinstance(value, str):
        _reject(f"SBOM {label} is invalid")
    name = value.strip()
    if _NAME.fullmatch(name) is None or contains_unsafe_secret(name):
        _reject(f"SBOM {label} is invalid")
    return _CANON_SEP.sub("-", name).lower()


def _version(value: object) -> str:
    if not isinstance(value, str):
        _reject("SBOM package version is invalid")
    version = value.strip()
    if _VERSION.fullmatch(version) is None or contains_unsafe_secret(version):
        _reject("SBOM package version is invalid")
    return version


def _license_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        _LICENSE_ID.fullmatch(candidate) is None
        or candidate not in _KNOWN_SPDX_IDS
        or contains_unsafe_secret(candidate)
    ):
        return None
    return candidate


def _file_digest(path: Path, *, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        _reject(f"SBOM {label} is unavailable")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"SBOM {label} is unreadable") from exc
    if len(raw) > _MAX_ARTIFACT_BYTES:
        _reject(f"SBOM {label} exceeds bounded size")
    return _digest_bytes(raw)


def _declared_requirements(path: Path) -> list[str]:
    if path.is_symlink() or not path.is_file():
        _reject("SBOM requirements.txt is unavailable")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValidationError("SBOM requirements.txt is unreadable") from exc
    names: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _REQ_NAME.match(line)
        if match is None:
            _reject("SBOM requirement declaration is unsupported")
        names.add(_canonical_name(match.group(1), label="requirement name"))
    return sorted(names)


def _source_facts(repo_root: Path, *, require_clean: bool) -> dict[str, object]:
    if repo_root.is_symlink():
        _reject("SBOM repository root is unsafe")
    root = repo_root.resolve()
    if not root.is_dir():
        _reject("SBOM repository root is unavailable")

    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                [
                    "/usr/bin/git",
                    "--no-replace-objects",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "submodule.recurse=false",
                    *args,
                ],
                cwd=str(root),
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "HOME": "/nonexistent",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_NO_REPLACE_OBJECTS": "1",
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValidationError("SBOM git state is unavailable") from exc
        if result.returncode != 0:
            _reject("SBOM git state is unavailable")
        return result.stdout.strip()

    top = Path(git("rev-parse", "--show-toplevel")).resolve()
    if top != root:
        _reject("SBOM repository root mismatch")
    head = git("rev-parse", "HEAD").lower()
    if _HEAD.fullmatch(head) is None:
        _reject("SBOM source revision is invalid")
    origin = git("remote", "get-url", "origin")
    cleaned = origin.removesuffix(".git")
    if cleaned.startswith("git@github.com:"):
        repository = cleaned.split(":", 1)[1]
    elif "github.com/" in cleaned:
        repository = cleaned.split("github.com/", 1)[1]
    else:
        _reject("SBOM repository origin is unsupported")
    repository = repository.strip("/")
    if repository != REPOSITORY:
        _reject("SBOM repository identity mismatch")
    index_rows = git("ls-files", "-v", "-z").split("\0")
    if any(
        row and (row[0] == "S" or row[0].islower())
        for row in index_rows
    ):
        _reject("SBOM source index contains hidden file state")
    dirty = bool(
        git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
    )
    if require_clean and dirty:
        _reject("SBOM generation requires a clean repository")
    return {
        "repository": repository,
        "source_revision": head,
        "clean": not dirty,
    }


def _runtime_inventory() -> list[dict[str, object]]:
    """Return the effective distribution set using Python import-path precedence.

    Layered runtimes can expose a higher-precedence environment plus system
    site-packages. Inventory each path in order and retain only the first
    canonical package identity so shadowed fallback distributions do not
    appear as conflicting resolved versions.
    """
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in sys.path:
        search_path = entry or os.getcwd()
        for dist in distributions(path=[search_path]):
            metadata = dist.metadata
            canonical = _canonical_name(metadata.get("Name"))
            if canonical in seen:
                continue
            seen.add(canonical)
            rows.append(
                {
                    "name": metadata.get("Name"),
                    "version": dist.version,
                    "license_expression": metadata.get("License-Expression"),
                    "license": metadata.get("License"),
                }
            )
    return rows


def _python_facts() -> dict[str, str]:
    return {
        "implementation": sys.implementation.name,
        "version": platform.python_version(),
    }


def _normalize_inventory(
    inventory: list[dict[str, object]],
    *,
    declared: set[str],
) -> list[dict[str, object]]:
    if not isinstance(inventory, list) or len(inventory) > _MAX_COMPONENTS:
        _reject("SBOM runtime inventory is invalid")
    by_name: dict[str, dict[str, object]] = {}
    for raw in inventory:
        if not isinstance(raw, dict):
            _reject("SBOM runtime distribution is invalid")
        name = _canonical_name(raw.get("name"))
        version = _version(raw.get("version"))
        if name in by_name:
            if by_name[name]["version"] != version:
                _reject("SBOM runtime inventory has conflicting package versions")
            continue
        license_id = _license_id(raw.get("license_expression"))
        if license_id is None:
            license_id = _license_id(raw.get("license"))
        purl = (
            f"pkg:pypi/{quote(name, safe='-._~')}@"
            f"{quote(version, safe='-._~')}"
        )
        component: dict[str, object] = {
            "type": "library",
            "bom-ref": purl,
            "name": name,
            "version": version,
            "purl": purl,
            "properties": [
                {
                    "name": "atlas:declared-direct",
                    "value": "true" if name in declared else "false",
                }
            ],
        }
        if license_id is not None:
            component["licenses"] = [{"license": {"id": license_id}}]
        by_name[name] = component

    missing = declared - set(by_name)
    if missing:
        _reject(
            "SBOM runtime is missing declared dependencies: "
            + ",".join(sorted(missing))
        )
    return [by_name[name] for name in sorted(by_name)]


def build_sbom_documents_from_facts(
    repo_root: Path,
    *,
    inventory: list[dict[str, object]],
    source_facts: dict[str, object],
    python_facts: dict[str, str],
) -> tuple[dict[str, object], dict[str, object]]:
    root = repo_root.resolve()
    if source_facts.get("repository") != REPOSITORY:
        _reject("SBOM source repository is invalid")
    head = source_facts.get("source_revision")
    if not isinstance(head, str) or _HEAD.fullmatch(head) is None:
        _reject("SBOM source revision is invalid")
    if source_facts.get("clean") is not True:
        _reject("SBOM source facts are not clean")

    implementation = python_facts.get("implementation")
    python_version = python_facts.get("version")
    if (
        not isinstance(implementation, str)
        or _NAME.fullmatch(implementation) is None
        or not isinstance(python_version, str)
        or _VERSION.fullmatch(python_version) is None
    ):
        _reject("SBOM Python runtime identity is invalid")


    requirements = root / "requirements.txt"
    third_party = root / "THIRD_PARTY.md"
    declared = set(_declared_requirements(requirements))
    components = _normalize_inventory(inventory, declared=declared)
    inventory_digest = _digest_payload(components)
    serial = "urn:uuid:" + str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"https://github.com/{REPOSITORY}@{head}:{inventory_digest}",
        )
    )
    sbom: dict[str, object] = {
        "bomFormat": BOM_FORMAT,
        "specVersion": BOM_SPEC_VERSION,
        "serialNumber": serial,
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": f"pkg:github/{REPOSITORY}@{head}",
                "name": "datarelay-atlas",
                "version": head,
                "purl": f"pkg:github/{REPOSITORY}@{head}",
            },
            "properties": [
                {
                    "name": "atlas:source-repository",
                    "value": REPOSITORY,
                },
                {
                    "name": "atlas:source-revision",
                    "value": head,
                },
                {
                    "name": "atlas:runtime-inventory",
                    "value": "importlib.metadata",
                },
            ],
        },
        "components": components,
    }
    validate_cyclonedx_sbom(sbom)
    sbom_sha = _digest_bytes(_artifact_json_bytes(sbom))
    provenance_basis: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": PROVENANCE_KIND,
        "authority": AUTHORITY,
        "generator": GENERATOR,
        "repository": REPOSITORY,
        "source_revision": head,
        "source_clean": True,
        "python_implementation": implementation,
        "python_version": python_version,
        "component_count": len(components),
        "declared_direct_dependency_count": len(declared),
        "requirements_sha256": _file_digest(
            requirements,
            label="requirements.txt",
        ),
        "third_party_sha256": _file_digest(
            third_party,
            label="THIRD_PARTY.md",
        ),
        "sbom_filename": BOM_FILENAME,
        "sbom_sha256": sbom_sha,
        "sbom_serial_number": serial,
    }
    provenance = {
        **provenance_basis,
        "provenance_digest": _digest_payload(provenance_basis),
    }
    validate_sbom_provenance(provenance)
    return sbom, provenance


def build_sbom_documents(
    repo_root: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    return build_sbom_documents_from_facts(
        repo_root,
        inventory=_runtime_inventory(),
        source_facts=_source_facts(repo_root, require_clean=True),
        python_facts=_python_facts(),
    )


def validate_cyclonedx_sbom(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "bomFormat",
        "specVersion",
        "serialNumber",
        "version",
        "metadata",
        "components",
    }:
        _reject("SBOM CycloneDX schema is invalid")
    if (
        payload.get("bomFormat") != BOM_FORMAT
        or payload.get("specVersion") != BOM_SPEC_VERSION
        or payload.get("version") != 1
    ):
        _reject("SBOM CycloneDX identity is invalid")
    serial = payload.get("serialNumber")
    if not isinstance(serial, str) or not serial.startswith("urn:uuid:"):
        _reject("SBOM serial number is invalid")
    try:
        uuid.UUID(serial.removeprefix("urn:uuid:"))
    except (ValueError, AttributeError) as exc:
        raise ValidationError("SBOM serial number is invalid") from exc

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or set(metadata) != {
        "component",
        "properties",
    }:
        _reject("SBOM metadata is invalid")
    app = metadata.get("component")
    if not isinstance(app, dict) or set(app) != {
        "type",
        "bom-ref",
        "name",
        "version",
        "purl",
    }:
        _reject("SBOM application component is invalid")
    app_version = app.get("version")
    expected_app_purl = f"pkg:github/{REPOSITORY}@{app_version}"
    if (
        app.get("type") != "application"
        or app.get("name") != "datarelay-atlas"
        or not isinstance(app_version, str)
        or _HEAD.fullmatch(app_version) is None
        or app.get("bom-ref") != expected_app_purl
        or app.get("purl") != expected_app_purl
    ):
        _reject("SBOM application component is invalid")
    properties = metadata.get("properties")
    if properties != [
        {"name": "atlas:source-repository", "value": REPOSITORY},
        {"name": "atlas:source-revision", "value": app_version},
        {"name": "atlas:runtime-inventory", "value": "importlib.metadata"},
    ]:
        _reject("SBOM metadata properties are invalid")

    components = payload.get("components")
    if not isinstance(components, list) or len(components) > _MAX_COMPONENTS:
        _reject("SBOM components are invalid")
    names: list[str] = []
    refs: set[str] = set()
    for item in components:
        if not isinstance(item, dict):
            _reject("SBOM component is invalid")
        allowed = {
            "type",
            "bom-ref",
            "name",
            "version",
            "purl",
            "properties",
            "licenses",
        }
        required = {
            "type",
            "bom-ref",
            "name",
            "version",
            "purl",
            "properties",
        }
        if not set(item).issubset(allowed) or not required.issubset(item):
            _reject("SBOM component schema is invalid")
        if item.get("type") != "library":
            _reject("SBOM component type is invalid")
        name = _canonical_name(item.get("name"))
        version = _version(item.get("version"))
        purl = (
            f"pkg:pypi/{quote(name, safe='-._~')}@"
            f"{quote(version, safe='-._~')}"
        )
        if item.get("purl") != purl or item.get("bom-ref") != purl:
            _reject("SBOM component purl is invalid")
        if purl in refs:
            _reject("SBOM component reference is duplicated")
        refs.add(purl)
        names.append(name)
        if item.get("properties") not in (
            [{"name": "atlas:declared-direct", "value": "true"}],
            [{"name": "atlas:declared-direct", "value": "false"}],
        ):
            _reject("SBOM component properties are invalid")

        licenses = item.get("licenses")
        if licenses is not None:
            if (
                not isinstance(licenses, list)
                or len(licenses) != 1
                or not isinstance(licenses[0], dict)
                or set(licenses[0]) != {"license"}
                or not isinstance(licenses[0]["license"], dict)
                or set(licenses[0]["license"]) != {"id"}
                or _license_id(licenses[0]["license"]["id"]) is None
            ):
                _reject("SBOM component license is invalid")
    if names != sorted(names) or len(names) != len(set(names)):
        _reject("SBOM component order is not deterministic")
    expected_serial = "urn:uuid:" + str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            (
                f"https://github.com/{REPOSITORY}@{app_version}:"
                f"{_digest_payload(components)}"
            ),
        )
    )
    if serial != expected_serial:
        _reject("SBOM serial number does not match content")
    return dict(payload)


def validate_sbom_provenance(payload: object) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "authority",
        "generator",
        "repository",
        "source_revision",
        "source_clean",
        "python_implementation",
        "python_version",
        "component_count",
        "declared_direct_dependency_count",
        "requirements_sha256",
        "third_party_sha256",
        "sbom_filename",
        "sbom_sha256",
        "sbom_serial_number",
        "provenance_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("SBOM provenance schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != PROVENANCE_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("generator") != GENERATOR
        or payload.get("repository") != REPOSITORY
        or payload.get("source_clean") is not True
        or payload.get("sbom_filename") != BOM_FILENAME
    ):
        _reject("SBOM provenance identity is invalid")
    head = payload.get("source_revision")
    if not isinstance(head, str) or _HEAD.fullmatch(head) is None:
        _reject("SBOM provenance source revision is invalid")
    for key in (
        "requirements_sha256",
        "third_party_sha256",
        "sbom_sha256",
        "provenance_digest",
    ):
        value = payload.get(key)
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            _reject(f"SBOM provenance {key} is invalid")
    for key in ("component_count", "declared_direct_dependency_count"):
        value = payload.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= _MAX_COMPONENTS
        ):
            _reject(f"SBOM provenance {key} is invalid")
    implementation = payload.get("python_implementation")
    python_version = payload.get("python_version")
    if (
        not isinstance(implementation, str)
        or _NAME.fullmatch(implementation) is None
        or not isinstance(python_version, str)
        or _VERSION.fullmatch(python_version) is None
    ):
        _reject("SBOM provenance Python runtime is invalid")
    serial = payload.get("sbom_serial_number")
    if not isinstance(serial, str) or not serial.startswith("urn:uuid:"):
        _reject("SBOM provenance serial number is invalid")
    try:
        uuid.UUID(serial.removeprefix("urn:uuid:"))
    except (ValueError, AttributeError) as exc:
        raise ValidationError("SBOM provenance serial number is invalid") from exc
    basis = {
        key: value
        for key, value in payload.items()
        if key != "provenance_digest"
    }
    if _digest_payload(basis) != payload["provenance_digest"]:
        _reject("SBOM provenance digest mismatch")
    return dict(payload)


def validate_sbom_bundle(
    bundle_dir: Path,
    *,
    repo_root: Path | None = None,
    require_current_source: bool = False,
    require_current_runtime: bool = False,
) -> dict[str, object]:
    bundle = bundle_dir.resolve()
    if bundle_dir.is_symlink() or not bundle.is_dir():
        _reject("SBOM bundle directory is unsafe")
    sbom_path = bundle / BOM_FILENAME
    provenance_path = bundle / PROVENANCE_FILENAME
    for artifact, label in (
        (sbom_path, "CycloneDX artifact"),
        (provenance_path, "provenance artifact"),
    ):
        if artifact.is_symlink() or not artifact.is_file():
            _reject(f"SBOM {label} is unavailable")
        try:
            raw = artifact.read_bytes()
        except OSError as exc:
            raise ValidationError(f"SBOM {label} is unreadable") from exc
        if len(raw) > _MAX_ARTIFACT_BYTES:
            _reject(f"SBOM {label} exceeds bounded size")

    try:
        sbom_raw = sbom_path.read_bytes()
        provenance_raw = provenance_path.read_bytes()
        sbom = json.loads(sbom_raw.decode("utf-8"))
        provenance = json.loads(provenance_raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("SBOM bundle JSON is invalid") from exc
    sbom = validate_cyclonedx_sbom(sbom)
    provenance = validate_sbom_provenance(provenance)
    if _digest_bytes(sbom_raw) != provenance["sbom_sha256"]:
        _reject("SBOM artifact digest does not match provenance")
    if sbom["serialNumber"] != provenance["sbom_serial_number"]:
        _reject("SBOM serial number does not match provenance")
    if len(sbom["components"]) != provenance["component_count"]:
        _reject("SBOM component count does not match provenance")
    app = sbom["metadata"]["component"]
    if app["version"] != provenance["source_revision"]:
        _reject("SBOM source revision does not match provenance")
    direct_count = sum(
        item["properties"][0]["value"] == "true"
        for item in sbom["components"]
    )
    if direct_count != provenance["declared_direct_dependency_count"]:
        _reject("SBOM direct dependency count does not match provenance")

    if require_current_runtime and repo_root is None:
        _reject("SBOM current runtime validation requires repository root")

    if repo_root is not None:
        root = repo_root.resolve()
        if (
            _file_digest(root / "requirements.txt", label="requirements.txt")
            != provenance["requirements_sha256"]
        ):
            _reject("SBOM requirements provenance is stale")
        if (
            _file_digest(root / "THIRD_PARTY.md", label="THIRD_PARTY.md")
            != provenance["third_party_sha256"]
        ):
            _reject("SBOM third-party provenance is stale")
        source: dict[str, object] | None = None
        if require_current_source or require_current_runtime:
            source = _source_facts(root, require_clean=True)
            if source["source_revision"] != provenance["source_revision"]:
                _reject("SBOM source revision is stale")
        if require_current_runtime:
            expected_sbom, expected_provenance = (
                build_sbom_documents_from_facts(
                    root,
                    inventory=_runtime_inventory(),
                    source_facts=source
                    if source is not None
                    else _source_facts(root, require_clean=True),
                    python_facts=_python_facts(),
                )
            )
            if sbom != expected_sbom or provenance != expected_provenance:
                _reject("SBOM runtime evidence does not match current runtime")
    return {
        "state": "VALIDATED_EVIDENCE",
        "authority": AUTHORITY,
        "repository": provenance["repository"],
        "source_revision": provenance["source_revision"],
        "component_count": provenance["component_count"],
        "sbom_sha256": provenance["sbom_sha256"],
        "provenance_digest": provenance["provenance_digest"],
        "serial_number": provenance["sbom_serial_number"],
    }


def _write_json_new(path: Path, payload: object) -> None:
    if path.exists() or path.is_symlink():
        _reject("SBOM output file already exists")
    raw = _artifact_json_bytes(payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ValidationError("SBOM output file cannot be created") from exc
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def publish_sbom_bundle(
    repo_root: Path,
    dest_dir: Path,
) -> dict[str, object]:
    if not dest_dir.is_absolute():
        _reject("SBOM destination must be an absolute path")
    repo = repo_root.resolve()
    if dest_dir.is_relative_to(repo):
        _reject("SBOM destination must be outside the repository")
    parent = dest_dir.parent
    if parent.resolve() != parent or not parent.is_dir():
        _reject("SBOM destination parent is unsafe")
    if dest_dir.exists() or dest_dir.is_symlink():
        _reject("SBOM destination already exists")

    # Prove the exact clean source and runtime inventory before creating any
    # artifact path. Build into a sibling temporary directory, validate that
    # complete bundle, then expose it with one same-filesystem rename.
    sbom, provenance = build_sbom_documents(repo_root)
    temp_dir: Path | None = None
    published = False
    try:
        temp_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{dest_dir.name}.partial-",
                dir=str(parent),
            )
        )
        _write_json_new(temp_dir / BOM_FILENAME, sbom)
        _write_json_new(temp_dir / PROVENANCE_FILENAME, provenance)

        directory_fd = os.open(temp_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        summary = validate_sbom_bundle(
            temp_dir,
            repo_root=repo_root,
            require_current_source=True,
            require_current_runtime=True,
        )
        if dest_dir.exists() or dest_dir.is_symlink():
            _reject("SBOM destination already exists")
        os.rename(temp_dir, dest_dir)
        published = True

        parent_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return summary
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError("SBOM atomic publication failed") from exc
    finally:
        if temp_dir is not None and not published and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
