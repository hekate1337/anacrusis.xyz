import time
import unittest

from bmw_b58_emu.broadcast import install_broadcast
from bmw_b58_emu.bus import MemoryHub
from bmw_b58_emu.dme import Dme, DmeConfig
from bmw_b58_emu.tester import bmw_client, obd_client
from bmw_b58_emu.vehicle import Ignition, Vehicle


class EmuCase(unittest.TestCase):
    broadcast = False

    def setUp(self):
        self.hub = MemoryHub()
        self.vehicle = Vehicle(thermal_scale=50)
        self.dme = Dme(self.hub.connect(), self.vehicle, DmeConfig())
        self.tasks = install_broadcast(self.dme) if self.broadcast else []
        self.vehicle.set_ignition(Ignition.ON)
        self.dme.start()
        self.tbus = self.hub.connect()
        self.bmw = bmw_client(self.hub.connect()).start()
        self.obd = obd_client(self.hub.connect()).start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.bmw.stop()
        self.obd.stop()
        self.dme.stop()

    def start_engine(self):
        self.vehicle.start_engine()
        self.wait(lambda: self.vehicle.engine == 2 and self.vehicle.rpm > 600, 5)

    @staticmethod
    def wait(cond, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        raise AssertionError("condition not met in time")
