"""Build one immutable, offline, owner-operated Atlas release bundle.

Development host only. Never connects to or modifies production.
The included deploy.py checks artifacts again before any privileged action.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

APP_SHA256 = "4cd01be9f5d3f97ca2739087855a70a680fc15654344217d34c5b3c2f104069b"
TRACKED_SHA256 = "ebc19d743236f7f27f6dd8484950bc14d1000949d36febc8e1b6a1282b06f49c"
OPERATOR_SHA256 = "604728305e8e24cad2547f053ce7120bb923d9133b5a9b3afbc24237a8a69789"
FROZEN = "d561eed138d56b6d3878048f66d08c7c5b4723cb"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def _add_archive(tar: tarfile.TarFile, path: Path, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    if path.is_dir():
        info.type = tarfile.DIRTYPE
        info.mode = 0o700
        tar.addfile(info)
        return
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("release artifact must be a regular file")
    info.size = path.stat().st_size
    info.mode = 0o700 if name in ("uv",) else 0o600
    with path.open("rb") as stream:
        tar.addfile(info, stream)


def build(repo: Path, artifacts: Path, uv: Path, output: Path) -> dict:
    if output.exists() or output.is_symlink():
        raise RuntimeError("REFUSED:bundle already exists")
    inputs = {
        "app.tar": artifacts / "atlas-integrated-d561eed.tar",
        "tracked.sha256": artifacts / "atlas-integrated-d561eed.sha256",
        "operator.py": artifacts / "atlas-ingress-operator-f260320.py",
        "requirements.lock": artifacts / "atlas-requirements-d561eed.lock",
        "uv": uv,
        "deploy.py": repo / "scripts/atlas_owner_release.py",
        "README.md": repo / "docs/runbooks/atlas-owner-release-bundle-20261019.md",
    }
    for name, path in inputs.items():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("missing or nonregular artifact: " + name)
    for name, digest in (
        ("app.tar", APP_SHA256),
        ("tracked.sha256", TRACKED_SHA256),
        ("operator.py", OPERATOR_SHA256),
    ):
        if sha(inputs[name]) != digest:
            raise RuntimeError("frozen release input has changed: " + name)
    wheels_dir = artifacts / "wheels-d561eed"
    if wheels_dir.is_symlink() or not wheels_dir.is_dir():
        raise RuntimeError("offline wheelhouse missing")
    wheels = sorted(wheels_dir.iterdir())
    if len(wheels) != 29 or any(not p.is_file() or p.is_symlink() or p.suffix != ".whl" for p in wheels):
        raise RuntimeError("wheelhouse must contain exactly 29 regular wheel files")
    all_entries = dict(inputs)
    for wheel in wheels:
        all_entries["wheels/" + wheel.name] = wheel
    manifest = {
        "schema": 1,
        "app_head": FROZEN,
        "files": {name: sha(path) for name, path in sorted(all_entries.items())},
    }
    with tempfile.TemporaryDirectory(prefix="atlas-owner-bundle-build-") as tmp:
        manifest_file = Path(tmp) / "bundle.json"
        manifest_file.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            _add_archive(tar, manifest_file, "bundle.json")
            _add_archive(tar, wheels_dir, "wheels/")
            for name, path in sorted(all_entries.items()):
                _add_archive(tar, path, name)
    return {
        "output": str(output),
        "sha256": sha(output),
        "files": len(all_entries) + 1,
        "wheels": 29,
        "application": FROZEN,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = build(args.repo, args.artifacts, args.uv, args.output)
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
