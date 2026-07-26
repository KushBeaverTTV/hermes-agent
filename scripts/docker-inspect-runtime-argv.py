#!/usr/bin/env python3
"""Convert non-secret Docker inspect runtime settings to structured docker-run argv."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def runtime_argv(inspect_data: list[dict[str, Any]]) -> list[str]:
    if len(inspect_data) != 1:
        raise ValueError(f"expected one container inspection, got {len(inspect_data)}")
    obj = inspect_data[0]
    host = obj.get("HostConfig") or {}
    config = obj.get("Config") or {}
    args: list[str] = ["--restart", "unless-stopped"]

    for bind in host.get("Binds") or []:
        args.extend(["-v", str(bind)])

    for mount in host.get("Mounts") or []:
        pieces = [f"type={mount['Type']}", f"src={mount['Source']}", f"dst={mount['Target']}"]
        if mount.get("ReadOnly"):
            pieces.append("readonly")
        if mount.get("Consistency"):
            pieces.append(f"consistency={mount['Consistency']}")
        args.extend(["--mount", ",".join(pieces)])

    for container_port, bindings in sorted((host.get("PortBindings") or {}).items()):
        for binding in bindings or []:
            host_ip = str(binding.get("HostIp") or "")
            host_port = str(binding.get("HostPort") or "")
            if not host_port:
                continue
            published = f"{host_port}:{container_port}"
            if host_ip and host_ip not in {"0.0.0.0", "::"}:
                published = f"{host_ip}:{published}"
            args.extend(["-p", published])

    container_name = str(obj.get("Name") or "").lstrip("/")
    networks = ((obj.get("NetworkSettings") or {}).get("Networks") or {})
    if networks:
        for network_name, settings in sorted(networks.items()):
            if network_name in {"default", "bridge"}:
                continue
            aliases = (settings or {}).get("Aliases") or []
            scoped = [f"name={network_name}"]
            for alias in aliases:
                value = str(alias or "").strip()
                if value and value != container_name:
                    scoped.append(f"alias={value}")
            args.extend(["--network", ",".join(scoped)])
    else:
        network = str(host.get("NetworkMode") or "")
        if network and network not in {"default", "bridge"}:
            args.extend(["--network", network])

    # Routing labels are public Traefik configuration. Preserve only that
    # allowlisted namespace so arbitrary metadata can never replay secrets.
    for key, value in sorted((config.get("Labels") or {}).items()):
        if str(key).startswith("traefik."):
            args.extend(["--label", f"{key}={value}"])

    for value in host.get("ExtraHosts") or []:
        args.extend(["--add-host", str(value)])
    for value in host.get("Dns") or []:
        args.extend(["--dns", str(value)])
    for value in host.get("CapAdd") or []:
        args.extend(["--cap-add", str(value)])
    for value in host.get("CapDrop") or []:
        args.extend(["--cap-drop", str(value)])
    for value in host.get("SecurityOpt") or []:
        args.extend(["--security-opt", str(value)])
    for key, value in sorted((host.get("Tmpfs") or {}).items()):
        args.extend(["--tmpfs", f"{key}:{value}" if value else str(key)])
    if host.get("Privileged"):
        args.append("--privileged")
    if host.get("ReadonlyRootfs"):
        args.append("--read-only")

    devices = host.get("Devices") or []
    for device in devices:
        spec = f"{device['PathOnHost']}:{device['PathInContainer']}"
        permissions = device.get("CgroupPermissions")
        if permissions:
            spec += f":{permissions}"
        args.extend(["--device", spec])

    return args


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("json", "nul"), default="json")
    ns = parser.parse_args()
    data = json.load(sys.stdin)
    args = runtime_argv(data)
    if ns.format == "json":
        json.dump(args, sys.stdout)
        sys.stdout.write("\n")
    else:
        sys.stdout.buffer.write(b"\0".join(arg.encode() for arg in args) + b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
