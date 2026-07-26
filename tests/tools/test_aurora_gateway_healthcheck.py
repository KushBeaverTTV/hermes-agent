from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "docker/aurora-gateway-healthcheck.py"
spec = importlib.util.spec_from_file_location("aurora_gateway_healthcheck", SCRIPT)
assert spec is not None
assert spec.loader is not None
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def _write_proc(
    root: Path,
    pid: int,
    argv: list[str],
    *,
    state: str = "S",
    start_time: str = "100",
) -> None:
    proc = root / str(pid)
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    # /proc/<pid>/stat field 3 is state; field 22 is process start time.
    tail = [state, *(["1"] * 18), start_time]
    (proc / "stat").write_text(
        f"{pid} (process name) {' '.join(tail)}\n",
        encoding="utf-8",
    )


def _write_ready(path: Path, *, pid: int, start_time: str) -> None:
    path.write_text(
        json.dumps({"pid": pid, "process_start_time": start_time}),
        encoding="utf-8",
    )


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
    _write_proc(
        tmp_path,
        12,
        ["/opt/hermes/.venv/bin/hermes", "gateway"],
        state="Z",
    )
    _write_proc(tmp_path, 13, ["/opt/hermes/.venv/bin/hermes", "gateway"])
    assert health.live_gateway_pids(tmp_path) == [13]


def test_health_requires_one_live_gateway_with_matching_readiness(tmp_path):
    marker = tmp_path / "gateway.ready.json"
    _write_proc(
        tmp_path,
        13,
        ["/opt/hermes/.venv/bin/hermes", "gateway"],
        start_time="777",
    )
    _write_ready(marker, pid=13, start_time="777")

    assert health.is_healthy(tmp_path, marker) is True


def test_missing_stale_or_corrupt_readiness_is_unhealthy(tmp_path):
    marker = tmp_path / "gateway.ready.json"
    _write_proc(
        tmp_path,
        13,
        ["/opt/hermes/.venv/bin/hermes", "gateway"],
        start_time="777",
    )
    assert health.is_healthy(tmp_path, marker) is False

    _write_ready(marker, pid=13, start_time="old")
    assert health.is_healthy(tmp_path, marker) is False

    marker.write_text("not-json", encoding="utf-8")
    assert health.is_healthy(tmp_path, marker) is False


def test_duplicate_live_gateways_are_unhealthy_even_with_one_marker(tmp_path):
    marker = tmp_path / "gateway.ready.json"
    for pid in (13, 14):
        _write_proc(
            tmp_path,
            pid,
            ["/opt/hermes/.venv/bin/hermes", "gateway"],
            start_time=str(pid * 10),
        )
    _write_ready(marker, pid=13, start_time="130")

    assert health.is_healthy(tmp_path, marker) is False


def test_stopped_gateway_is_unhealthy_even_with_matching_readiness(tmp_path):
    marker = tmp_path / "gateway.ready.json"
    _write_proc(
        tmp_path,
        13,
        ["/opt/hermes/.venv/bin/hermes", "gateway"],
        state="T",
        start_time="777",
    )
    _write_ready(marker, pid=13, start_time="777")

    assert health.live_gateway_pids(tmp_path) == []
    assert health.is_healthy(tmp_path, marker) is False


def test_dead_gateway_is_unhealthy(tmp_path):
    marker = tmp_path / "gateway.ready.json"
    _write_proc(tmp_path, 10, ["sleep", "infinity"])
    assert health.live_gateway_pids(tmp_path) == []
    assert health.is_healthy(tmp_path, marker) is False
