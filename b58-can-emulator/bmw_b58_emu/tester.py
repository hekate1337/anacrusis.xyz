"""Diagnostic tester client: a minimal ISO-TP/UDS/OBD client plus a small CLI.

Handy for smoke-testing the emulator without external tools:

    python -m bmw_b58_emu.tester -i vcan0 obd 01 0C
    python -m bmw_b58_emu.tester -i vcan0 bmw 22 F1 90
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from typing import Optional

from .bus import Bus, Frame, SocketCanBus
from .isotp import IsoTpConfig, IsoTpEndpoint, IsoTpMessage


class TesterTimeout(Exception):
    pass


class DiagClient:
    def __init__(self, bus: Bus, cfg: IsoTpConfig, name: str = "tester") -> None:
        self.bus = bus
        self._rx: "queue.Queue[bytes]" = queue.Queue()
        self.ep = IsoTpEndpoint(cfg, bus.send, lambda m: self._rx.put(m.payload), name)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"{name}-rx")

    def start(self) -> "DiagClient":
        self.ep.start()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self.ep.stop()

    def __enter__(self) -> "DiagClient":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            f = self.bus.recv(0.02)
            if f is not None:
                self.ep.handle_frame(f)
            self.ep.poll()

    def request(self, payload: bytes, timeout: float = 2.0) -> bytes:
        while not self._rx.empty():
            self._rx.get_nowait()
        self.ep.send(payload)
        end = time.monotonic() + timeout
        while True:
            try:
                resp = self._rx.get(timeout=max(0.001, end - time.monotonic()))
            except queue.Empty:
                raise TesterTimeout(f"no response to {payload.hex(' ')}") from None
            if len(resp) == 3 and resp[0] == 0x7F and resp[2] == 0x78:  # responsePending
                end = time.monotonic() + 5.0
                continue
            return resp

    def send_only(self, payload: bytes) -> None:
        self.ep.send(payload)


def bmw_client(bus: Bus, ecu: int = 0x12, tester: int = 0xF1, functional: bool = False) -> DiagClient:
    """BMW D-CAN style: requests on 0x6F1 with target address byte, responses on 0x600+ecu."""
    cfg = IsoTpConfig(rx_ids=(0x600 + ecu,), tx_id=0x6F1, rx_ext=(tester,), tx_ext=0xDF if functional else ecu)
    return DiagClient(bus, cfg, "bmw-tester")


def obd_client(bus: Bus, functional: bool = False) -> DiagClient:
    cfg = IsoTpConfig(rx_ids=(0x7E8,), tx_id=0x7DF if functional else 0x7E0, padding=0x55)
    return DiagClient(bus, cfg, "obd-tester")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Send a raw diagnostic request to the emulator")
    ap.add_argument("-i", "--iface", default="vcan0")
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("mode", choices=["obd", "bmw"], help="obd = 0x7E0/0x7E8, bmw = 0x6F1/0x612 (ECU 0x12)")
    ap.add_argument("bytes", nargs="+", help="request bytes in hex, e.g. 22 F1 90")
    a = ap.parse_args(argv)
    try:
        req = bytes.fromhex("".join(a.bytes))
    except ValueError:
        ap.error("request bytes must be hex, e.g. 22 F1 90")
    try:
        bus = SocketCanBus(a.iface)
    except OSError as e:
        print(f"error: cannot open CAN interface {a.iface!r}: {e}", file=sys.stderr)
        return 2
    with bus:
        client = (obd_client(bus) if a.mode == "obd" else bmw_client(bus)).start()
        try:
            resp = client.request(req, a.timeout)
        except TesterTimeout as e:
            print(e, file=sys.stderr)
            return 1
        finally:
            client.stop()
    print(resp.hex(" ").upper())
    return 0


if __name__ == "__main__":
    sys.exit(main())
