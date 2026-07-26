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
ROLLBACK_VERIFIER = ROOT / "scripts/validate-aurora-rollback-receipt.py"


def test_dockerfile_uses_locked_voice_extra_and_oci_provenance_labels():
    source = DOCKERFILE.read_text(encoding="utf-8")

    assert "uv sync --frozen --inexact --no-dev --extra voice" in source
    assert "test -x /opt/hermes/.venv/bin/hermes" in source
    assert "hermes_cli.__file__" in source
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
    approval_guard = '[[ "$AURORA_CUTOVER_APPROVED" == "YES:$GIT_SHA" ]]'

    assert default_mode in source
    assert 'plan)' in source
    assert 'prepare)' in source
    assert 'cutover)' in source
    assert approval_guard in source
    guard_index = source.index(approval_guard)
    for mutation in ('docker stop "$CONTAINER"', 'docker rename "$CONTAINER"'):
        assert guard_index < source.index(mutation)
    assert "PLAN PASS: no build, stop, rename, run, restart, or cutover performed" in source


def test_manual_rollback_is_sha_bound_and_health_wait_exceeds_docker_budget():
    source = RUNBOOK.read_text(encoding="utf-8")

    assert 'AURORA_ROLLBACK_APPROVED="${AURORA_ROLLBACK_APPROVED:-}"' in source
    assert 'DEPLOYMENT_SHA="${2:-${AURORA_ROLLBACK_SHA:-$GIT_SHA}}"' in source
    assert '[[ "$AURORA_ROLLBACK_APPROVED" == "YES:$DEPLOYMENT_SHA" ]]' in source
    assert 'RECEIPT_DIR="$RECEIPT_ROOT/$DEPLOYMENT_SHA"' in source
    assert 'for _ in {1..48}; do' in source
    assert 'scripts/validate-aurora-rollback-receipt.py' in source


def test_rollback_receipt_verifier_rejects_each_identity_mismatch(tmp_path):
    receipt = tmp_path / "receipt"
    receipt.mkdir()
    sha = "a" * 40
    expected_container = f"hermes-rollback-{sha[:12]}"
    expected_image = "sha256:" + "b" * 64
    (receipt / "authority-sha.txt").write_text(sha + "\n")
    (receipt / "container-name.txt").write_text(expected_container + "\n")
    (receipt / "previous-image-id.txt").write_text(expected_image + "\n")

    base = [
        sys.executable,
        str(ROLLBACK_VERIFIER),
        "--receipt-dir", str(receipt),
        "--deployment-sha", sha,
        "--expected-container", expected_container,
        "--preserved-image", expected_image,
    ]
    assert subprocess.run(base, capture_output=True, text=True).returncode == 0

    cases = [
        ("authority-sha.txt", "c" * 40),
        ("container-name.txt", "wrong-container"),
        ("previous-image-id.txt", "sha256:" + "d" * 64),
    ]
    originals = {name: (receipt / name).read_text() for name, _ in cases}
    for name, wrong in cases:
        (receipt / name).write_text(wrong + "\n")
        failed = subprocess.run(base, capture_output=True, text=True)
        assert failed.returncode != 0, name
        (receipt / name).write_text(originals[name])


def test_deploy_runbook_derives_host_data_mount_instead_of_assuming_host_opt_data():
    source = RUNBOOK.read_text(encoding="utf-8")

    assert 'HOST_DATA_DIR="${AURORA_HOST_DATA_DIR:-}"' in source
    assert 'ENV_FILE="${AURORA_ENV_FILE:-}"' in source
    assert "resolve_host_paths" in source
    assert 'mount.get("Destination") == "/opt/data"' in source
    assert 'ENV_FILE="${ENV_FILE:-$HOST_DATA_DIR/.env}"' in source
    assert '-v "$HOST_DATA_DIR/cache/audio:/opt/data/cache/audio:ro"' in source
    assert '-v "$HOST_DATA_DIR/cache/huggingface:/opt/data/cache/huggingface:rw"' in source
    assert '-v "$HOST_DATA_DIR:/opt/data:rw"' not in source
    assert '--env-file "$ENV_FILE"' in source
    assert '[[ -r /opt/data/.env ]]' not in source


def test_startup_check_resolves_custom_aurora_source_and_locked_stt():
    source = STARTUP.read_text(encoding="utf-8")
    assert "EXPECTED_GATEWAY_SOURCE=/opt/hermes/gateway/run.py" in source
    assert "gateway.run.__file__" in source
    assert "GatewayAuthorizationMixin" in source
    assert "issubclass(GatewayRunner, GatewayAuthorizationMixin)" in source
    assert "owner_user_ids" in source
    assert "self._is_explicit_owner_source" in source
    assert 'grep -q "owner_supersedes = is_explicit_owner_source"' not in source
    assert '[ -x /opt/hermes/.venv/bin/hermes ]' in source
    assert "import hermes_cli" in source
    assert "faster_whisper" in source
    assert "mnemosyne" in source
    assert "AURORA STARTUP CHECK PASS" in source
