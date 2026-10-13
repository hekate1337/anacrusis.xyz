"""Cyclic PT-CAN style broadcast frames.

IMPORTANT: arbitration IDs follow the *style* of BMW F/G-series PT-CAN (CRC8 in byte 0, 4-bit alive
counter in byte 1) but the signal layouts are emulator-defined, not BMW's proprietary matrix. See
docs/b58_ptcan.dbc for the exact layout used here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Tuple

from .bus import Frame

if TYPE_CHECKING:  # pragma: no cover
    from .dme import Dme

ID_ENGINE = 0x0A5
ID_SPEED = 0x1A1
ID_TEMPS = 0x1D0
ID_TERMINALS = 0x12F
ID_AMBIENT = 0x2CA
ID_ODOMETER = 0x330
ID_NM = 0x512


def crc8(data: bytes) -> int:
    """SAE J1850 CRC-8 (poly 0x1D, init 0xFF, xorout 0xFF)."""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def _s16(v: float) -> bytes:
    return (max(-32768, min(32767, int(round(v)))) & 0xFFFF).to_bytes(2, "little")


def _u16(v: float) -> bytes:
    return max(0, min(65535, int(round(v)))).to_bytes(2, "little")


def _u8(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _finish(body: bytes, counter: int) -> bytes:
    """body = bytes 2..7 -> full frame with counter nibble and CRC."""
    b1 = 0xF0 | (counter & 0x0F)
    payload = bytes([b1]) + body
    return bytes([crc8(payload)]) + payload


def build_engine(s: Dict[str, float], c: int) -> bytes:
    running = 1 if s["engine"] == 2 else 0
    return _finish(_s16(s["torque"] * 2) + _u16(s["rpm"] * 4) + bytes([_u8(s["pedal"] * 2.5), running]), c)


def build_speed(s: Dict[str, float], c: int) -> bytes:
    return _finish(_u16(s["speed"] * 100) + bytes([_u8(s["brake"] * 2.55), int(s["gear"]) & 0x0F, 0, 0]), c)


def build_temps(s: Dict[str, float], c: int) -> bytes:
    return _finish(bytes([_u8(s["coolant"] + 48), _u8(s["oil"] + 48), _u8(s["iat"] + 48), _u8(s["boost"] / 2)]) + _u16(s["baro"] * 10), c)


def build_terminals(s: Dict[str, float], c: int, mil: bool) -> bytes:
    ign, eng = int(s["ignition"]), int(s["engine"])
    kl_r, kl_15, kl_50 = ign >= 1, ign >= 2, eng == 1
    flags = (1 if kl_r else 0) | (2 if kl_15 else 0) | (4 if kl_50 else 0) | (8 if mil else 0)
    return _finish(bytes([flags, eng, _u8(s["battery"] * 10), 0, 0, 0]), c)


def build_ambient(s: Dict[str, float], c: int) -> bytes:
    return _finish(bytes([_u8((s["ambient"] + 40) * 2), 0, 0, 0, 0, 0]), c)


def build_odometer(s: Dict[str, float], c: int) -> bytes:
    km = int(s["odometer"]) & 0xFFFFFF
    return _finish(km.to_bytes(3, "little") + bytes([_u8(s["fuel_level"] * 2.55), 0, 0]), c)


def build_nm(address: int, c: int) -> bytes:
    return bytes([address, 0x01 | ((c & 0x0F) << 4), 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])


def install_broadcast(dme: "Dme") -> List:
    """Register all cyclic frames on the DME's node; returns the Task objects."""
    counters: Dict[int, int] = {}

    def make(can_id: int, builder: Callable[[Dict[str, float], int], bytes], nm: bool = False) -> Callable[[], None]:
        def tick() -> None:
            if not dme.comm_tx_enabled:
                return
            snap = dme.vehicle.snapshot()
            if snap["ignition"] == 0:
                return  # bus asleep with terminals off
            c = counters.get(can_id, 0)
            counters[can_id] = (c + 1) & 0x0F
            data = build_nm(dme.config.address, c) if nm else builder(snap, c)
            dme.node.send(Frame(can_id, data))

        return tick

    table: List[Tuple[str, int, float, Callable]] = [
        ("engine", ID_ENGINE, 0.02, build_engine),
        ("speed", ID_SPEED, 0.02, build_speed),
        ("temps", ID_TEMPS, 0.1, build_temps),
        ("terminals", ID_TERMINALS, 0.1, lambda s, c: build_terminals(s, c, dme.dtcs.mil_on())),
        ("ambient", ID_AMBIENT, 1.0, build_ambient),
        ("odometer", ID_ODOMETER, 1.0, build_odometer),
    ]
    tasks = [dme.node.schedule(f"bc-{n}", p, make(i, b)) for n, i, p, b in table]
    tasks.append(dme.node.schedule("bc-nm", 1.0, make(ID_NM + (dme.config.address - 0x12), lambda s, c: b"", nm=True)))
    return tasks
