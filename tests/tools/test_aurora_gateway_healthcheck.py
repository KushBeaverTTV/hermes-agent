from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "docker/aurora-gateway-healthcheck.py"
spec = importlib.util.spec_from_file_location("aurora_gateway_healthcheck", SCRIPT)
assert spec is not None
assert spec.loader is not None
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def _write_proc(root: Path, pid: int, argv: list[str], state: str = "S") -> None:
    proc = root / str(pid)
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    (proc / "stat").write_text(f"{pid} (process name) {state} 1 2 3\n")


def test_gateway_argv_matches_exact_argument_pair():
    assert health.is_gateway_argv([
        "/opt/hermes/.venv/bin/python3",
        "/opt/hermes/.venv/bin/hermes",
        "gateway",
    ])
    assert not health.is_gateway_argv([
        "/usr/bin/python3",
        "/opt/aurora/gateway-healthcheck.py",
    ])
    assert not health.is_gateway_argv(["sh", "-c", "pgrep -f hermes gateway"])


def test_live_gateway_probe_ignores_self_unrelated_and_zombie(tmp_path):
    _write_proc(tmp_path, 10, ["/usr/bin/python3", "/opt/aurora/gateway-healthcheck.py"])
    _write_proc(tmp_path, 11, ["/opt/hermes/.venv/bin/hermes", "chat"])
    _write_proc(tmp_path, 12, ["/opt/hermes/.venv/bin/hermes", "gateway"], state="Z")
    _write_proc(tmp_path, 13, ["/opt/hermes/.venv/bin/hermes", "gateway"])
    assert health.live_gateway_pids(tmp_path) == [13]


def test_dead_gateway_is_unhealthy(tmp_path):
    _write_proc(tmp_path, 10, ["sleep", "infinity"])
    assert health.live_gateway_pids(tmp_path) == []
