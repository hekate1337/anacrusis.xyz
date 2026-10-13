"""Data identifiers (UDS 0x22 / 0x2E). ISO-standard identification DIDs plus emulator-defined live data.

The live-data block (0xD0xx) is *emulator-defined*: real BMW DME live data DIDs are proprietary and
are not reproduced here. Scalings are documented in docs/PROTOCOL.md.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .dme import Dme

Snapshot = Dict[str, float]


@dataclass
class DidSpec:
    did: int
    name: str
    read: Callable[[Snapshot], bytes]
    write: Optional[Callable[[bytes], None]] = None
    write_len: Optional[int] = None
    write_security: int = 0  # minimum unlocked security level required to write
    freeze: bool = False  # included in DTC snapshot records


@dataclass(frozen=True)
class LiveDid:
    did: int
    name: str
    key: str
    fmt: str  # big-endian struct code
    scale: float
    offset: float
    unit: str

    def encode(self, snap: Snapshot) -> bytes:
        size = struct.calcsize(">" + self.fmt)
        bits = size * 8
        signed = self.fmt.islower()
        lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
        raw = int(round((float(snap.get(self.key, 0.0)) - self.offset) / self.scale))
        return struct.pack(">" + self.fmt, max(lo, min(hi, raw)))

    def decode(self, data: bytes) -> float:
        return struct.unpack(">" + self.fmt, data)[0] * self.scale + self.offset


LIVE_DIDS = [
    LiveDid(0xD001, "Engine speed", "rpm", "H", 1.0, 0, "rpm"),
    LiveDid(0xD002, "Vehicle speed", "speed", "H", 0.01, 0, "km/h"),
    LiveDid(0xD003, "Coolant temperature", "coolant", "h", 0.1, 0, "degC"),
    LiveDid(0xD004, "Engine oil temperature", "oil", "h", 0.1, 0, "degC"),
    LiveDid(0xD005, "Intake air temperature", "iat", "h", 0.1, 0, "degC"),
    LiveDid(0xD006, "Manifold absolute pressure", "map", "H", 0.1, 0, "kPa"),
    LiveDid(0xD007, "Ambient pressure", "baro", "H", 0.1, 0, "kPa"),
    LiveDid(0xD008, "Accelerator pedal", "pedal", "H", 0.01, 0, "%"),
    LiveDid(0xD009, "Throttle plate angle", "throttle", "H", 0.01, 0, "%"),
    LiveDid(0xD00A, "Engine torque (indicated)", "torque", "h", 0.1, 0, "Nm"),
    LiveDid(0xD00B, "Ignition timing", "timing", "h", 0.1, 0, "deg BTDC"),
    LiveDid(0xD00C, "Rail pressure (high)", "rail_kpa", "H", 1.0, 0, "kPa"),
    LiveDid(0xD00D, "Lambda (measured)", "lambda_", "H", 0.001, 0, "-"),
    LiveDid(0xD00E, "Battery voltage", "battery", "H", 0.001, 0, "V"),
    LiveDid(0xD00F, "Fuel level", "fuel_level", "H", 0.1, 0, "%"),
    LiveDid(0xD010, "Air mass flow", "maf", "H", 0.1, 0, "g/s"),
    LiveDid(0xD011, "Odometer", "odometer", "I", 0.1, 0, "km"),
    LiveDid(0xD012, "Engine run time", "run_time", "I", 1.0, 0, "s"),
    LiveDid(0xD013, "Gear", "gear", "B", 1.0, 0, "-"),
    LiveDid(0xD014, "Terminal status (0=KL30,1=KL R,2=KL15)", "ignition", "B", 1.0, 0, "-"),
    LiveDid(0xD015, "Engine state (0=off,1=cranking,2=running)", "engine", "B", 1.0, 0, "-"),
    LiveDid(0xD016, "Boost pressure (relative)", "boost", "H", 0.1, 0, "kPa"),
]
_FREEZE = {0xD001, 0xD002, 0xD003, 0xD006, 0xD00E, 0xD008}


def ascii_field(value: str, length: int) -> bytes:
    return value.encode("ascii", "replace")[:length].ljust(length, b"\0")


def build_dids(ecu: "Dme") -> Dict[int, DidSpec]:
    cfg = ecu.config
    reg: Dict[int, DidSpec] = {}

    def add(spec: DidSpec) -> None:
        reg[spec.did] = spec

    def static(did: int, name: str, getter: Callable[[], bytes], **kw) -> None:
        add(DidSpec(did, name, lambda _s, g=getter: g(), **kw))

    def write_vin(data: bytes) -> None:
        text = data.decode("ascii", "strict")
        if len(text) != 17 or any(c in "IOQ" for c in text.upper()) or not text.isalnum():
            raise ValueError("invalid VIN")
        cfg.vin = text.upper()
        ecu.save_state()

    def write_coding(data: bytes) -> None:
        ecu.coding[:] = data
        ecu.save_state()

    add(DidSpec(0xF190, "VIN", lambda _s: cfg.vin.encode("ascii"), write_vin, 17, write_security=1))
    static(0xF186, "Active diagnostic session", lambda: bytes([ecu.uds.session]))
    static(0xF187, "Spare part number", lambda: ascii_field(cfg.part_number, 7))
    static(0xF18A, "System supplier", lambda: ascii_field(cfg.supplier, 16))
    static(0xF18B, "ECU manufacturing date (BCD yy mm dd)", lambda: bytes.fromhex(cfg.manufacturing_date))
    static(0xF18C, "ECU serial number", lambda: ascii_field(cfg.serial, 16))
    static(0xF189, "Software version", lambda: ascii_field(cfg.sw_version, 16))
    static(0xF191, "Hardware version", lambda: ascii_field(cfg.hw_version, 16))
    static(0xF197, "System name", lambda: ascii_field(cfg.system_name, 16))
    add(DidSpec(0xC000, "Coding data (emulator)", lambda _s: bytes(ecu.coding), write_coding, 16, write_security=1))
    for ld in LIVE_DIDS:
        add(DidSpec(ld.did, ld.name, ld.encode, freeze=ld.did in _FREEZE))
    return reg
