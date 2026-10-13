"""A CAN node: one RX thread demuxing into ISO-TP endpoints plus a cyclic task scheduler."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from .bus import Bus, Frame
from .isotp import IsoTpConfig, IsoTpEndpoint, IsoTpMessage

log = logging.getLogger(__name__)
trace_log = logging.getLogger("bmw_b58_emu.can")


@dataclass
class Task:
    name: str
    period: float
    fn: Callable[[], None]
    enabled: bool = True
    next_due: float = 0.0


class CanNode:
    def __init__(self, bus: Bus, name: str = "node") -> None:
        self.bus = bus
        self.name = name
        self._endpoints: List[IsoTpEndpoint] = []
        self._tasks: List[Task] = []
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._lock = threading.Lock()
        self.tx_count = 0
        self.rx_count = 0
        self.rx_gate: Callable[[], bool] = lambda: True  # False => node is "offline"

    def send(self, frame: Frame) -> None:
        self.bus.send(frame)
        self.tx_count += 1
        if trace_log.isEnabledFor(logging.DEBUG):
            trace_log.debug("TX %s", frame)

    def add_endpoint(self, cfg: IsoTpConfig, on_message: Callable[[IsoTpMessage], None], name: str) -> IsoTpEndpoint:
        ep = IsoTpEndpoint(cfg, self._send_if_online, on_message, name)
        self._endpoints.append(ep)
        return ep

    def _send_if_online(self, frame: Frame) -> None:
        if self.rx_gate():
            self.send(frame)

    def schedule(self, name: str, period: float, fn: Callable[[], None]) -> Task:
        t = Task(name, period, fn, next_due=time.monotonic() + period)
        with self._lock:
            self._tasks.append(t)
        return t

    def start(self) -> None:
        self._stop.clear()
        for ep in self._endpoints:
            ep.start()
        for target, nm in ((self._rx_loop, "rx"), (self._sched_loop, "sched")):
            th = threading.Thread(target=target, name=f"{self.name}-{nm}", daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=2)
        self._threads.clear()
        for ep in self._endpoints:
            ep.stop()

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                f = self.bus.recv(0.02)
            except OSError:
                if self._stop.is_set():
                    return
                log.exception("bus receive failed")
                time.sleep(0.1)
                continue
            if f is not None and self.rx_gate():
                self.rx_count += 1
                if trace_log.isEnabledFor(logging.DEBUG):
                    trace_log.debug("RX %s", f)
                for ep in self._endpoints:
                    try:
                        if ep.handle_frame(f):
                            break
                    except Exception:
                        log.exception("error handling frame %s", f)
            for ep in self._endpoints:
                ep.poll()

    def _sched_loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                tasks = list(self._tasks)
            nxt = now + 0.05
            for t in tasks:
                if now >= t.next_due:
                    if t.enabled and self.rx_gate():
                        try:
                            t.fn()
                        except Exception:
                            log.exception("task %s failed", t.name)
                    t.next_due += t.period
                    if t.next_due < now - 5 * t.period or not t.enabled:
                        t.next_due = now + t.period
                nxt = min(nxt, t.next_due)
            self._stop.wait(max(0.0005, nxt - time.monotonic()))
