"""Bounded, non-destructive readiness probes for authenticated health surfaces."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home


_DISK_DEGRADED_PERCENT = 90.0


def get_process_start_time(pid: int, proc_root: Path = Path("/proc")) -> str:
    """Return Linux /proc stat field 22 for PID-reuse-safe identity."""

    stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    close = stat.rfind(")")
    if close < 0:
        raise ValueError("malformed proc stat")
    fields = stat[close + 2 :].split()
    if len(fields) < 20:
        raise ValueError("proc stat lacks process start time")
    return fields[19]


def _gateway_ready_path(home: Path | None = None) -> Path:
    return (home if home is not None else get_hermes_home()) / "gateway.ready.json"


def read_gateway_readiness(path: Path) -> dict[str, Any] | None:
    """Read and minimally validate a readiness marker without raising."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("pid"), int):
        return None
    if not str(payload.get("process_start_time") or ""):
        return None
    return payload


def write_gateway_readiness(
    *,
    adapters: Mapping[Any, Any] | None = None,
    home: Path | None = None,
) -> Path:
    """Atomically publish that the initialized gateway reached its run loop."""

    pid = os.getpid()
    marker = _gateway_ready_path(home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "process_start_time": get_process_start_time(pid),
        "ready_at_unix": int(time.time()),
        "adapters": sorted(
            str(getattr(key, "value", key)) for key in (adapters or {}).keys()
        ),
    }
    temporary = marker.with_name(f".{marker.name}.{pid}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(marker)
    marker.chmod(0o600)
    return marker


def remove_gateway_readiness(*, home: Path | None = None) -> bool:
    """Remove only this process's marker, never a replacement's marker."""

    marker = _gateway_ready_path(home)
    payload = read_gateway_readiness(marker)
    if payload is None:
        return False
    pid = os.getpid()
    try:
        start_time = get_process_start_time(pid)
    except (OSError, ValueError):
        return False
    if payload.get("pid") != pid or payload.get("process_start_time") != start_time:
        return False
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


def _check(status: str, detail: str | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if detail:
        result["detail"] = detail
    result.update(extra)
    return result


def _probe_state_db(home: Path) -> dict[str, Any]:
    path = home / "state.db"
    if not path.exists():
        return _check("ok", "not initialized")
    try:
        # A readiness probe must never compete with normal state writers. A
        # read-only schema query still catches unreadable/corrupt databases
        # without taking a write reservation on every health poll.
        # ``closing(...)`` is required: sqlite3's connection context manager
        # only commits/rolls back — it never closes, so a bare ``with
        # sqlite3.connect(...)`` leaks one connection (and its fds) per
        # health poll in the long-running gateway (#69678/#69567 bug class).
        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return _check("ok")
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    if not path.exists():
        return _check("ok", "using defaults")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            return _check("degraded", "top level is not a mapping")
        return _check("ok")
    except Exception as exc:
        return _check("degraded", f"invalid config ({type(exc).__name__})")


def _probe_disk(home: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(home)
        used_pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
        status = "degraded" if used_pct >= _DISK_DEGRADED_PERCENT else "ok"
        return _check(status, used_percent=used_pct, free_bytes=usage.free)
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_gateway(runtime_status: dict[str, Any]) -> dict[str, Any]:
    state = str(runtime_status.get("gateway_state") or "unknown")
    platforms = runtime_status.get("platforms")
    connected = 0
    configured = 0
    if isinstance(platforms, dict):
        configured = len(platforms)
        connected = sum(
            1
            for value in platforms.values()
            if isinstance(value, dict)
            and str(value.get("state") or value.get("status") or "").lower()
            in {"connected", "running", "ok"}
        )
    status = "ok" if state in {"running", "draining"} else "degraded"
    return _check(status, state=state, connected_platforms=connected, platforms=configured)


def collect_runtime_readiness(
    *,
    configured_model: str,
    runtime_status: dict[str, Any] | None,
    active_api_runs: int = 0,
    process_completion_queue_depth: int = 0,
    active_delegations: int = 0,
) -> dict[str, Any]:
    """Return bounded readiness diagnostics without mutating runtime state.

    The detailed health endpoint is authenticated. Even there, probes expose
    status and counts only: never config values, credentials, paths, commands,
    queue payloads, or exception messages.
    """
    home = get_hermes_home()
    runtime = runtime_status if isinstance(runtime_status, dict) else {}
    checks = {
        "state_db": _probe_state_db(home),
        "config": _probe_config(home),
        "model": _check("ok" if str(configured_model or "").strip() else "degraded"),
        "disk": _probe_disk(home),
        "gateway": _probe_gateway(runtime),
        "background_queues": _check(
            "ok",
            active_api_runs=max(0, int(active_api_runs)),
            process_completions=max(0, int(process_completion_queue_depth)),
            active_delegations=max(0, int(active_delegations)),
        ),
    }
    overall = "ok" if all(item.get("status") == "ok" for item in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}


__all__ = [
    "collect_runtime_readiness",
    "get_process_start_time",
    "read_gateway_readiness",
    "remove_gateway_readiness",
    "write_gateway_readiness",
]
