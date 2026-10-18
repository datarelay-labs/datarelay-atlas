"""Build a reproducible SHA256 release manifest from an immutable Git commit.

This is source-only preparation. It never touches the production host.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path


def _git(*args: str, cwd: Path) -> bytes:
    return subprocess.check_output(["git", *args], cwd=cwd)


def build_manifest(repo: Path, commit: str) -> tuple[bytes, int]:
    full = _git("rev-parse", "--verify", commit + "^{commit}", cwd=repo).decode().strip()
    if full != commit or len(full) != 40:
        raise ValueError("exact 40-character commit SHA required")
    tree = _git("ls-tree", "-r", "-z", full, cwd=repo)
    lines = []
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        header, name = entry.split(b"\t", 1)
        mode, kind, oid = header.split(b" ")
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("nonregular tracked release entry")
        if b"\n" in name or b"\r" in name or b"\\" in name:
            raise ValueError("unsafe tracked release filename")
        raw = _git("cat-file", "blob", oid.decode(), cwd=repo)
        lines.append(hashlib.sha256(raw).hexdigest().encode() + b"  " + name + b"\n")
    return b"".join(lines), len(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    content, count = build_manifest(args.repo, args.commit)
    if args.output.exists() or args.output.is_symlink():
        parser.error("refusing to overwrite an existing manifest")
    args.output.write_bytes(content)
    print(f"COMMIT={args.commit}")
    print(f"FILES={count}")
    print(f"MANIFEST_SHA256={hashlib.sha256(content).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
