#!/usr/bin/env python3
"""Container healthcheck for one initialized Hermes gateway process."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


def is_gateway_argv(argv: list[str]) -> bool:
    """True only when an argv element named hermes is followed by gateway."""

    return any(
        Path(arg).name == "hermes" and index + 1 < len(argv) and argv[index + 1] == "gateway"
        for index, arg in enumerate(argv)
    )


def _proc_state_and_start_time(stat_path: Path) -> tuple[str, str]:
    stat = stat_path.read_text(encoding="utf-8")
    close = stat.rfind(")")
    if close < 0:
        raise ValueError("malformed proc stat")
    fields = stat[close + 2 :].split()
    if len(fields) < 20:
        raise ValueError("proc stat lacks process start time")
    return fields[0], fields[19]


def live_gateway_pids(proc_root: Path = Path("/proc")) -> list[int]:
    found: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            argv = [
                part.decode("utf-8", errors="replace")
                for part in (entry / "cmdline").read_bytes().split(b"\0")
                if part
            ]
            state, _ = _proc_state_and_start_time(entry / "stat")
        except (OSError, IndexError, ValueError):
            continue
        if state != "Z" and is_gateway_argv(argv):
            found.append(int(entry.name))
    return sorted(found)


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("pid"), int):
        return None
    if not str(payload.get("process_start_time") or ""):
        return None
    return payload


def is_healthy(
    proc_root: Path = Path("/proc"),
    ready_file: Path | None = None,
) -> bool:
    if ready_file is None:
        home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
        ready_file = home / "gateway.ready.json"
    pids = live_gateway_pids(proc_root)
    if len(pids) != 1:
        return False
    marker = _read_marker(ready_file)
    if marker is None or marker.get("pid") != pids[0]:
        return False
    try:
        _, start_time = _proc_state_and_start_time(proc_root / str(pids[0]) / "stat")
    except (OSError, ValueError):
        return False
    return marker.get("process_start_time") == start_time


def main() -> int:
    pids = live_gateway_pids()
    if len(pids) != 1:
        print(
            f"AURORA-HEALTH: expected one live gateway, found {len(pids)}",
            file=sys.stderr,
        )
        return 1
    if not is_healthy():
        print("AURORA-HEALTH: readiness marker missing or stale", file=sys.stderr)
        return 1
    print(f"AURORA-HEALTH: ready gateway pid={pids[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
