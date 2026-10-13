"""SAE J1979 / ISO 15031-5 OBD-II services for the DME."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from . import dtc as dtcmod
from .vehicle import PEAK_TORQUE_NM

if TYPE_CHECKING:  # pragma: no cover
    from .dme import Dme

Snap = Dict[str, float]
Enc = Callable[[Snap, "Dme"], bytes]


def _u8(v: float) -> bytes:
    return bytes([max(0, min(255, int(round(v))))])


def _u16(v: float) -> bytes:
    return max(0, min(65535, int(round(v)))).to_bytes(2, "big")


def _u32(v: float) -> bytes:
    return max(0, min(0xFFFFFFFF, int(round(v)))).to_bytes(4, "big")


def _monitor_status(s: Snap, ecu: "Dme") -> bytes:
    n = min(127, len(ecu.dtcs.entries(dtcmod.CONFIRMED)))
    return bytes([(0x80 if ecu.dtcs.mil_on() else 0) | n, 0x07, 0x65, 0x00])


PIDS: Dict[int, Enc] = {
    0x01: _monitor_status,
    0x03: lambda s, e: bytes([2 if s["engine"] == 2 and s["coolant"] > 30 else 1, 0]),
    0x04: lambda s, e: _u8(s["load"] * 2.55),
    0x05: lambda s, e: _u8(s["coolant"] + 40),
    0x06: lambda s, e: _u8(s["stft"] * 1.28 + 128),
    0x07: lambda s, e: _u8(s["ltft"] * 1.28 + 128),
    0x0B: lambda s, e: _u8(s["map"]),
    0x0C: lambda s, e: _u16(s["rpm"] * 4),
    0x0D: lambda s, e: _u8(s["speed"]),
    0x0E: lambda s, e: _u8((s["timing"] + 64) * 2),
    0x0F: lambda s, e: _u8(s["iat"] + 40),
    0x10: lambda s, e: _u16(s["maf"] * 100),
    0x11: lambda s, e: _u8(s["throttle"] * 2.55),
    0x13: lambda s, e: b"\x03",
    0x1C: lambda s, e: b"\x06",  # EOBD
    0x1F: lambda s, e: _u16(s["run_time"]),
    0x21: lambda s, e: _u16(s["dist_mil"]),
    0x23: lambda s, e: _u16(s["rail_kpa"] / 10),
    0x24: lambda s, e: _u16(s["lambda_"] * 32768) + _u16(1.5 * 8192),
    0x2F: lambda s, e: _u8(s["fuel_level"] * 2.55),
    0x30: lambda s, e: _u8(s["warmups"]),
    0x31: lambda s, e: _u16(s["dist_since_clear"]),
    0x33: lambda s, e: _u8(s["baro"]),
    0x3C: lambda s, e: _u16((s["cat_temp"] + 40) * 10),
    0x42: lambda s, e: _u16(s["battery"] * 1000),
    0x43: lambda s, e: _u16(s["load"] * 2.55),
    0x44: lambda s, e: _u16(s["lambda_cmd"] * 32768),
    0x45: lambda s, e: _u8(s["throttle"] * 2.55),
    0x46: lambda s, e: _u8(s["ambient"] + 40),
    0x49: lambda s, e: _u8(s["pedal"] * 2.55),
    0x4A: lambda s, e: _u8(s["pedal"] * 2.55),
    0x4C: lambda s, e: _u8(s["throttle"] * 2.55),
    0x4D: lambda s, e: _u16(s["time_mil"] / 60),
    0x4E: lambda s, e: _u16(s["time_since_clear"] / 60),
    0x51: lambda s, e: b"\x01",  # gasoline
    0x5C: lambda s, e: _u8(s["oil"] + 40),
    0x5E: lambda s, e: _u16(s["fuel_rate_lph"] * 20),
    0x62: lambda s, e: _u8(s["torque"] / PEAK_TORQUE_NM * 100 + 125),
    0x63: lambda s, e: _u16(PEAK_TORQUE_NM),
    0xA6: lambda s, e: _u32(s["odometer"] * 10),
}
FREEZE_PIDS = (0x04, 0x05, 0x0B, 0x0C, 0x0D, 0x0F, 0x11, 0x10)


def supported_bitmap(base: int, pids) -> bytes:
    """PID 0x00/0x20/... bitmap covering base+1 .. base+0x20; LSB set when a later range has PIDs."""
    word = 0
    for p in pids:
        if base < p <= base + 0x20:
            word |= 1 << (32 - (p - base))
    if any(p > base + 0x20 for p in pids):
        word |= 1
    return word.to_bytes(4, "big")


class ObdServer:
    def __init__(self, ecu: "Dme") -> None:
        self.ecu = ecu

    def handle(self, req: bytes, functional: bool = False) -> Optional[bytes]:
        mode = req[0]
        fn = {0x01: self._m01, 0x02: self._m02, 0x03: self._m03, 0x04: self._m04, 0x07: self._m07, 0x09: self._m09, 0x0A: self._m0a}.get(mode)
        if fn is None:
            return None if functional else bytes([0x7F, mode, 0x11])
        return fn(req)

    def _m01(self, req: bytes) -> Optional[bytes]:
        if len(req) < 2 or len(req) > 7:
            return None
        snap = self.ecu.vehicle.snapshot()
        out = bytearray([0x41])
        avail = sorted(PIDS)
        for pid in req[1:]:
            if pid % 0x20 == 0:
                out += bytes([pid]) + supported_bitmap(pid, avail)
            elif pid in PIDS:
                out += bytes([pid]) + PIDS[pid](snap, self.ecu)
        return bytes(out) if len(out) > 1 else None

    def _m02(self, req: bytes) -> Optional[bytes]:
        if len(req) != 3:
            return None
        pid = req[1]
        frozen = [e for e in self.ecu.dtcs.entries() if e.freeze]
        entry = min(frozen, key=lambda e: e.first_seen) if frozen else None  # frame belongs to the first fault stored
        avail = [0x02] + list(FREEZE_PIDS)
        if pid % 0x20 == 0:
            return bytes([0x42, pid, 0]) + supported_bitmap(pid, avail)
        if entry is None or pid not in avail:
            return None
        if pid == 0x02:
            return bytes([0x42, 0x02, 0x00]) + dtcmod.encode_obd(entry.code)
        return bytes([0x42, pid, 0x00]) + PIDS[pid](entry.freeze, self.ecu)  # type: ignore[arg-type]

    def _dtc_list(self, mode: int, mask: int) -> bytes:
        entries = self.ecu.dtcs.entries(mask)[:63]
        return bytes([mode + 0x40, len(entries)]) + b"".join(dtcmod.encode_obd(e.code) for e in entries)

    def _m03(self, req: bytes) -> bytes:
        return self._dtc_list(0x03, dtcmod.CONFIRMED)

    def _m07(self, req: bytes) -> bytes:
        return self._dtc_list(0x07, dtcmod.PENDING)

    def _m0a(self, req: bytes) -> bytes:
        return bytes([0x4A, 0x00])  # no permanent DTCs are modelled

    def _m04(self, req: bytes) -> bytes:
        self.ecu.clear_dtcs(None)
        return b"\x44"

    def _m09(self, req: bytes) -> Optional[bytes]:
        if len(req) != 2:
            return None
        pid, cfg = req[1], self.ecu.config
        if pid == 0x00:
            return bytes([0x49, 0x00]) + supported_bitmap(0, [0x02, 0x04, 0x06, 0x0A])
        if pid == 0x02:
            return bytes([0x49, 0x02, 0x01]) + cfg.vin.encode("ascii")
        if pid == 0x04:
            return bytes([0x49, 0x04, 0x01]) + cfg.calibration_id.encode("ascii")[:16].ljust(16, b"\0")
        if pid == 0x06:
            return bytes([0x49, 0x06, 0x01]) + bytes.fromhex(cfg.cvn)
        if pid == 0x0A:
            return bytes([0x49, 0x0A, 0x01]) + b"ECM-EngineControl".ljust(20, b"\0")
        return None
