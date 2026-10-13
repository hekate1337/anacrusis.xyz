"""Diagnostic trouble code store (OBD-II P-codes carried as ISO 14229 3-byte DTCs)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# UDS DTC status byte bits (ISO 14229-1)
TEST_FAILED = 0x01
FAILED_THIS_CYCLE = 0x02
PENDING = 0x04
CONFIRMED = 0x08
NOT_COMPLETED_SINCE_CLEAR = 0x10
FAILED_SINCE_CLEAR = 0x20
NOT_COMPLETED_THIS_CYCLE = 0x40
WARNING_INDICATOR = 0x80

CATALOG: Dict[str, str] = {
    "P0011": "Camshaft position A timing over-advanced (bank 1)",
    "P0014": "Exhaust camshaft position timing over-advanced (bank 1)",
    "P0087": "Fuel rail/system pressure too low",
    "P0101": "Mass air flow circuit range/performance",
    "P0106": "Manifold absolute pressure sensor range/performance",
    "P0118": "Engine coolant temperature sensor circuit high",
    "P0128": "Coolant thermostat below regulating temperature",
    "P0171": "System too lean (bank 1)",
    "P0172": "System too rich (bank 1)",
    "P0234": "Turbocharger overboost condition",
    "P0299": "Turbocharger underboost condition",
    "P0300": "Random/multiple cylinder misfire detected",
    "P0301": "Cylinder 1 misfire detected",
    "P0302": "Cylinder 2 misfire detected",
    "P0303": "Cylinder 3 misfire detected",
    "P0304": "Cylinder 4 misfire detected",
    "P0305": "Cylinder 5 misfire detected",
    "P0306": "Cylinder 6 misfire detected",
    "P0420": "Catalyst system efficiency below threshold (bank 1)",
    "P0562": "System voltage low",
    "P0606": "ECM/PCM processor fault",
    "P2096": "Post catalyst fuel trim system too lean (bank 1)",
}


def encode_obd(code: str) -> bytes:
    """'P0301' -> b'\\x03\\x01' (SAE J2012 two-byte form)."""
    code = code.strip().upper()
    if len(code) != 5 or code[0] not in "PCBU" or any(c not in "0123456789ABCDEF" for c in code[1:]):
        raise ValueError(f"invalid DTC {code!r}")
    sys = "PCBU".index(code[0])
    if code[1] not in "0123":
        raise ValueError(f"invalid DTC {code!r}")
    b0 = (sys << 6) | (int(code[1]) << 4) | int(code[2], 16)
    b1 = (int(code[3], 16) << 4) | int(code[4], 16)
    return bytes([b0, b1])


def decode_obd(b: bytes) -> str:
    return "PCBU"[b[0] >> 6] + str((b[0] >> 4) & 3) + f"{b[0] & 0xF:X}{b[1] >> 4:X}{b[1] & 0xF:X}"


def uds_dtc(code: str) -> bytes:
    return encode_obd(code) + b"\x00"  # failure-type byte 00


@dataclass
class DtcEntry:
    code: str
    status: int
    occurrences: int = 1
    freeze: Optional[Dict[str, float]] = None
    first_seen: float = field(default_factory=time.time)

    @property
    def description(self) -> str:
        return CATALOG.get(self.code, "Manufacturer/unknown fault")


class DtcStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: Dict[str, DtcEntry] = {}
        self.logging_enabled = True  # UDS 0x85 ControlDTCSetting

    def raise_fault(self, code: str, snapshot: Optional[Dict[str, float]] = None, confirmed: bool = True) -> Optional[DtcEntry]:
        encode_obd(code)  # validate
        code = code.upper()
        with self._lock:
            if not self.logging_enabled:
                return None
            e = self._entries.get(code)
            if e is None:
                e = DtcEntry(code, 0, 0, dict(snapshot) if snapshot else None)
                self._entries[code] = e
            e.occurrences += 1
            e.status |= TEST_FAILED | FAILED_THIS_CYCLE | PENDING | FAILED_SINCE_CLEAR
            e.status &= ~(NOT_COMPLETED_SINCE_CLEAR | NOT_COMPLETED_THIS_CYCLE)
            if confirmed:
                e.status |= CONFIRMED | WARNING_INDICATOR
            return e

    def heal(self, code: str) -> None:
        """Fault no longer present: clears testFailed/pending, keeps the stored (confirmed) record."""
        with self._lock:
            e = self._entries.get(code.upper())
            if e:
                e.status &= ~(TEST_FAILED | PENDING | FAILED_THIS_CYCLE | WARNING_INDICATOR)

    def clear(self, code: Optional[str] = None) -> int:
        with self._lock:
            if code is None:
                n = len(self._entries)
                self._entries.clear()
                return n
            return 1 if self._entries.pop(code.upper(), None) else 0

    def entries(self, mask: int = 0xFF) -> List[DtcEntry]:
        with self._lock:
            return [e for e in sorted(self._entries.values(), key=lambda x: x.code) if e.status & mask]

    def get(self, code: str) -> Optional[DtcEntry]:
        with self._lock:
            return self._entries.get(code.upper())

    def mil_on(self) -> bool:
        return any(e.status & WARNING_INDICATOR for e in self.entries())

    def to_state(self) -> List[dict]:
        with self._lock:
            return [
                {"code": e.code, "status": e.status, "occurrences": e.occurrences, "freeze": e.freeze, "first_seen": e.first_seen}
                for e in self._entries.values()
            ]

    def load_state(self, items: List[dict]) -> None:
        with self._lock:
            self._entries = {
                i["code"]: DtcEntry(i["code"], int(i["status"]), int(i.get("occurrences", 1)), i.get("freeze"), i.get("first_seen", time.time()))
                for i in items
            }
