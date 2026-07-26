#!/usr/bin/env python3
"""Fail-closed static preflight for an Aurora custom-image build context."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import subprocess
import sys


REQUIRED = (
    ".dockerignore",
    "Dockerfile",
    "Dockerfile.aurora",
    "pyproject.toml",
    "uv.lock",
    "docker/aurora-main-wrapper.sh",
    "docker/aurora-gateway-healthcheck.py",
    "docker/aurora-startup-check.sh",
    "docker/aurora-deploy-runbook.sh",
    "scripts/docker-inspect-runtime-argv.py",
    "scripts/verify-aurora-vendor-manifest.py",
    "vendor/AURORA-VENDOR-MANIFEST.json",
)
_TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}
_PLACEHOLDER_MARKERS = (
    "dummy",
    "example",
    "fake",
    "redacted",
    "replace",
    "your-",
    "your_",
    "xxxxx",
)
_PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_DOCUMENTATION_SECRET_SCAN_EXCLUSIONS = (
    Path("optional-skills/mlops/training/unsloth/references"),
    Path("website/docs"),
    Path("website/i18n"),
)
_SECRET_VALUE_CONTEXT = re.compile(
    r"(?:[A-Za-z0-9_.-]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|AUTH|CREDENTIAL|KEY)"
    r"[A-Za-z0-9_.-]*\s*(?:=|:)\s*['\"]?|Bearer\s+['\"]?)$",
    re.IGNORECASE,
)


class ImageContextError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ImageContextError(message)


def _tracked_text_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        rels = [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]
        candidates = [root / rel for rel in rels]
    else:
        # A git archive is intentionally metadata-free. Traverse only files
        # physically present in that exported context and never follow symlinks.
        candidates = sorted(
            (path for path in root.rglob("*") if path.is_file() and not path.is_symlink()),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    paths: list[Path] = []
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if rel.name.startswith("Dockerfile") or rel.suffix.lower() in _TEXT_SUFFIXES:
            paths.append(path)
    return paths


def _credential_prefix_regex(root: Path) -> re.Pattern[str]:
    """Load the runtime redactor's prefix authority without importing project deps."""

    source = (root / "agent/redact.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    patterns: list[str] | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_PREFIX_PATTERNS"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                patterns = value
                break
    if not patterns:
        raise ImageContextError("SECRET-SCAN FAIL redactor prefix authority unavailable")
    return re.compile(r"(?<![A-Za-z0-9_-])(?:" + "|".join(patterns) + r")(?![A-Za-z0-9_-])")


def validate(root: Path) -> None:
    root = root.resolve()
    missing = [rel for rel in REQUIRED if not (root / rel).is_file()]
    _require(not missing, f"REQUIRED FILE FAIL missing={missing}")

    vendor = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/verify-aurora-vendor-manifest.py"),
            "--root",
            str(root),
        ],
        text=True,
        capture_output=True,
    )
    if vendor.returncode != 0:
        detail = (vendor.stdout + vendor.stderr).strip()
        raise ImageContextError(detail or "VENDOR MANIFEST FAIL")
    print(vendor.stdout.strip())

    base = (root / "Dockerfile").read_text(encoding="utf-8")
    base_from = [line.strip() for line in base.splitlines() if line.lstrip().startswith("FROM ")]
    unpinned = [line for line in base_from if "@sha256:" not in line]
    _require(not unpinned, f"BASE-DIGEST FAIL floating official base image(s): {unpinned}")
    print(f"BASE-DIGEST PASS count={len(base_from)} sample={base_from[:2]}")

    overlay = (root / "Dockerfile.aurora").read_text(encoding="utf-8")
    arg_pos = overlay.find("ARG AURORA_BASE=")
    from_pos = overlay.find("FROM ${AURORA_BASE}")
    _require(
        min(arg_pos, from_pos) >= 0 and arg_pos < from_pos,
        "OVERLAY FAIL AURORA_BASE ARG is missing or not global before first FROM",
    )
    _require('CMD [ "gateway" ]' in overlay, "OVERLAY FAIL exact gateway CMD missing")
    _require(
        re.search(r"CMD\s*\[\s*\"gateway\"\s*,\s*\"run\"", overlay) is None,
        "OVERLAY FAIL obsolete gateway run command present",
    )
    _require(
        'ENTRYPOINT [ "/init", "/opt/aurora/main-wrapper.sh" ]' in overlay,
        "OVERLAY FAIL s6 + Aurora wrapper ENTRYPOINT missing",
    )
    _require(
        "ARG AURORA_AUTHORITY_SHA" in overlay and "/opt/aurora-authority-sha" in overlay,
        "OVERLAY FAIL immutable authority SHA contract missing",
    )
    for label in (
        "org.opencontainers.image.revision",
        "org.opencontainers.image.created",
        "org.opencontainers.image.source",
    ):
        _require(label in overlay, f"OVERLAY FAIL OCI label missing: {label}")
    _require(
        "pgrep -f" not in overlay and "/opt/aurora/gateway-healthcheck.py" in overlay,
        "OVERLAY FAIL healthcheck absent or self-matching",
    )
    _require(
        "uv sync --frozen --inexact --no-dev --extra voice" in overlay,
        "OVERLAY FAIL locked non-pruning voice dependency sync missing",
    )
    _require(
        "test -x /opt/hermes/.venv/bin/hermes" in overlay and "hermes_cli.__file__" in overlay,
        "OVERLAY FAIL post-sync Hermes project install proof missing",
    )
    _require(
        "uv pip install --system faster-whisper" not in overlay,
        "OVERLAY FAIL unpinned faster-whisper install present",
    )
    _require(" -m pip" not in overlay, "OVERLAY FAIL pip module invocation present")
    _require(
        "sqlite-autoconf-3510300.tar.gz" in overlay,
        "OVERLAY FAIL fixed SQLite source build missing",
    )
    print("OVERLAY-WIRING PASS command=hermes gateway readiness=s6+pid-start-marker")

    runbook = (root / "docker/aurora-deploy-runbook.sh").read_text(encoding="utf-8")
    _require(
        runbook.startswith("#!/usr/bin/env bash\n"),
        "RUNBOOK FAIL executable lacks Bash shebang",
    )
    for needle in (
        'MODE="${AURORA_DEPLOY_MODE:-plan}"',
        "plan)",
        "prepare)",
        "cutover)",
        "AURORA_CUTOVER_APPROVED",
        'BASE_IMAGE="hermes-upstream:${GIT_SHA}"',
        "git status --porcelain=v1 --untracked-files=all",
        "git archive --format=tar HEAD",
        "scripts/docker-inspect-runtime-argv.py --format nul",
        "--rollback",
        "image-id.txt",
    ):
        _require(needle in runbook, f"RUNBOOK FAIL missing: {needle}")
    print("RUNBOOK-WIRING PASS default=plan cutover=approval-gated")

    runtime_helper = (root / "scripts/docker-inspect-runtime-argv.py").read_text(
        encoding="utf-8"
    )
    _require(
        '["--restart", "unless-stopped"]' in runtime_helper,
        "RUNTIME-ARGV FAIL unless-stopped enforcement missing",
    )
    print("RUNTIME-ARGV PASS structured no-secret replay")

    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
    for pattern in (".env", ".env.*", ".hermes/", "auth.json", "*.db", "*.log"):
        _require(
            pattern in dockerignore,
            f"BUILD-CONTEXT FAIL missing sensitive exclusion: {pattern}",
        )
    print("BUILD-CONTEXT PASS committed git archive + sensitive excludes")

    credential_prefix = _credential_prefix_regex(root)
    try:
        scan_paths = _tracked_text_paths(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ImageContextError(f"SECRET-SCAN FAIL git inventory: {type(exc).__name__}") from exc
    for path in scan_paths:
        relative = path.relative_to(root)
        if relative == Path("agent/redact.py"):
            # This file defines the credential signatures used by this scan.
            continue
        if any(
            relative == prefix or prefix in relative.parents
            for prefix in _DOCUMENTATION_SECRET_SCAN_EXCLUSIONS
        ):
            # Generated/reference docs contain explicit placeholder credentials.
            continue
        if relative.parts and relative.parts[0] == "tests":
            # Test credentials are fixtures; production source is scanned below.
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(lines, 1):
            if "# aurora-secret-scan: ok" in line:
                continue
            matches = list(credential_prefix.finditer(line))
            real_matches = [
                match
                for match in matches
                if _SECRET_VALUE_CONTEXT.search(line[: match.start()])
                and not any(marker in match.group(0).lower() for marker in _PLACEHOLDER_MARKERS)
            ]
            if real_matches or (
                relative != Path("agent/redact.py") and _PRIVATE_KEY_HEADER.search(line)
            ):
                raise ImageContextError(
                    f"SECRET-SCAN FAIL possible embedded credential "
                    f"{relative}:{line_no}"
                )
    print(f"SECRET-SCAN PASS tracked_text_files={len(scan_paths)}")
    print("AURORA-IMAGE-CONTEXT PASS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        validate(args.root)
    except ImageContextError as exc:
        print(f"IMAGE-CONTEXT-FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
