#!/usr/bin/env python3
"""Fail-closed validation for Aurora manual rollback receipts."""
from __future__ import annotations

import argparse
from pathlib import Path


def _read_exact(receipt_dir: Path, name: str) -> str:
    path = receipt_dir / name
    if not path.is_file():
        raise ValueError(f"missing {name}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"empty {name}")
    return value


def validate(
    receipt_dir: Path,
    *,
    deployment_sha: str,
    expected_container: str,
    preserved_image: str,
) -> None:
    receipt_sha = _read_exact(receipt_dir, "authority-sha.txt")
    receipt_container = _read_exact(receipt_dir, "container-name.txt")
    receipt_image = _read_exact(receipt_dir, "previous-image-id.txt")
    if receipt_sha != deployment_sha:
        raise ValueError("authority SHA mismatch")
    if receipt_container != expected_container:
        raise ValueError("rollback container mismatch")
    if receipt_image != preserved_image:
        raise ValueError("preserved image mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--deployment-sha", required=True)
    parser.add_argument("--expected-container", required=True)
    parser.add_argument("--preserved-image", required=True)
    args = parser.parse_args()
    try:
        validate(
            args.receipt_dir,
            deployment_sha=args.deployment_sha,
            expected_container=args.expected_container,
            preserved_image=args.preserved_image,
        )
    except (OSError, ValueError) as exc:
        print(f"ROLLBACK RECEIPT INVALID: {exc}")
        return 1
    print("ROLLBACK RECEIPT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
