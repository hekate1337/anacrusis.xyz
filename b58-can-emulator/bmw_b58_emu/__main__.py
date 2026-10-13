"""Entry point: python -m bmw_b58_emu --iface vcan0"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from .api import make_server
from .broadcast import install_broadcast
from .bus import SocketCanBus
from .dme import Dme, DmeConfig
from .vcan import ensure_vcan
from .vehicle import Ignition, Vehicle


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bmw_b58_emu", description="BMW B58 DME + OBD-II CAN bus emulator")
    ap.add_argument("-i", "--iface", default="vcan0", help="SocketCAN interface (default vcan0)")
    ap.add_argument("--create-vcan", action="store_true", help="create/bring up the vcan interface first (needs root)")
    ap.add_argument("--api-host", default="127.0.0.1")
    ap.add_argument("--api-port", type=int, default=8080, help="control API port, 0 disables")
    ap.add_argument("--vin", help="17-character VIN")
    ap.add_argument("--state-file", help="JSON file persisting DTCs, VIN, coding and odometer")
    ap.add_argument("--ignition", choices=["off", "accessory", "on"], default="on")
    ap.add_argument("--start-engine", action="store_true", help="crank the engine immediately")
    ap.add_argument("--scenario", choices=["manual", "idle", "cruise", "wot", "cycle"], default="idle")
    ap.add_argument("--speed", type=float, default=100.0, help="target speed for --scenario cruise")
    ap.add_argument("--ambient", type=float, default=20.0, help="ambient temperature, degC")
    ap.add_argument("--thermal-scale", type=float, default=1.0, help="speed up warm-up/cool-down (e.g. 20)")
    ap.add_argument("--no-broadcast", action="store_true", help="disable cyclic PT-CAN frames (diagnostics only)")
    ap.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv log every CAN frame")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.verbose >= 1:
        logging.getLogger("bmw_b58_emu").setLevel(logging.INFO)
    if a.verbose >= 2:
        logging.getLogger("bmw_b58_emu.can").setLevel(logging.DEBUG)
    log = logging.getLogger("bmw_b58_emu")
    log.setLevel(min(log.level or logging.INFO, logging.INFO))

    try:
        if a.create_vcan:
            ensure_vcan(a.iface)
        bus = SocketCanBus(a.iface)
    except (OSError, RuntimeError) as e:
        print(f"error: cannot open CAN interface {a.iface!r}: {e}\n"
              f"hint: sudo scripts/setup_vcan.sh {a.iface}   (or pass --create-vcan as root)", file=sys.stderr)
        return 2

    cfg = DmeConfig()
    if a.vin:
        cfg.vin = a.vin.upper()
    vehicle = Vehicle(ambient=a.ambient, thermal_scale=a.thermal_scale)
    dme = Dme(bus, vehicle, cfg, a.state_file)
    if not a.no_broadcast:
        install_broadcast(dme)
    vehicle.driver.configure(a.scenario, a.speed)
    vehicle.set_ignition({"off": Ignition.OFF, "accessory": Ignition.ACCESSORY, "on": Ignition.ON}[a.ignition])
    if a.start_engine:
        vehicle.start_engine()
    dme.start()

    server = None
    if a.api_port:
        server = make_server(dme, a.api_host, a.api_port)
        threading.Thread(target=server.serve_forever, daemon=True, name="api").start()
    print(f"B58 DME emulator on {a.iface}: BMW diag 0x6F1->0x{0x600 + cfg.address:03X} (ECU 0x{cfg.address:02X}), OBD-II 0x7DF/0x7E0->0x7E8"
          + (f", API http://{a.api_host}:{a.api_port}/" if server else ""), flush=True)

    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    done.wait()
    if server:
        server.shutdown()
    dme.stop()
    bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
