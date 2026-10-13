"""CAN bus abstraction: Linux SocketCAN (vcan/can) and an in-process hub for tests."""
from __future__ import annotations

import errno
import queue
import socket
import struct
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_SFF_MASK = 0x7FF
CAN_EFF_MASK = 0x1FFFFFFF


@dataclass(frozen=True)
class Frame:
    can_id: int
    data: bytes = b""
    extended: bool = False
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if len(self.data) > 8:
            raise ValueError("classic CAN frames carry at most 8 data bytes")
        limit = CAN_EFF_MASK if self.extended else CAN_SFF_MASK
        if not 0 <= self.can_id <= limit:
            raise ValueError(f"CAN id {self.can_id:#x} out of range")

    def __str__(self) -> str:
        return f"{self.can_id:03X}#{self.data.hex().upper()}"


class Bus(ABC):
    @abstractmethod
    def send(self, frame: Frame) -> None: ...

    @abstractmethod
    def recv(self, timeout: float) -> Optional[Frame]: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "Bus":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SocketCanBus(Bus):
    """Raw SocketCAN socket. Linux only; works with vcan0, can0, slcan0, ..."""

    _FMT = "=IB3x8s"

    def __init__(self, channel: str) -> None:
        if not hasattr(socket, "AF_CAN"):
            raise OSError("SocketCAN is not available on this platform (Linux only)")
        self.channel = channel
        self._sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        # We do not want our own frames echoed back to us.
        self._sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 0)
        self._sock.bind((channel,))

    def send(self, frame: Frame) -> None:
        can_id = frame.can_id | (CAN_EFF_FLAG if frame.extended else 0)
        raw = struct.pack(self._FMT, can_id, len(frame.data), frame.data.ljust(8, b"\0"))
        for _ in range(20):
            try:
                self._sock.send(raw)
                return
            except OSError as e:
                if e.errno not in (errno.ENOBUFS, errno.EAGAIN):
                    raise
                time.sleep(0.001)

    def recv(self, timeout: float) -> Optional[Frame]:
        self._sock.settimeout(timeout)
        try:
            raw = self._sock.recv(16)
        except (socket.timeout, BlockingIOError):
            return None
        if len(raw) < 16:
            return None
        can_id, dlc, data = struct.unpack(self._FMT, raw)
        if can_id & (CAN_ERR_FLAG | CAN_RTR_FLAG):
            return None
        ext = bool(can_id & CAN_EFF_FLAG)
        can_id &= CAN_EFF_MASK if ext else CAN_SFF_MASK
        return Frame(can_id, bytes(data[: min(dlc, 8)]), ext, time.time())

    def close(self) -> None:
        self._sock.close()


class MemoryHub:
    """In-process virtual CAN segment: every node hears every other node."""

    def __init__(self) -> None:
        self._nodes: List["MemoryBus"] = []
        self._lock = threading.Lock()
        self.log: List[Frame] = []
        self.keep_log = False

    def connect(self) -> "MemoryBus":
        node = MemoryBus(self)
        with self._lock:
            self._nodes.append(node)
        return node

    def _deliver(self, sender: "MemoryBus", frame: Frame) -> None:
        stamped = Frame(frame.can_id, frame.data, frame.extended, time.time())
        with self._lock:
            if self.keep_log:
                self.log.append(stamped)
            nodes = list(self._nodes)
        for n in nodes:
            if n is not sender and not n._closed:
                n._q.put(stamped)

    def _remove(self, node: "MemoryBus") -> None:
        with self._lock:
            if node in self._nodes:
                self._nodes.remove(node)


class MemoryBus(Bus):
    def __init__(self, hub: MemoryHub) -> None:
        self._hub = hub
        self._q: "queue.Queue[Frame]" = queue.Queue()
        self._closed = False

    def send(self, frame: Frame) -> None:
        self._hub._deliver(self, frame)

    def recv(self, timeout: float) -> Optional[Frame]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed = True
        self._hub._remove(self)
