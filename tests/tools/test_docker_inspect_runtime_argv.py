from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/docker-inspect-runtime-argv.py"
spec = importlib.util.spec_from_file_location("docker_inspect_runtime_argv", SCRIPT)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_runtime_argv_preserves_non_secret_runtime_contract_as_list():
    inspected = [{
        "HostConfig": {
            "Binds": ["/opt/data:/opt/data:rw"],
            "Mounts": [],
            "PortBindings": {"4860/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4860"}]},
            "NetworkMode": "hostnet",
            "ExtraHosts": ["example:127.0.0.1"],
            "Dns": ["1.1.1.1"],
            "CapAdd": ["NET_ADMIN"],
            "CapDrop": None,
            "SecurityOpt": ["no-new-privileges"],
            "Tmpfs": {"/run": "rw,noexec"},
            "Privileged": False,
            "ReadonlyRootfs": True,
            "Devices": [{
                "PathOnHost": "/dev/net/tun",
                "PathInContainer": "/dev/net/tun",
                "CgroupPermissions": "rwm",
            }],
        },
        "Config": {
            "Env": ["SECRET_TOKEN=must-not-leak"],
        },
    }]
    args = mod.runtime_argv(inspected)
    assert isinstance(args, list)
    assert args == [
        "--restart", "unless-stopped",
        "-v", "/opt/data:/opt/data:rw",
        "-p", "127.0.0.1:4860:4860/tcp",
        "--network", "hostnet",
        "--add-host", "example:127.0.0.1",
        "--dns", "1.1.1.1",
        "--cap-add", "NET_ADMIN",
        "--security-opt", "no-new-privileges",
        "--tmpfs", "/run:rw,noexec",
        "--read-only",
        "--device", "/dev/net/tun:/dev/net/tun:rwm",
    ]
    assert not any("SECRET" in value or "must-not-leak" in value for value in args)


def test_runtime_argv_rejects_missing_or_multiple_inspections():
    for invalid in ([], [{}, {}]):
        try:
            mod.runtime_argv(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {invalid!r}")
