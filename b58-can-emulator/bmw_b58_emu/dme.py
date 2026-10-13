"""The DME (engine control unit, diagnostic address 0x12) tying everything together."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from . import dtc as dtcmod
from .bus import Bus
from .dids import DidSpec, build_dids
from .isotp import IsoTpConfig, IsoTpMessage
from .node import CanNode
from .obd import ObdServer
from .uds import UdsServer
from .vehicle import Vehicle

log = logging.getLogger(__name__)

BMW_TESTER_ADDR = 0xF1
BMW_FUNCTIONAL_ADDR = 0xDF
BMW_REQUEST_ID = 0x6F1  # tester -> any ECU, first data byte = target address
OBD_FUNCTIONAL_ID = 0x7DF
OBD_REQUEST_ID = 0x7E0
OBD_RESPONSE_ID = 0x7E8


@dataclass
class DmeConfig:
    address: int = 0x12
    vin: str = "WBA5B58000FA00001"
    part_number: str = "8600000"
    supplier: str = "Bosch (emulated)"
    manufacturing_date: str = "240115"
    serial: str = "EMU0000000000001"
    sw_version: str = "B58EMU-1.0.0"
    hw_version: str = "MEVD17.2.G"
    system_name: str = "DME B58 (emulated)"
    calibration_id: str = "B58EMUCAL00001"
    cvn: str = "DEADBEEF"


class Dme:
    def __init__(self, bus: Bus, vehicle: Vehicle, config: Optional[DmeConfig] = None, state_file: Optional[str] = None) -> None:
        self.config = config or DmeConfig()
        self.vehicle = vehicle
        self.state_file = state_file
        self.dtcs = dtcmod.DtcStore()
        self.coding = bytearray(16)
        self.comm_tx_enabled = True
        self._offline_until = 0.0
        self._save_lock = threading.Lock()
        self._sim_stop = threading.Event()
        self._sim_thread: Optional[threading.Thread] = None
        self.load_state()
        self.uds = UdsServer(self)
        self.obd = ObdServer(self)
        self.dids: Dict[int, DidSpec] = build_dids(self)

        self.node = CanNode(bus, "dme")
        self.node.rx_gate = self.online
        a = self.config.address
        self.bmw_ep = self.node.add_endpoint(
            IsoTpConfig(
                rx_ids=(BMW_REQUEST_ID,),
                tx_id=0x600 + a,
                rx_ext=(a, BMW_FUNCTIONAL_ADDR),
                tx_ext=BMW_TESTER_ADDR,
                functional_ext=frozenset({BMW_FUNCTIONAL_ADDR}),
            ),
            self._on_bmw,
            "dme-bmw",
        )
        self.obd_ep = self.node.add_endpoint(
            IsoTpConfig(
                rx_ids=(OBD_FUNCTIONAL_ID, OBD_REQUEST_ID),
                tx_id=OBD_RESPONSE_ID,
                functional_ids=frozenset({OBD_FUNCTIONAL_ID}),
                padding=0x55,
            ),
            self._on_obd,
            "dme-obd",
        )
        self.node.schedule("s3", 0.25, self.uds.tick)

    # ---- lifecycle -------------------------------------------------------
    def start(self, simulate: bool = True) -> None:
        self.node.start()
        if simulate:
            self._sim_stop.clear()
            self._sim_thread = threading.Thread(target=self._sim_loop, name="vehicle-sim", daemon=True)
            self._sim_thread.start()

    def stop(self) -> None:
        self._sim_stop.set()
        if self._sim_thread:
            self._sim_thread.join(timeout=2)
        self.node.stop()
        self.save_state()

    def _sim_loop(self, hz: float = 100.0) -> None:
        last = time.monotonic()
        while not self._sim_stop.wait(1.0 / hz):
            now = time.monotonic()
            self.vehicle.step(min(now - last, 0.1))
            last = now
            self.vehicle.mil = self.dtcs.mil_on()
            self.vehicle.misfire = any(
                e.code.startswith("P030") and e.status & dtcmod.TEST_FAILED for e in self.dtcs.entries()
            )

    # ---- availability ----------------------------------------------------
    def online(self) -> bool:
        return time.monotonic() >= self._offline_until

    def reset(self, kind: int) -> None:
        def go() -> None:
            self._offline_until = time.monotonic() + 0.8
            self.uds.reset_state()
            self.save_state()

        threading.Timer(0.05, go).start()

    # ---- message handlers ------------------------------------------------
    def _on_bmw(self, msg: IsoTpMessage) -> None:
        resp = self.uds.handle(msg.payload, msg.functional)
        if resp:
            self.bmw_ep.send(resp)

    def _on_obd(self, msg: IsoTpMessage) -> None:
        if msg.payload and msg.payload[0] < 0x10:
            resp = self.obd.handle(msg.payload, msg.functional)
        else:
            resp = self.uds.handle(msg.payload, msg.functional)
        if resp:
            self.obd_ep.send(resp)

    # ---- faults ------------------------------------------------------------
    def set_fault(self, code: str, confirmed: bool = True) -> Optional[dtcmod.DtcEntry]:
        e = self.dtcs.raise_fault(code, self.vehicle.snapshot(), confirmed)
        self.save_state()
        return e

    def clear_dtcs(self, code: Optional[str]) -> None:
        self.dtcs.clear(code)
        if code is None:
            self.vehicle.reset_since_clear()
            self.vehicle.dist_mil = 0.0
            self.vehicle.time_mil = 0.0
        self.save_state()

    # ---- persistence -------------------------------------------------------
    def save_state(self) -> None:
        if not self.state_file:
            return
        data = {
            "vin": self.config.vin,
            "coding": self.coding.hex(),
            "dtcs": self.dtcs.to_state(),
            "odometer": self.vehicle.odometer,
        }
        with self._save_lock:
            tmp = f"{self.state_file}.tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.state_file)

    def load_state(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as fh:
                data = json.load(fh)
            self.config.vin = data.get("vin", self.config.vin)
            self.coding[:] = bytes.fromhex(data.get("coding", self.coding.hex()))
            self.dtcs.load_state(data.get("dtcs", []))
            self.vehicle.odometer = float(data.get("odometer", self.vehicle.odometer))
        except (OSError, ValueError, KeyError):
            log.warning("could not load state from %s", self.state_file, exc_info=True)
