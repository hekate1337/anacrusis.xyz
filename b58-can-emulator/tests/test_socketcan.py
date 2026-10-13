"""End-to-end tests over a real SocketCAN interface.

Skipped unless the interface named by $B58_VCAN_IFACE (default vcan0) exists and is up, e.g. after
`sudo scripts/setup_vcan.sh vcan0`. CI creates it in a dedicated job.
"""
import os
import subprocess
import sys
import time
import unittest

from bmw_b58_emu.broadcast import install_broadcast
from bmw_b58_emu.bus import Frame, SocketCanBus
from bmw_b58_emu.dme import Dme, DmeConfig
from bmw_b58_emu.tester import bmw_client, obd_client
from bmw_b58_emu.vehicle import Ignition, Vehicle

IFACE = os.environ.get("B58_VCAN_IFACE", "vcan0")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _vcan_available() -> bool:
    try:
        SocketCanBus(IFACE).close()
        return True
    except OSError:
        return False


# CI sets B58_REQUIRE_VCAN=1 in the SocketCAN job so a broken interface fails instead of skipping.
_REQUIRED = os.environ.get("B58_REQUIRE_VCAN") == "1"


@unittest.skipUnless(_REQUIRED or _vcan_available(), f"SocketCAN interface {IFACE} not available")
class SocketCanTests(unittest.TestCase):
    def open(self):
        bus = SocketCanBus(IFACE)
        self.addCleanup(bus.close)
        return bus

    def recv_until(self, bus, pred, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            f = bus.recv(0.1)
            if f is not None and pred(f):
                return f
        self.fail("frame not received")

    def test_raw_frames_round_trip(self):
        a, b = self.open(), self.open()
        a.send(Frame(0x123, b"\x01\x02\x03"))
        f = self.recv_until(b, lambda f: f.can_id == 0x123)
        self.assertEqual((f.data, f.extended), (b"\x01\x02\x03", False))
        a.send(Frame(0x18DAF112, b"\xAA" * 8, extended=True))
        f = self.recv_until(b, lambda f: f.can_id == 0x18DAF112)
        self.assertEqual((f.data, f.extended), (b"\xAA" * 8, True))

    def test_dme_over_socketcan(self):
        vehicle = Vehicle(thermal_scale=50)
        dme = Dme(self.open(), vehicle, DmeConfig())
        install_broadcast(dme)
        vehicle.set_ignition(Ignition.ON)
        dme.start()
        self.addCleanup(dme.stop)
        vehicle.start_engine()

        with obd_client(self.open()) as obd, bmw_client(self.open()) as bmw:
            self.assertEqual(obd.request(bytes([9, 2]))[3:], DmeConfig().vin.encode())  # multi-frame
            self.assertEqual(bmw.request(bytes([0x22, 0xF1, 0x90])), b"\x62\xF1\x90" + DmeConfig().vin.encode())
            end = time.monotonic() + 5
            while True:
                r = obd.request(bytes([1, 0x0C]))
                if int.from_bytes(r[2:4], "big") / 4 > 500 or time.monotonic() > end:
                    break
                time.sleep(0.1)
            self.assertGreater(int.from_bytes(r[2:4], "big") / 4, 500)

        sniff = self.open()
        self.recv_until(sniff, lambda f: f.can_id == 0x0A5)  # cyclic engine frame

    def test_command_line_end_to_end(self):
        env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
        emu = subprocess.Popen(
            [sys.executable, "-m", "bmw_b58_emu", "-i", IFACE, "--api-port", "0", "--start-engine"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(emu.wait, 5)
        self.addCleanup(emu.terminate)
        self.assertIn("B58 DME emulator on", emu.stdout.readline())

        def ask(*args):
            return subprocess.run([sys.executable, "-m", "bmw_b58_emu.tester", "-i", IFACE, *args],
                                  cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)

        r = ask("obd", "01", "0C")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("41 0C"), r.stdout)
        r = ask("bmw", "22", "F1", "90")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("62 F1 90"), r.stdout)
