#!/usr/bin/env python3
"""Container healthcheck for the exact Hermes gateway process."""
from __future__ import annotations

import sys
from pathlib import Path


def is_gateway_argv(argv: list[str]) -> bool:
    """True only when an argv element named hermes is followed by gateway."""
    return any(
        Path(arg).name == "hermes" and index + 1 < len(argv) and argv[index + 1] == "gateway"
        for index, arg in enumerate(argv)
    )


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
            # /proc/PID/stat: state is the first field after the final ')'.
            stat_tail = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].strip()
            state = stat_tail.split(None, 1)[0]
        except (OSError, IndexError):
            continue
        if state != "Z" and is_gateway_argv(argv):
            found.append(int(entry.name))
    return sorted(found)


def main() -> int:
    pids = live_gateway_pids()
    if not pids:
        print("AURORA-HEALTH: gateway process missing", file=sys.stderr)
        return 1
    print(f"AURORA-HEALTH: gateway pids={pids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
