#!/usr/bin/env python3
"""Fail-closed verifier for Aurora's committed vendor artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile


MANIFEST_REL = Path("vendor/AURORA-VENDOR-MANIFEST.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_identity(path: Path) -> tuple[str | None, str | None]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_paths = sorted(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            )
            if len(metadata_paths) != 1:
                return None, None
            name = None
            version = None
            for line in archive.read(metadata_paths[0]).decode("utf-8").splitlines():
                if line.startswith("Name: "):
                    name = line[6:].strip()
                elif line.startswith("Version: "):
                    version = line[9:].strip()
                if name and version:
                    break
            return name, version
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return None, None


def verify(root: Path) -> list[str]:
    root = root.resolve()
    manifest_path = root / MANIFEST_REL
    errors: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"MANIFEST FAIL {MANIFEST_REL}: {type(exc).__name__}"]

    if manifest.get("schema") != "aurora-vendor-manifest-v1":
        errors.append("MANIFEST FAIL unsupported schema")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return errors + ["MANIFEST FAIL artifacts must be a non-empty list"]

    seen: set[str] = set()
    for row in artifacts:
        if not isinstance(row, dict):
            errors.append("MANIFEST FAIL artifact row is not an object")
            continue
        rel_text = str(row.get("path") or "")
        rel = Path(rel_text)
        if (
            not rel_text
            or rel.is_absolute()
            or ".." in rel.parts
            or rel_text in seen
        ):
            errors.append(f"PATH FAIL {rel_text or '<empty>'}")
            continue
        seen.add(rel_text)
        path = (root / rel).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            errors.append(f"PATH FAIL {rel_text}")
            continue
        if not path.is_file():
            errors.append(f"REQUIRED FILE FAIL {rel_text}")
            continue
        expected_size = row.get("size")
        if not isinstance(expected_size, int) or path.stat().st_size != expected_size:
            errors.append(
                f"SIZE FAIL {rel_text} expected={expected_size} actual={path.stat().st_size}"
            )
        expected_hash = str(row.get("sha256") or "")
        actual_hash = _sha256(path)
        if len(expected_hash) != 64 or actual_hash != expected_hash:
            errors.append(
                f"HASH FAIL {rel_text} expected={expected_hash} actual={actual_hash}"
            )
        if row.get("kind") == "python-wheel":
            actual_name, actual_version = _wheel_identity(path)
            if (actual_name, actual_version) != (row.get("name"), row.get("version")):
                errors.append(
                    f"WHEEL METADATA FAIL {rel_text} "
                    f"expected={row.get('name')}=={row.get('version')} "
                    f"actual={actual_name}=={actual_version}"
                )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    errors = verify(args.root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    manifest = json.loads((args.root.resolve() / MANIFEST_REL).read_text(encoding="utf-8"))
    print(f"AURORA-VENDOR-MANIFEST PASS count={len(manifest['artifacts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
