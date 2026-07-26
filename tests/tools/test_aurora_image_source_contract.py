from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile.aurora"
RUNBOOK = ROOT / "docker/aurora-deploy-runbook.sh"
STARTUP = ROOT / "docker/aurora-startup-check.sh"
MANIFEST = ROOT / "vendor/AURORA-VENDOR-MANIFEST.json"
VERIFIER = ROOT / "scripts/verify-aurora-vendor-manifest.py"


def test_dockerfile_uses_locked_voice_extra_and_oci_provenance_labels():
    source = DOCKERFILE.read_text(encoding="utf-8")

    assert "uv sync --frozen --no-dev --extra voice" in source
    assert "uv pip install --system faster-whisper" not in source
    assert "org.opencontainers.image.revision" in source
    assert "org.opencontainers.image.created" in source
    assert "org.opencontainers.image.source" in source
    assert "USER root" in source
    assert "s6-setuidgid hermes" in source


def test_vendor_manifest_is_machine_verifiable():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema"] == "aurora-vendor-manifest-v1"
    assert len(manifest["artifacts"]) == 3
    assert all(len(row["sha256"]) == 64 for row in manifest["artifacts"])

    completed = subprocess.run(
        [sys.executable, str(VERIFIER), "--root", str(ROOT)],
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "AURORA-VENDOR-MANIFEST PASS count=3" in completed.stdout


def test_deploy_runbook_defaults_to_plan_and_guards_live_mutation():
    source = RUNBOOK.read_text(encoding="utf-8")
    default_mode = 'MODE="${AURORA_DEPLOY_MODE:-plan}"'
    approval_guard = 'AURORA_CUTOVER_APPROVED' 

    assert default_mode in source
    assert 'plan)' in source
    assert 'prepare)' in source
    assert 'cutover)' in source
    assert approval_guard in source
    guard_index = source.index(approval_guard)
    for mutation in ('docker stop "$CONTAINER"', 'docker rename "$CONTAINER"'):
        assert guard_index < source.index(mutation)
    assert "PLAN PASS: no build, stop, rename, run, restart, or cutover performed" in source


def test_deploy_runbook_derives_host_data_mount_instead_of_assuming_host_opt_data():
    source = RUNBOOK.read_text(encoding="utf-8")

    assert 'HOST_DATA_DIR="${AURORA_HOST_DATA_DIR:-}"' in source
    assert 'ENV_FILE="${AURORA_ENV_FILE:-}"' in source
    assert "resolve_host_paths" in source
    assert 'mount.get("Destination") == "/opt/data"' in source
    assert 'ENV_FILE="${ENV_FILE:-$HOST_DATA_DIR/.env}"' in source
    assert '-v "$HOST_DATA_DIR:/opt/data:rw"' in source
    assert '--env-file "$ENV_FILE"' in source
    assert '[[ -r /opt/data/.env ]]' not in source


def test_startup_check_resolves_custom_aurora_source_and_locked_stt():
    source = STARTUP.read_text(encoding="utf-8")
    assert "EXPECTED_GATEWAY_SOURCE=/opt/hermes/gateway/run.py" in source
    assert "gateway.run.__file__" in source
    assert "faster_whisper" in source
    assert "mnemosyne" in source
    assert "AURORA STARTUP CHECK PASS" in source
