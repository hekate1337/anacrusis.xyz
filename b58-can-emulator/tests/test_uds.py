import time

from bmw_b58_emu import uds as udsmod
from bmw_b58_emu.dids import LIVE_DIDS
from bmw_b58_emu.tester import TesterTimeout, bmw_client
from .helpers import EmuCase


class UdsTests(EmuCase):
    def unlock(self, level=1):
        self.assertEqual(self.bmw.request(b"\x10\x03")[:2], b"\x50\x03")
        r = self.bmw.request(bytes([0x27, level]))
        seed = int.from_bytes(r[2:6], "big")
        key = udsmod.compute_key(seed, level).to_bytes(4, "big")
        self.assertEqual(self.bmw.request(bytes([0x27, level + 1]) + key), bytes([0x67, level + 1]))

    def test_session_and_tester_present(self):
        self.assertEqual(self.bmw.request(b"\x10\x03"), bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4]))
        self.assertEqual(self.bmw.request(b"\x22\xF1\x86"), bytes([0x62, 0xF1, 0x86, 0x03]))
        self.assertEqual(self.bmw.request(b"\x3E\x00"), b"\x7E\x00")
        with self.assertRaises(TesterTimeout):  # suppressPosRspMsgIndication
            self.bmw.request(b"\x3E\x80", timeout=0.3)
        self.assertEqual(self.bmw.request(b"\x10\x09"), bytes([0x7F, 0x10, 0x12]))
        self.assertEqual(self.bmw.request(b"\x10"), bytes([0x7F, 0x10, 0x13]))

    def test_s3_timeout(self):
        orig = udsmod.S3_TIMEOUT
        udsmod.S3_TIMEOUT = 0.4
        self.addCleanup(setattr, udsmod, "S3_TIMEOUT", orig)
        self.bmw.request(b"\x10\x03")
        time.sleep(0.9)
        self.assertEqual(self.bmw.request(b"\x22\xF1\x86")[-1], 0x01)

    def test_read_vin_and_multi_did_multiframe(self):
        r = self.bmw.request(b"\x22\xF1\x90")
        self.assertEqual(r[:3], bytes([0x62, 0xF1, 0x90]))
        self.assertEqual(r[3:].decode(), self.dme.config.vin)
        r = self.bmw.request(b"\x22\xF1\x90\xF1\x89\xF1\x91\xF1\x8C")
        self.assertEqual(len(r), 1 + 4 * 2 + 17 + 16 * 3)
        self.assertEqual(self.bmw.request(b"\x22\xF1\x77"), bytes([0x7F, 0x22, 0x31]))

    def test_block_size_and_stmin_respected(self):
        from bmw_b58_emu.isotp import IsoTpConfig
        from bmw_b58_emu.tester import DiagClient
        self.bmw.stop()  # two testers would both answer with flow control
        cfg = IsoTpConfig(rx_ids=(0x612,), tx_id=0x6F1, rx_ext=(0xF1,), tx_ext=0x12, block_size=2, stmin=2)
        with DiagClient(self.hub.connect(), cfg) as c:
            t0 = time.monotonic()
            r = c.request(b"\x22\xF1\x90\xF1\x89\xF1\x91\xF1\x8C")
            self.assertEqual(len(r), 1 + 8 + 17 + 48)
            self.assertGreater(time.monotonic() - t0, 0.01)

    def test_live_data_dids(self):
        self.start_engine()
        did = next(d for d in LIVE_DIDS if d.key == "rpm")
        r = self.bmw.request(bytes([0x22]) + did.did.to_bytes(2, "big"))
        self.assertTrue(500 < did.decode(r[3:]) < 1500)
        batt = next(d for d in LIVE_DIDS if d.key == "battery")
        r = self.bmw.request(bytes([0x22]) + batt.did.to_bytes(2, "big"))
        self.assertTrue(13.0 < batt.decode(r[3:]) < 15.0)

    def test_security_access(self):
        self.assertEqual(self.bmw.request(b"\x27\x01"), bytes([0x7F, 0x27, 0x7F]))  # default session
        self.bmw.request(b"\x10\x03")
        self.assertEqual(self.bmw.request(b"\x27\x02\x00\x00\x00\x00"), bytes([0x7F, 0x27, 0x24]))
        self.assertEqual(self.bmw.request(b"\x27\x07"), bytes([0x7F, 0x27, 0x12]))
        self.bmw.request(b"\x27\x01")
        self.assertEqual(self.bmw.request(b"\x27\x02\x00\x00\x00\x00"), bytes([0x7F, 0x27, 0x35]))
        self.unlock()
        self.assertEqual(self.bmw.request(b"\x27\x01"), bytes([0x67, 0x01, 0, 0, 0, 0]))  # already unlocked

    def test_security_lockout(self):
        self.bmw.request(b"\x10\x03")
        codes = []
        for _ in range(3):
            self.bmw.request(b"\x27\x01")
            codes.append(self.bmw.request(b"\x27\x02\x01\x02\x03\x04")[2])
        self.assertEqual(codes, [0x35, 0x35, 0x36])
        self.assertEqual(self.bmw.request(b"\x27\x01"), bytes([0x7F, 0x27, 0x37]))

    def test_write_did_requires_session_and_security(self):
        vin = b"WBA5B58000FA00099"
        self.assertEqual(self.bmw.request(b"\x2E\xF1\x90" + vin), bytes([0x7F, 0x2E, 0x7F]))
        self.bmw.request(b"\x10\x03")
        self.assertEqual(self.bmw.request(b"\x2E\xF1\x90" + vin), bytes([0x7F, 0x2E, 0x33]))
        self.unlock()
        self.assertEqual(self.bmw.request(b"\x2E\xF1\x90" + vin), bytes([0x6E, 0xF1, 0x90]))
        self.assertEqual(self.bmw.request(b"\x2E\xF1\x90" + vin[:-1]), bytes([0x7F, 0x2E, 0x13]))
        self.assertEqual(self.bmw.request(b"\x2E\xF1\x90" + b"WBAIOQ" + vin[6:]), bytes([0x7F, 0x2E, 0x31]))
        self.assertEqual(self.bmw.request(b"\x22\xF1\x90")[3:], vin)
        self.assertEqual(self.obd.request(bytes([9, 2]))[3:], vin)
        self.assertEqual(self.bmw.request(b"\x2E\xC0\x00" + bytes(range(16)))[:1], b"\x6E")
        self.assertEqual(self.bmw.request(b"\x22\xC0\x00")[3:], bytes(range(16)))

    def test_dtc_services(self):
        self.start_engine()
        self.dme.set_fault("P0301")
        self.dme.set_fault("P0299", confirmed=False)
        r = self.bmw.request(b"\x19\x01\x08")
        self.assertEqual(r, bytes([0x59, 0x01, 0xFF, 0x01, 0x00, 0x01]))
        r = self.bmw.request(b"\x19\x02\xFF")
        self.assertEqual(r[:3], bytes([0x59, 0x02, 0xFF]))
        recs = [(r[i : i + 3].hex(), r[i + 3]) for i in range(3, len(r), 4)]
        self.assertEqual(recs, [("029900", 0x27), ("030100", 0xAF)])
        r = self.bmw.request(b"\x19\x04\x03\x01\x00\x01")
        self.assertEqual(r[:7], bytes([0x59, 0x04, 0x03, 0x01, 0x00, 0xAF, 0x01]))
        self.assertGreater(r[7], 0)
        r = self.bmw.request(b"\x19\x06\x03\x01\x00\xFF")
        self.assertEqual(r[-2:], bytes([0x01, 0x02]) [:1] + r[-1:])
        self.assertEqual(self.bmw.request(b"\x19\x04\x01\x23\x00\x01"), bytes([0x7F, 0x19, 0x31]))
        self.assertGreater(len(self.bmw.request(b"\x19\x0A")), 20 * 4)
        self.assertEqual(self.bmw.request(b"\x14\x03\x01\x00"), b"\x54")
        self.assertEqual(self.bmw.request(b"\x19\x01\xFF")[-2:], b"\x00\x01")
        self.assertEqual(self.bmw.request(b"\x14\xFF\xFF\xFF"), b"\x54")
        self.assertEqual(self.bmw.request(b"\x19\x01\xFF")[-2:], b"\x00\x00")

    def test_control_dtc_setting_and_comm_control(self):
        self.assertEqual(self.bmw.request(b"\x85\x02"), bytes([0x7F, 0x85, 0x7F]))
        self.bmw.request(b"\x10\x03")
        self.assertEqual(self.bmw.request(b"\x85\x02"), b"\xC5\x02")
        self.assertIsNone(self.dme.set_fault("P0300"))
        self.bmw.request(b"\x10\x01")  # back to default restores logging
        self.assertIsNotNone(self.dme.set_fault("P0300"))
        self.bmw.request(b"\x10\x03")
        self.assertEqual(self.bmw.request(b"\x28\x01\x01"), b"\x68\x01")
        self.assertFalse(self.dme.comm_tx_enabled)
        self.assertEqual(self.bmw.request(b"\x28\x00\x01"), b"\x68\x00")
        self.assertTrue(self.dme.comm_tx_enabled)

    def test_routines(self):
        self.assertEqual(self.bmw.request(b"\x31\x01\xF0\x01"), bytes([0x7F, 0x31, 0x7F]))
        self.bmw.request(b"\x10\x03")
        self.vehicle.ltft = 5.0
        self.assertEqual(self.bmw.request(b"\x31\x01\xF0\x01"), bytes([0x71, 0x01, 0xF0, 0x01, 0x00]))
        self.assertEqual(self.vehicle.ltft, 0.0)
        self.assertEqual(self.bmw.request(b"\x31\x01\xFF\x00"), bytes([0x7F, 0x31, 0x22]))
        self.assertEqual(self.bmw.request(b"\x31\x01\x12\x34"), bytes([0x7F, 0x31, 0x31]))

    def test_unknown_service_and_functional_silence(self):
        self.assertEqual(self.bmw.request(b"\x34\x00"), bytes([0x7F, 0x34, 0x11]))
        with bmw_client(self.hub.connect(), functional=True) as f:
            self.assertEqual(f.request(b"\x3E\x00"), b"\x7E\x00")
            with self.assertRaises(TesterTimeout):
                f.request(b"\x34\x00", timeout=0.3)

    def test_uds_also_on_obd_ids(self):
        self.assertEqual(self.obd.request(b"\x3E\x00"), b"\x7E\x00")
        self.assertEqual(self.obd.request(b"\x22\xF1\x90")[3:].decode(), self.dme.config.vin)

    def test_ecu_reset_goes_briefly_offline(self):
        self.bmw.request(b"\x10\x03")
        self.assertEqual(self.bmw.request(b"\x11\x01"), b"\x51\x01")
        time.sleep(0.2)
        with self.assertRaises(TesterTimeout):
            self.bmw.request(b"\x3E\x00", timeout=0.3)
        self.wait(self.dme.online, 3)
        self.assertEqual(self.bmw.request(b"\x22\xF1\x86")[-1], 0x01)  # session reset
