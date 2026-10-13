import json
import os
import tempfile
import unittest

from bmw_b58_emu import dtc
from bmw_b58_emu.bus import MemoryHub
from bmw_b58_emu.dme import Dme, DmeConfig
from bmw_b58_emu.vehicle import Vehicle


class DtcCodecTests(unittest.TestCase):
    def test_round_trip(self):
        for code, raw in (("P0301", b"\x03\x01"), ("P2096", b"\x20\x96"), ("C1234", b"\x52\x34"),
                          ("B0ABC", b"\x8A\xBC"), ("U3FFF", b"\xFF\xFF")):
            self.assertEqual(dtc.encode_obd(code), raw, code)
            self.assertEqual(dtc.decode_obd(raw), code)
        self.assertEqual(dtc.encode_obd(" p0420 "), b"\x04\x20")
        self.assertEqual(dtc.uds_dtc("P0300"), b"\x03\x00\x00")

    def test_invalid(self):
        for bad in ("", "P030", "P03011", "X0301", "P4301", "P03G1"):
            with self.assertRaises(ValueError, msg=bad):
                dtc.encode_obd(bad)


class DtcStoreTests(unittest.TestCase):
    def test_status_lifecycle(self):
        s = dtc.DtcStore()
        e = s.raise_fault("p0171", {"rpm": 800.0})
        self.assertEqual(e.code, "P0171")
        self.assertEqual(e.occurrences, 1)
        self.assertTrue(e.status & dtc.CONFIRMED and e.status & dtc.WARNING_INDICATOR)
        self.assertTrue(s.mil_on())
        s.raise_fault("P0171", {"rpm": 3000.0})
        self.assertEqual(e.occurrences, 2)
        self.assertEqual(e.freeze, {"rpm": 800.0}, "freeze frame is kept from the first occurrence")
        s.heal("P0171")
        self.assertFalse(e.status & (dtc.TEST_FAILED | dtc.PENDING | dtc.WARNING_INDICATOR))
        self.assertTrue(e.status & dtc.CONFIRMED)
        self.assertFalse(s.mil_on())
        self.assertEqual(s.entries(dtc.PENDING), [])
        self.assertEqual([x.code for x in s.entries(dtc.CONFIRMED)], ["P0171"])

    def test_pending_only_and_logging_disabled(self):
        s = dtc.DtcStore()
        e = s.raise_fault("P0420", confirmed=False)
        self.assertTrue(e.status & dtc.PENDING)
        self.assertFalse(e.status & dtc.CONFIRMED)
        self.assertFalse(s.mil_on())
        s.logging_enabled = False
        self.assertIsNone(s.raise_fault("P0300"))
        self.assertIsNone(s.get("P0300"))

    def test_clear(self):
        s = dtc.DtcStore()
        s.raise_fault("P0301")
        s.raise_fault("P0302")
        self.assertEqual(s.clear("p0301"), 1)
        self.assertEqual(s.clear("P0301"), 0)
        self.assertEqual(s.clear(), 1)
        self.assertEqual(s.entries(), [])


class StatePersistenceTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, "state.json")

    def make_dme(self):
        return Dme(MemoryHub().connect(), Vehicle(), DmeConfig(), self.path)

    def test_state_survives_restart(self):
        a = self.make_dme()
        a.config.vin = "WBA11111111111111"
        a.coding[:4] = b"\xDE\xAD\xBE\xEF"
        a.vehicle.odometer = 54321.5
        a.set_fault("P0301")
        a.save_state()

        b = self.make_dme()
        self.assertEqual(b.config.vin, "WBA11111111111111")
        self.assertEqual(bytes(b.coding[:4]), b"\xDE\xAD\xBE\xEF")
        self.assertAlmostEqual(b.vehicle.odometer, 54321.5)
        e = b.dtcs.get("P0301")
        self.assertIsNotNone(e)
        self.assertTrue(e.status & dtc.CONFIRMED)
        self.assertIsNotNone(e.freeze)

        b.clear_dtcs(None)
        self.assertEqual(json.load(open(self.path))["dtcs"], [])

    def test_corrupt_state_file_is_ignored(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        with self.assertLogs("bmw_b58_emu.dme", "WARNING"):
            d = self.make_dme()
        self.assertEqual(d.config.vin, DmeConfig().vin)
        self.assertEqual(d.dtcs.entries(), [])

    def test_no_state_file_writes_nothing(self):
        d = Dme(MemoryHub().connect(), Vehicle(), DmeConfig())
        d.set_fault("P0300")
        self.assertFalse(os.path.exists(self.path))
