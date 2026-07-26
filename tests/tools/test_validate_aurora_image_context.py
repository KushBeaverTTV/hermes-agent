from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts/validate-aurora-image-context.py"
REQUIRED = (
    "agent/redact.py",
    ".dockerignore",
    "Dockerfile",
    "Dockerfile.aurora",
    "pyproject.toml",
    "uv.lock",
    "docker/aurora-main-wrapper.sh",
    "docker/aurora-startup-check.sh",
    "docker/aurora-gateway-healthcheck.py",
    "docker/aurora-deploy-runbook.sh",
    "scripts/docker-inspect-runtime-argv.py",
    "scripts/verify-aurora-vendor-manifest.py",
    "vendor/AURORA-VENDOR-MANIFEST.json",
    "vendor/mnemosyne/mnemosyne_memory-3.14.2-py3-none-any.whl",
    "vendor/mnemosyne/mnemosyne_hermes-0.5.2-py3-none-any.whl",
    "vendor/sqlite/sqlite-autoconf-3510300.tar.gz",
)


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "context"
    for rel in REQUIRED:
        source = ROOT / rel
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "--", *REQUIRED], cwd=root, check=True)
    return root


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(root)],
        text=True,
        capture_output=True,
    )

def test_clean_minimal_context_passes(tmp_path):
    root = _fixture(tmp_path)
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "AURORA-IMAGE-CONTEXT PASS" in result.stdout


def test_clean_export_without_git_metadata_passes(tmp_path):
    root = _fixture(tmp_path)
    shutil.rmtree(root / ".git")
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "AURORA-IMAGE-CONTEXT PASS" in result.stdout


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("hash", "HASH FAIL"),
        ("missing", "REQUIRED FILE FAIL"),
        ("secret", "SECRET-SCAN FAIL"),
    ],
)
def test_validator_fails_closed_on_tamper_missing_or_secret(tmp_path, mutation, expected):
    root = _fixture(tmp_path)
    if mutation == "hash":
        wheel = root / "vendor/mnemosyne/mnemosyne_memory-3.14.2-py3-none-any.whl"
        wheel.write_bytes(wheel.read_bytes() + b"tamper")
    elif mutation == "missing":
        (root / "docker/aurora-main-wrapper.sh").unlink()
    else:
        leak = root / "leak.py"
        leak.write_text(
            "OPENAI_API_KEY = "
            + repr("sk-" + "auroralive0123456789abcdef")
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "--", "leak.py"], cwd=root, check=True)

    result = _run(root)
    assert result.returncode != 0
    assert expected in result.stdout + result.stderr
