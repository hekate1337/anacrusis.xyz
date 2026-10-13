import time

from bmw_b58_emu.broadcast import ID_ENGINE, ID_SPEED, ID_TEMPS, crc8
from bmw_b58_emu.vehicle import EngineState, Ignition, Vehicle
from .helpers import EmuCase


class VehicleModelTests(EmuCase):
    def run_sim(self, v, seconds, dt=0.01):
        for _ in range(int(seconds / dt)):
            v.step(dt)

    def test_start_idle_and_warmup(self):
        v = Vehicle(thermal_scale=100)
        v.set_ignition(Ignition.ON)
        v.start_engine()
        self.run_sim(v, 1.5)
        self.assertEqual(v.engine, EngineState.RUNNING)
        self.assertTrue(900 < v.rpm < 1300, v.rpm)  # cold idle
        self.run_sim(v, 1.5)
        self.run_sim(v, 120)
        self.assertGreater(v.coolant, 80)
        self.assertTrue(650 < v.rpm < 800, v.rpm)

    def test_cruise_shifts_and_consumes_fuel(self):
        v = Vehicle(thermal_scale=200)
        v.set_ignition(Ignition.ON)
        v.start_engine()
        v.driver.configure("cruise", 100)
        fuel0, odo0 = v.fuel_level, v.odometer
        self.run_sim(v, 60)
        self.assertAlmostEqual(v.speed, 100, delta=4)
        self.assertGreaterEqual(v.gear, 6)
        self.assertLess(v.rpm, 2600)
        self.assertLess(v.fuel_level, fuel0)
        self.assertGreater(v.odometer - odo0, 0.5)

    def test_wot_boost_and_limiter(self):
        v = Vehicle()
        v.set_ignition(Ignition.ON)
        v.start_engine()
        v.driver.configure("wot")
        peak_boost = peak_rpm = 0.0
        for _ in range(3000):
            v.step(0.01)
            peak_boost = max(peak_boost, v.snapshot()["boost"])
            peak_rpm = max(peak_rpm, v.rpm)
        self.assertGreater(peak_boost, 80)
        self.assertLessEqual(peak_rpm, 7000)
        self.assertTrue(150 < v.speed <= 251, v.speed)

    def test_ignition_off_kills_engine(self):
        v = Vehicle()
        v.set_ignition(Ignition.ON)
        v.start_engine()
        self.run_sim(v, 3)
        v.set_ignition(Ignition.OFF)
        self.run_sim(v, 2)
        self.assertEqual(v.rpm, 0)

    def test_overrides(self):
        v = Vehicle()
        v.set_override("coolant", 123.0)
        self.assertEqual(v.snapshot()["coolant"], 123.0)
        with self.assertRaises(ValueError):
            v.set_override("nope", 1)
        v.clear_override()
        self.assertNotEqual(v.snapshot()["coolant"], 123.0)


class BroadcastTests(EmuCase):
    broadcast = True

    def collect(self, seconds):
        frames = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            f = self.tbus.recv(0.05)
            if f:
                frames.append(f)
        return frames

    def test_cyclic_frames_rates_crc_counter(self):
        self.start_engine()
        frames = self.collect(1.0)
        by = {}
        for f in frames:
            by.setdefault(f.can_id, []).append(f)
        self.assertGreater(len(by[ID_ENGINE]), 25)
        self.assertGreater(len(by[ID_TEMPS]), 5)
        prev = None
        for f in by[ID_ENGINE]:
            self.assertEqual(f.data[0], crc8(f.data[1:]))
            c = f.data[1] & 0x0F
            if prev is not None:
                self.assertEqual(c, (prev + 1) & 0x0F)
            prev = c
        last = by[ID_ENGINE][-1].data
        rpm = int.from_bytes(last[4:6], "little") / 4
        self.assertTrue(500 < rpm < 1500)
        self.assertEqual(last[7], 1)
        self.assertIn(0x512, by)

    def test_comm_control_silences_broadcast_and_bus_sleeps_with_ignition_off(self):
        self.bmw.request(b"\x10\x03")
        self.bmw.request(b"\x28\x01\x01")
        self.collect(0.1)
        self.assertEqual([f for f in self.collect(0.4) if f.can_id in (ID_ENGINE, ID_SPEED)], [])
        self.bmw.request(b"\x28\x00\x01")
        self.assertTrue([f for f in self.collect(0.3) if f.can_id == ID_ENGINE])
        self.vehicle.set_ignition(Ignition.OFF)
        self.collect(0.1)
        self.assertEqual(self.collect(0.3), [])
