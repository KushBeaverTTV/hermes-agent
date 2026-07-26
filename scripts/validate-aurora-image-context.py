#!/usr/bin/env python3
"""Fail-closed static preflight for the Aurora custom image build context."""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "vendor/mnemosyne/mnemosyne_memory-3.14.2-py3-none-any.whl": "6217aafb85796e783610a234d8a3762bf46835e7f64c0910703949d7ace6ce7e",
    "vendor/mnemosyne/mnemosyne_hermes-0.5.2-py3-none-any.whl": "af028d0dc8b2a4f52c17f9767099881b00a58366805908c00855e360f0bf1a2a",
    "vendor/sqlite/sqlite-autoconf-3510300.tar.gz": "81f5be397049b0cae1b167f2225af7646fc0f82e4a9b3c48c9ea3a533e21d77a",
}
REQUIRED = [
    "Dockerfile",
    "Dockerfile.aurora",
    "docker/aurora-main-wrapper.sh",
    "docker/aurora-gateway-healthcheck.py",
    "docker/aurora-startup-check.sh",
    "docker/aurora-deploy-runbook.sh",
    "scripts/docker-inspect-runtime-argv.py",
    *EXPECTED,
]


def fail(message: str) -> None:
    print(f"IMAGE-CONTEXT-FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


missing = [p for p in REQUIRED if not (ROOT / p).is_file()]
if missing:
    fail(f"missing required paths: {missing}")

for rel, expected in EXPECTED.items():
    actual = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
    if actual != expected:
        fail(f"hash mismatch {rel}: expected={expected} actual={actual}")
    print(f"HASH PASS {rel} {actual}")

base = (ROOT / "Dockerfile").read_text(encoding="utf-8")
base_from = [line.strip() for line in base.splitlines() if line.lstrip().startswith("FROM ")]
unpinned = [line for line in base_from if "@sha256:" not in line]
if unpinned:
    fail(f"floating official base image(s): {unpinned}")
print(f"BASE-DIGEST PASS count={len(base_from)} sample={base_from[:2]}")

overlay = (ROOT / "Dockerfile.aurora").read_text(encoding="utf-8")
arg_pos = overlay.find("ARG AURORA_BASE=")
from_pos = overlay.find("FROM ${AURORA_BASE}")
if min(arg_pos, from_pos) < 0 or arg_pos > from_pos:
    fail("AURORA_BASE ARG is missing or not global before first FROM")
if 'CMD [ "gateway" ]' not in overlay:
    fail("custom image does not invoke exact hermes gateway subcommand")
if re.search(r"CMD\s*\[\s*\"gateway\"\s*,\s*\"run\"", overlay):
    fail("obsolete gateway run command present")
if 'ENTRYPOINT [ "/init", "/opt/aurora/main-wrapper.sh" ]' not in overlay:
    fail("s6 + Aurora startup wrapper ENTRYPOINT missing")
if "ARG AURORA_AUTHORITY_SHA" not in overlay or "/opt/aurora-authority-sha" not in overlay:
    fail("immutable authority build-SHA contract missing")
if "pgrep -f" in overlay or "/opt/aurora/gateway-healthcheck.py" not in overlay:
    fail("healthcheck is absent or can self-match through pgrep")
if "faster-whisper==1.2.1" not in overlay:
    fail("pinned local STT dependency missing")
if " -m pip" in overlay:
    fail("overlay uses pip, but the stock uv venv has no pip module")
if "--no-cache-dir" in overlay:
    fail("overlay uses pip-only --no-cache-dir instead of uv --no-cache")
if "uv pip install --python /opt/hermes/.venv/bin/python3" not in overlay:
    fail("overlay does not install dependencies through the stock uv toolchain")
if "sqlite-autoconf-3510300.tar.gz" not in overlay:
    fail("SQLite fixed source build missing")
print("OVERLAY-WIRING PASS command=hermes gateway watchdog=docker-restart+s6")

runbook = (ROOT / "docker/aurora-deploy-runbook.sh").read_text(encoding="utf-8")
if not runbook.startswith("#!/usr/bin/env bash\n"):
    fail("deploy/rollback runbook is executable but lacks a Bash shebang")
for needle in (
    '--platform "$PLATFORM"',
    "WhisperModel('small'",
    "docker inspect",
    "git status --porcelain=v1 --untracked-files=all",
    "git archive --format=tar HEAD",
    "scripts/docker-inspect-runtime-argv.py --format nul",
    "docker rename",
    "--rollback",
    "image-id.txt",
    'BASE_IMAGE="hermes-upstream:${GIT_SHA}"',
):
    if needle not in runbook:
        fail(f"runbook missing: {needle}")
print("RUNBOOK-WIRING PASS")

runtime_helper = (ROOT / "scripts/docker-inspect-runtime-argv.py").read_text(encoding="utf-8")
if '["--restart", "unless-stopped"]' not in runtime_helper:
    fail("runtime argv helper does not enforce unless-stopped restart")
print("RUNTIME-ARGV PASS structured no-secret replay")

dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
for pattern in (".env", ".env.*", ".hermes/", "auth.json", "*.db", "*.log"):
    if pattern not in dockerignore:
        fail(f"dockerignore missing sensitive-context exclusion: {pattern}")
print("BUILD-CONTEXT PASS committed git archive + sensitive excludes")

# Secret-value scan: policy variable names are allowed, assignments with values are not.
scan_paths = [ROOT / "Dockerfile.aurora"] + [ROOT / p for p in REQUIRED if p.endswith(".sh")]
secret_assignment = re.compile(
    r"(?:API_KEY|TOKEN|PASSWORD|SECRET)\s*=\s*['\"]?(?!\$\{|\$|['\"]?$)[A-Za-z0-9_./+:-]{8,}",
    re.IGNORECASE,
)
for path in scan_paths:
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if secret_assignment.search(line):
            fail(f"possible embedded secret {path.relative_to(ROOT)}:{line_no}")
print("SECRET-SCAN PASS no embedded assignments")
print("AURORA-IMAGE-CONTEXT PASS")
