"""Create a virtual SocketCAN interface (vcan) when run with enough privileges."""
from __future__ import annotations

import os
import shutil
import subprocess


def _run(cmd: list) -> subprocess.CompletedProcess:
    if os.geteuid() != 0 and shutil.which("sudo"):
        cmd = ["sudo", "-n"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True)


def ensure_vcan(name: str) -> None:
    """Idempotently create and bring up `name` as a vcan interface. Raises RuntimeError on failure."""
    if shutil.which("ip") is None:
        raise RuntimeError("`ip` (iproute2) not found; see scripts/setup_vcan.sh")
    if _run(["ip", "link", "show", name]).returncode == 0:
        _run(["ip", "link", "set", "up", name])
        return
    _run(["modprobe", "vcan"])
    r = _run(["ip", "link", "add", "dev", name, "type", "vcan"])
    if r.returncode != 0:
        raise RuntimeError(
            f"could not create {name}: {r.stderr.strip() or 'unknown error'}\n"
            f"Run: sudo modprobe vcan && sudo ip link add dev {name} type vcan && sudo ip link set up {name}"
        )
    r = _run(["ip", "link", "set", "up", name])
    if r.returncode != 0:
        raise RuntimeError(f"could not bring up {name}: {r.stderr.strip()}")
