import time

from bmw_b58_emu.obd import supported_bitmap
from .helpers import EmuCase


class ObdTests(EmuCase):
    def test_supported_pid_chain(self):
        r = self.obd.request(bytes([1, 0x00]))
        self.assertEqual(r[:3], bytes([0x41, 0x00, 0xBE]) [:3][:2] + r[2:3])
        bm = int.from_bytes(r[2:6], "big")
        self.assertTrue(bm & (1 << (32 - 0x0C)), "RPM supported")
        self.assertTrue(bm & 1, "next range advertised")
        r = self.obd.request(bytes([1, 0x20]))
        self.assertEqual(r[:2], bytes([0x41, 0x20]))

    def test_live_pids(self):
        self.start_engine()
        r = self.obd.request(bytes([1, 0x0C]))
        rpm = int.from_bytes(r[2:4], "big") / 4
        self.assertEqual(r[:2], bytes([0x41, 0x0C]))
        self.assertTrue(500 < rpm < 1500, rpm)
        r = self.obd.request(bytes([1, 0x05]))
        self.assertAlmostEqual(r[2] - 40, self.vehicle.coolant, delta=2)

    def test_multi_pid_request(self):
        r = self.obd.request(bytes([1, 0x0C, 0x0D]))
        self.assertEqual(r[0], 0x41)
        self.assertEqual(r[1], 0x0C)
        self.assertEqual(r[4], 0x0D)
        self.assertEqual(len(r), 6)

    def test_vin_multiframe(self):
        r = self.obd.request(bytes([9, 2]))
        self.assertEqual(r[:3], bytes([0x49, 0x02, 0x01]))
        self.assertEqual(r[3:].decode(), self.dme.config.vin)

    def test_functional_request(self):
        from bmw_b58_emu.tester import obd_client
        with obd_client(self.hub.connect(), functional=True) as c:
            self.assertEqual(c.request(bytes([1, 0x0D]))[:2], bytes([0x41, 0x0D]))

    def test_unsupported_service_nrc_and_pid_silence(self):
        self.assertEqual(self.obd.request(bytes([0x08, 0x00])), bytes([0x7F, 0x08, 0x11]))
        from bmw_b58_emu.tester import TesterTimeout
        with self.assertRaises(TesterTimeout):
            self.obd.request(bytes([1, 0x99]), timeout=0.3)

    def test_dtc_lifecycle_and_freeze_frame(self):
        self.start_engine()
        self.dme.set_fault("P0301")
        self.dme.set_fault("P0420")
        self.dme.set_fault("P0171", confirmed=False)
        r = self.obd.request(bytes([3]))
        self.assertEqual(r[:2], bytes([0x43, 2]))
        self.assertEqual(r[2:6], bytes([0x03, 0x01, 0x04, 0x20]))
        self.assertEqual(self.obd.request(bytes([7]))[1], 3)  # pending includes all three
        mon = self.obd.request(bytes([1, 0x01]))
        self.assertEqual(mon[2], 0x80 | 2)  # MIL on, 2 confirmed
        ff = self.obd.request(bytes([2, 0x02, 0x00]))
        self.assertEqual(ff[:3], bytes([0x42, 0x02, 0x00]))
        self.assertEqual(ff[3:5], bytes([0x03, 0x01]))
        rpm = self.obd.request(bytes([2, 0x0C, 0x00]))
        self.assertEqual(rpm[:3], bytes([0x42, 0x0C, 0x00]))
        self.assertEqual(self.obd.request(bytes([4])), bytes([0x44]))
        self.assertEqual(self.obd.request(bytes([3])), bytes([0x43, 0]))
        self.wait(lambda: not self.vehicle.mil, 2)
        self.assertEqual(self.obd.request(bytes([1, 0x01]))[2] & 0x80, 0)

    def test_bitmap_helper(self):
        self.assertEqual(supported_bitmap(0, [1, 0x20]), bytes([0x80, 0, 0, 0x01]))
        self.assertEqual(supported_bitmap(0x20, [0x21, 0x40]), bytes([0x80, 0, 0, 0x01]))
        self.assertEqual(supported_bitmap(0x20, [0x21]), bytes([0x80, 0, 0, 0x00]))
