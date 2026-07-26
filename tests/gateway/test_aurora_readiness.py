from __future__ import annotations

import json
from pathlib import Path

from gateway import readiness


def test_readiness_marker_binds_pid_start_time_and_adapters(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.os, "getpid", lambda: 42)
    monkeypatch.setattr(readiness, "get_process_start_time", lambda pid: "98765")

    marker = readiness.write_gateway_readiness(
        adapters={"telegram": object(), "discord": object()},
        home=tmp_path,
    )

    assert marker == tmp_path / "gateway.ready.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["pid"] == 42
    assert payload["process_start_time"] == "98765"
    assert payload["adapters"] == ["discord", "telegram"]
    assert marker.stat().st_mode & 0o777 == 0o600

    assert readiness.remove_gateway_readiness(home=tmp_path) is True
    assert not marker.exists()


def test_readiness_cleanup_does_not_delete_replacement_marker(tmp_path, monkeypatch):
    marker = tmp_path / "gateway.ready.json"
    marker.write_text(
        json.dumps({"pid": 99, "process_start_time": "replacement"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness.os, "getpid", lambda: 42)
    monkeypatch.setattr(readiness, "get_process_start_time", lambda pid: "old")

    assert readiness.remove_gateway_readiness(home=tmp_path) is False
    assert marker.exists()


def test_corrupt_readiness_marker_is_not_accepted(tmp_path):
    marker = tmp_path / "gateway.ready.json"
    marker.write_text("not-json", encoding="utf-8")
    assert readiness.read_gateway_readiness(marker) is None
