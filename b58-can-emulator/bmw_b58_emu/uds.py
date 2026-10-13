"""UDS (ISO 14229-1) server for the DME."""
from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING, Callable, Dict, Optional

from . import dtc as dtcmod
from .dids import LIVE_DIDS

if TYPE_CHECKING:  # pragma: no cover
    from .dme import Dme

S3_TIMEOUT = 5.0
LOCKOUT_SECONDS = 10.0
MAX_ATTEMPTS = 3
SECURITY_LEVELS = (0x01, 0x03, 0x05, 0x11)

SID_SESSION, SID_RESET, SID_CLEAR_DTC, SID_READ_DTC = 0x10, 0x11, 0x14, 0x19
SID_RDBI, SID_SECURITY, SID_COMM, SID_WDBI = 0x22, 0x27, 0x28, 0x2E
SID_ROUTINE, SID_TESTER, SID_DTC_SETTING = 0x31, 0x3E, 0x85

# NRCs
GENERAL_REJECT, SERVICE_NOT_SUPPORTED, SUBFUNC_NOT_SUPPORTED = 0x10, 0x11, 0x12
BAD_LENGTH, CONDITIONS, SEQUENCE, OUT_OF_RANGE = 0x13, 0x22, 0x24, 0x31
SECURITY_DENIED, INVALID_KEY, EXCEEDED, DELAY = 0x33, 0x35, 0x36, 0x37
SUBFUNC_BAD_SESSION, SERVICE_BAD_SESSION = 0x7E, 0x7F
_FUNCTIONAL_SILENT = {SERVICE_NOT_SUPPORTED, SUBFUNC_NOT_SUPPORTED, OUT_OF_RANGE, SUBFUNC_BAD_SESSION, SERVICE_BAD_SESSION}


class Nrc(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"NRC {code:#04x}")
        self.code = code


def compute_key(seed: int, level: int) -> int:
    """Emulator-defined seed->key function (NOT BMW's real algorithm)."""
    x = (seed ^ 0xB58B58B5) & 0xFFFFFFFF
    x = ((x << 5) | (x >> 27)) & 0xFFFFFFFF
    return (x + level * 0x0101) & 0xFFFFFFFF


class UdsServer:
    def __init__(self, ecu: "Dme") -> None:
        self.ecu = ecu
        self._lock = threading.RLock()
        self.session = 0x01
        self.security_level = 0
        self._seed: Optional[int] = None
        self._seed_level = 0
        self._attempts = 0
        self._lockout_until = 0.0
        self._last = time.monotonic()
        self.routines: Dict[int, Callable[[int, bytes], bytes]] = {
            0xF001: self._rtn_reset_trims,
            0xF003: self._rtn_selftest,
            0xFF00: self._rtn_erase,
        }
        self._handlers: Dict[int, Callable[[bytes], Optional[bytes]]] = {
            SID_SESSION: self._session,
            SID_RESET: self._ecu_reset,
            SID_CLEAR_DTC: self._clear_dtc,
            SID_READ_DTC: self._read_dtc,
            SID_RDBI: self._rdbi,
            SID_SECURITY: self._security,
            SID_COMM: self._comm_control,
            SID_WDBI: self._wdbi,
            SID_ROUTINE: self._routine,
            SID_TESTER: self._tester_present,
            SID_DTC_SETTING: self._dtc_setting,
        }

    # ---- state -----------------------------------------------------------
    def reset_state(self) -> None:
        with self._lock:
            self._set_session(0x01)
            self._attempts = 0
            self._lockout_until = 0.0

    def _set_session(self, session: int) -> None:
        self.session = session
        self.security_level = 0
        self._seed = None
        if session == 0x01:
            self.ecu.dtcs.logging_enabled = True
            self.ecu.comm_tx_enabled = True

    def _s3_check(self) -> None:
        if self.session != 0x01 and time.monotonic() - self._last > S3_TIMEOUT:
            self._set_session(0x01)

    def tick(self) -> None:
        with self._lock:
            self._s3_check()

    # ---- dispatch --------------------------------------------------------
    def handle(self, req: bytes, functional: bool = False) -> Optional[bytes]:
        if not req:
            return None
        sid = req[0]
        with self._lock:
            self._s3_check()
            handler = self._handlers.get(sid)
            try:
                if handler is None:
                    raise Nrc(SERVICE_NOT_SUPPORTED)
                resp = handler(req)
            except Nrc as n:
                if functional and n.code in _FUNCTIONAL_SILENT:
                    return None
                return bytes([0x7F, sid, n.code])
            self._last = time.monotonic()
            return resp

    @staticmethod
    def _need(req: bytes, n: int, exact: bool = True) -> None:
        if len(req) < n or (exact and len(req) != n):
            raise Nrc(BAD_LENGTH)

    @staticmethod
    def _suppressed(req: bytes) -> bool:
        return bool(req[1] & 0x80)

    def _require_non_default(self) -> None:
        if self.session == 0x01:
            raise Nrc(SERVICE_BAD_SESSION)

    # ---- services --------------------------------------------------------
    def _session(self, req: bytes) -> Optional[bytes]:
        self._need(req, 2)
        sub = req[1] & 0x7F
        if sub not in (0x01, 0x02, 0x03):
            raise Nrc(SUBFUNC_NOT_SUPPORTED)
        self._set_session(sub)
        if self._suppressed(req):
            return None
        return bytes([0x50, sub, 0x00, 0x32, 0x01, 0xF4])  # P2=50 ms, P2*=5000 ms

    def _ecu_reset(self, req: bytes) -> Optional[bytes]:
        self._need(req, 2)
        sub = req[1] & 0x7F
        if sub not in (0x01, 0x02, 0x03):
            raise Nrc(SUBFUNC_NOT_SUPPORTED)
        self.ecu.reset(sub)
        return None if self._suppressed(req) else bytes([0x51, sub])

    def _clear_dtc(self, req: bytes) -> bytes:
        self._need(req, 4)
        group = req[1:4]
        if group == b"\xff\xff\xff":
            self.ecu.clear_dtcs(None)
        else:
            code = dtcmod.decode_obd(group[:2])
            if not self.ecu.dtcs.get(code):
                raise Nrc(OUT_OF_RANGE)
            self.ecu.clear_dtcs(code)
        return b"\x54"

    def _read_dtc(self, req: bytes) -> Optional[bytes]:
        self._need(req, 2, exact=False)
        sub = req[1] & 0x7F
        store = self.ecu.dtcs
        if sub == 0x01:
            self._need(req, 3)
            n = len(store.entries(req[2]))
            resp = bytes([0x59, 0x01, 0xFF, 0x01]) + n.to_bytes(2, "big")
        elif sub == 0x02:
            self._need(req, 3)
            body = b"".join(dtcmod.uds_dtc(e.code) + bytes([e.status]) for e in store.entries(req[2]))
            resp = bytes([0x59, 0x02, 0xFF]) + body
        elif sub == 0x0A:
            self._need(req, 2)
            known = set(dtcmod.CATALOG) | {e.code for e in store.entries()}
            body = b""
            for code in sorted(known):
                e = store.get(code)
                body += dtcmod.uds_dtc(code) + bytes([e.status if e else 0x00])
            resp = bytes([0x59, 0x0A, 0xFF]) + body
        elif sub in (0x04, 0x06):
            self._need(req, 6)
            dtc_bytes, rec = req[2:5], req[5]
            entry = store.get(dtcmod.decode_obd(dtc_bytes[:2]))
            if entry is None:
                raise Nrc(OUT_OF_RANGE)
            head = bytes([0x59, sub]) + dtcmod.uds_dtc(entry.code) + bytes([entry.status])
            if sub == 0x04:
                if rec not in (0x01, 0xFF):
                    raise Nrc(OUT_OF_RANGE)
                body = b""
                if entry.freeze:
                    ids = [self.ecu.dids[d] for d in sorted(self.ecu.dids) if self.ecu.dids[d].freeze]
                    body = bytes([0x01, len(ids)]) + b"".join(s.did.to_bytes(2, "big") + s.read(entry.freeze) for s in ids)
                resp = head + body
            else:
                if rec not in (0x01, 0xFF):
                    raise Nrc(OUT_OF_RANGE)
                resp = head + bytes([0x01, min(entry.occurrences, 255)])
        else:
            raise Nrc(SUBFUNC_NOT_SUPPORTED)
        return None if self._suppressed(req) else resp

    def _rdbi(self, req: bytes) -> bytes:
        if len(req) < 3 or (len(req) - 1) % 2:
            raise Nrc(BAD_LENGTH)
        snap = self.ecu.vehicle.snapshot()
        out = bytearray([0x62])
        for i in range(1, len(req), 2):
            did = int.from_bytes(req[i : i + 2], "big")
            spec = self.ecu.dids.get(did)
            if spec is None:
                raise Nrc(OUT_OF_RANGE)
            out += req[i : i + 2] + spec.read(snap)
        if len(out) > 4095:
            raise Nrc(0x14)
        return bytes(out)

    def _wdbi(self, req: bytes) -> bytes:
        self._need(req, 4, exact=False)
        self._require_non_default()
        did = int.from_bytes(req[1:3], "big")
        spec = self.ecu.dids.get(did)
        if spec is None or spec.write is None:
            raise Nrc(OUT_OF_RANGE)
        if self.security_level < spec.write_security:
            raise Nrc(SECURITY_DENIED)
        data = req[3:]
        if spec.write_len is not None and len(data) != spec.write_len:
            raise Nrc(BAD_LENGTH)
        try:
            spec.write(data)
        except ValueError:
            raise Nrc(OUT_OF_RANGE)
        return bytes([0x6E]) + req[1:3]

    def _security(self, req: bytes) -> Optional[bytes]:
        self._need(req, 2, exact=False)
        sub = req[1] & 0x7F
        now = time.monotonic()
        if sub % 2 == 1:  # request seed
            self._need(req, 2)
            if sub not in SECURITY_LEVELS:
                raise Nrc(SUBFUNC_NOT_SUPPORTED)
            if self.session == 0x01:
                raise Nrc(SERVICE_BAD_SESSION)
            if now < self._lockout_until:
                raise Nrc(DELAY)
            if self.security_level == sub:
                return bytes([0x67, sub, 0, 0, 0, 0])
            self._seed = int.from_bytes(os.urandom(4), "big") or 1
            self._seed_level = sub
            return bytes([0x67, sub]) + self._seed.to_bytes(4, "big")
        # send key
        if (sub - 1) not in SECURITY_LEVELS:
            raise Nrc(SUBFUNC_NOT_SUPPORTED)
        self._need(req, 6)
        if self._seed is None or self._seed_level != sub - 1:
            raise Nrc(SEQUENCE)
        if now < self._lockout_until:
            raise Nrc(DELAY)
        if int.from_bytes(req[2:6], "big") == compute_key(self._seed, self._seed_level):
            self.security_level = self._seed_level
            self._seed = None
            self._attempts = 0
            return None if self._suppressed(req) else bytes([0x67, sub])
        self._seed = None
        self._attempts += 1
        if self._attempts >= MAX_ATTEMPTS:
            self._attempts = 0
            self._lockout_until = now + LOCKOUT_SECONDS
            raise Nrc(EXCEEDED)
        raise Nrc(INVALID_KEY)

    def _comm_control(self, req: bytes) -> Optional[bytes]:
        self._need(req, 3)
        self._require_non_default()
        ctrl = req[1] & 0x7F
        if ctrl not in (0, 1, 2, 3):
            raise Nrc(SUBFUNC_NOT_SUPPORTED)
        if req[2] & 0x3F not in (1, 2, 3):
            raise Nrc(OUT_OF_RANGE)
        self.ecu.comm_tx_enabled = ctrl in (0, 2)
        return None if self._suppressed(req) else bytes([0x68, ctrl])

    def _dtc_setting(self, req: bytes) -> Optional[bytes]:
        self._need(req, 2, exact=False)
        self._require_non_default()
        sub = req[1] & 0x7F
        if sub not in (1, 2):
            raise Nrc(SUBFUNC_NOT_SUPPORTED)
        self.ecu.dtcs.logging_enabled = sub == 1
        return None if self._suppressed(req) else bytes([0xC5, sub])

    def _tester_present(self, req: bytes) -> Optional[bytes]:
        self._need(req, 2)
        if (req[1] & 0x7F) != 0:
            raise Nrc(SUBFUNC_NOT_SUPPORTED)
        return None if self._suppressed(req) else bytes([0x7E, 0x00])

    def _routine(self, req: bytes) -> Optional[bytes]:
        self._need(req, 4, exact=False)
        sub = req[1] & 0x7F
        if sub not in (1, 2, 3):
            raise Nrc(SUBFUNC_NOT_SUPPORTED)
        rid = int.from_bytes(req[2:4], "big")
        fn = self.routines.get(rid)
        if fn is None:
            raise Nrc(OUT_OF_RANGE)
        self._require_non_default()
        record = fn(sub, req[4:])
        return None if self._suppressed(req) else bytes([0x71, sub]) + req[2:4] + record

    # ---- routines ----------------------------------------------------------
    def _rtn_reset_trims(self, sub: int, params: bytes) -> bytes:
        if sub == 1:
            self.ecu.vehicle.ltft = 0.0
        return b"\x00"

    def _rtn_selftest(self, sub: int, params: bytes) -> bytes:
        if self.ecu.vehicle.engine == 2 and self.ecu.vehicle.speed > 1:
            raise Nrc(CONDITIONS)
        return b"\x00"  # 00 = passed

    def _rtn_erase(self, sub: int, params: bytes) -> bytes:
        if self.session != 0x02:
            raise Nrc(CONDITIONS)
        if self.security_level < 1:
            raise Nrc(SECURITY_DENIED)
        return b"\x00"  # simulated only: nothing is erased
