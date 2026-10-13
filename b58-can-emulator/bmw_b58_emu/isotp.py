"""ISO 15765-2 (ISO-TP) transport with normal and BMW-style extended addressing."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, FrozenSet, Optional, Tuple

from .bus import Frame

log = logging.getLogger(__name__)

PCI_SF, PCI_FF, PCI_CF, PCI_FC = 0, 1, 2, 3
FC_CTS, FC_WAIT, FC_OVFLW = 0, 1, 2


def decode_stmin(v: int) -> float:
    if v <= 0x7F:
        return v / 1000.0
    if 0xF1 <= v <= 0xF9:
        return (v - 0xF0) / 10000.0
    return 0.127  # reserved values: use the maximum


@dataclass(frozen=True)
class IsoTpMessage:
    payload: bytes
    can_id: int
    ext: Optional[int]
    functional: bool


@dataclass
class IsoTpConfig:
    rx_ids: Tuple[int, ...]
    tx_id: int
    rx_ext: Optional[Tuple[int, ...]] = None  # accepted address bytes; None = normal addressing
    tx_ext: Optional[int] = None
    functional_ids: FrozenSet[int] = frozenset()
    functional_ext: FrozenSet[int] = frozenset()
    padding: Optional[int] = 0x00
    block_size: int = 0
    stmin: int = 0  # raw STmin byte we advertise
    max_rx_len: int = 4095
    timeout: float = 1.0  # N_Bs / N_Cr


class IsoTpEndpoint:
    def __init__(
        self,
        cfg: IsoTpConfig,
        send_frame: Callable[[Frame], None],
        on_message: Callable[[IsoTpMessage], None],
        name: str = "isotp",
    ) -> None:
        self.cfg = cfg
        self.name = name
        self._send_frame = send_frame
        self._on_message = on_message
        self._tx_q: "queue.Queue[bytes]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cv = threading.Condition()
        self._fc: Optional[Tuple[int, int, int]] = None
        # receive state
        self._rx_buf: Optional[bytearray] = None
        self._rx_len = 0
        self._rx_sn = 0
        self._rx_deadline = 0.0
        self._rx_blk = 0
        self._rx_ctx: Optional[Tuple[int, Optional[int], bool]] = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._tx_loop, name=f"{self.name}-tx", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=2)

    # ---- helpers ---------------------------------------------------------
    def _frame(self, pci_and_data: bytes) -> Frame:
        data = pci_and_data
        if self.cfg.tx_ext is not None:
            data = bytes([self.cfg.tx_ext]) + data
        if self.cfg.padding is not None:
            data = data.ljust(8, bytes([self.cfg.padding]))
        return Frame(self.cfg.tx_id, data)

    @property
    def _ovh(self) -> int:
        return 0 if self.cfg.tx_ext is None else 1

    # ---- receive ---------------------------------------------------------
    def handle_frame(self, f: Frame) -> bool:
        if f.extended or f.can_id not in self.cfg.rx_ids:
            return False
        d = f.data
        ext: Optional[int] = None
        if self.cfg.rx_ext is not None:
            if not d or d[0] not in self.cfg.rx_ext:
                return False
            ext, d = d[0], d[1:]
        if not d:
            return True
        functional = f.can_id in self.cfg.functional_ids or (ext is not None and ext in self.cfg.functional_ext)
        pci = d[0] >> 4
        if pci == PCI_SF:
            n = d[0] & 0x0F
            if 0 < n <= len(d) - 1:
                self._rx_buf = None
                self._on_message(IsoTpMessage(bytes(d[1 : 1 + n]), f.can_id, ext, functional))
        elif pci == PCI_FF:
            if functional or len(d) < 2:
                return True
            total = ((d[0] & 0x0F) << 8) | d[1]
            if total <= 6 or total > 4095:
                return True
            if total > self.cfg.max_rx_len:
                self._send_fc(FC_OVFLW, 0, 0)
                return True
            self._rx_buf = bytearray(d[2:])
            self._rx_len = total
            self._rx_sn = 1
            self._rx_blk = 0
            self._rx_ctx = (f.can_id, ext, functional)
            self._rx_deadline = time.monotonic() + self.cfg.timeout
            self._send_fc(FC_CTS, self.cfg.block_size, self.cfg.stmin)
        elif pci == PCI_CF:
            if self._rx_buf is None:
                return True
            if (d[0] & 0x0F) != self._rx_sn:
                log.debug("%s: bad ISO-TP sequence number, aborting", self.name)
                self._rx_buf = None
                return True
            self._rx_sn = (self._rx_sn + 1) & 0x0F
            self._rx_buf += d[1:]
            self._rx_deadline = time.monotonic() + self.cfg.timeout
            if len(self._rx_buf) >= self._rx_len:
                payload = bytes(self._rx_buf[: self._rx_len])
                cid, e, fn = self._rx_ctx  # type: ignore[misc]
                self._rx_buf = None
                self._on_message(IsoTpMessage(payload, cid, e, fn))
            else:
                self._rx_blk += 1
                if self.cfg.block_size and self._rx_blk >= self.cfg.block_size:
                    self._rx_blk = 0
                    self._send_fc(FC_CTS, self.cfg.block_size, self.cfg.stmin)
        elif pci == PCI_FC and len(d) >= 3:
            with self._cv:
                self._fc = (d[0] & 0x0F, d[1], d[2])
                self._cv.notify_all()
        return True

    def poll(self) -> None:
        if self._rx_buf is not None and time.monotonic() > self._rx_deadline:
            log.debug("%s: N_Cr timeout, dropping partial message", self.name)
            self._rx_buf = None

    def _send_fc(self, status: int, bs: int, stmin: int) -> None:
        self._send_frame(self._frame(bytes([(PCI_FC << 4) | status, bs, stmin])))

    # ---- transmit --------------------------------------------------------
    def send(self, payload: bytes) -> None:
        self._tx_q.put(bytes(payload))

    def _tx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._tx_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._transmit(payload)
            except Exception:  # pragma: no cover - keep the worker alive
                log.exception("%s: ISO-TP transmit failed", self.name)

    def _wait_fc(self) -> Optional[Tuple[int, int, int]]:
        end = time.monotonic() + self.cfg.timeout
        with self._cv:
            while self._fc is None:
                rem = end - time.monotonic()
                if rem <= 0 or self._stop.is_set():
                    return None
                self._cv.wait(rem)
            fc, self._fc = self._fc, None
            return fc

    def _transmit(self, payload: bytes) -> None:
        sf_max = 7 - self._ovh
        if len(payload) <= sf_max:
            self._send_frame(self._frame(bytes([len(payload)]) + payload))
            return
        if len(payload) > 4095:
            raise ValueError("ISO-TP payload too large")
        first = 6 - self._ovh
        cf_len = 7 - self._ovh
        with self._cv:
            self._fc = None
        n = len(payload)
        self._send_frame(self._frame(bytes([(PCI_FF << 4) | (n >> 8), n & 0xFF]) + payload[:first]))
        sent, sn, waits = first, 1, 0
        while sent < n:
            fc = self._wait_fc()
            if fc is None:
                log.debug("%s: N_Bs timeout", self.name)
                return
            status, bs, stmin = fc
            if status == FC_OVFLW:
                return
            if status == FC_WAIT:
                waits += 1
                if waits > 10:
                    return
                continue
            gap, count = decode_stmin(stmin), 0
            while sent < n:
                chunk = payload[sent : sent + cf_len]
                self._send_frame(self._frame(bytes([(PCI_CF << 4) | (sn & 0x0F)]) + chunk))
                sent += len(chunk)
                sn += 1
                count += 1
                if gap and sent < n:
                    time.sleep(gap)
                if bs and count >= bs and sent < n:
                    break
