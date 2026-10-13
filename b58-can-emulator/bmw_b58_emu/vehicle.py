"""Simplified B58B30 (3.0 L turbo I6) + ZF 8HP + vehicle physics model.

This is a plausibility model, not a calibrated simulation: it exists so that
diagnostic tools see coherent, moving data (rpm follows gears, boost follows
load, coolant warms up, fuel is consumed, ...).
"""
from __future__ import annotations

import math
import random
import threading
from enum import IntEnum
from typing import Dict, List, Optional, Tuple

GEAR_RATIOS = (5.000, 3.200, 2.143, 1.720, 1.314, 1.000, 0.822, 0.640)
FINAL_DRIVE = 3.154
WHEEL_RADIUS = 0.33
MASS_KG = 1750.0
DISPLACEMENT_L = 2.998
PEAK_TORQUE_NM = 500.0
REDLINE_RPM = 7000.0
SPEED_LIMIT_KMH = 250.0  # electronic limiter
TANK_L = 59.0
BARO_KPA = 101.3

_TORQUE_CURVE = ((0, 0.0), (1000, 0.35), (1500, 0.8), (1800, 1.0), (5000, 1.0), (6000, 0.85), (7000, 0.65), (8000, 0.4))


def _interp(points, x: float) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class Ignition(IntEnum):
    OFF = 0  # KL30 only
    ACCESSORY = 1  # KL R
    ON = 2  # KL15


class EngineState(IntEnum):
    OFF = 0
    CRANKING = 1
    RUNNING = 2


# (duration_s, target_speed_kmh) - a compact urban/extra-urban loop
DRIVE_CYCLE: List[Tuple[float, float]] = [
    (6, 0), (10, 30), (12, 50), (8, 50), (8, 0), (5, 0), (12, 60), (15, 90), (20, 110), (10, 60), (10, 0),
]


class Driver:
    """Virtual driver that produces pedal / brake commands."""

    MODES = ("manual", "idle", "cruise", "wot", "cycle")

    def __init__(self) -> None:
        self.mode = "idle"
        self.target_speed = 0.0
        self._integral = 0.0
        self._cycle_t = 0.0

    def configure(self, mode: str, target_speed: Optional[float] = None) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {self.MODES}")
        self.mode = mode
        if target_speed is not None:
            self.target_speed = clamp(float(target_speed), 0, 280)
        self._integral = 0.0
        self._cycle_t = 0.0

    def update(self, veh: "Vehicle", dt: float) -> None:
        if self.mode == "manual":
            return
        if self.mode == "idle":
            veh.pedal, veh.brake = 0.0, (30.0 if veh.speed > 1 else 0.0)
        elif self.mode == "wot":
            veh.pedal, veh.brake = 100.0, 0.0
        elif self.mode in ("cruise", "cycle"):
            if self.mode == "cycle":
                self._cycle_t += dt
                t, total = self._cycle_t, sum(d for d, _ in DRIVE_CYCLE)
                t %= total
                for dur, spd in DRIVE_CYCLE:
                    if t < dur:
                        self.target_speed = spd
                        break
                    t -= dur
            err = self.target_speed - veh.speed
            self._integral = clamp(self._integral + err * dt, -20, 40)
            pedal = clamp(0.8 * err + 0.4 * self._integral + 4.0, 0, 70 if self.mode == "cycle" else 100)
            if self.target_speed < 1 and veh.speed < 2:
                pedal = 0.0
            veh.pedal = pedal
            veh.brake = clamp(-err * 4 - 10, 0, 60) if err < -6 or (self.target_speed < 1 and veh.speed > 1) else 0.0


SIGNALS = (
    "rpm", "speed", "coolant", "oil", "iat", "ambient", "baro", "map", "boost", "throttle", "pedal", "brake",
    "load", "torque", "maf", "timing", "rail_kpa", "lambda_", "lambda_cmd", "stft", "ltft", "battery",
    "fuel_level", "fuel_rate_lph", "odometer", "run_time", "gear", "cat_temp",
)


class Vehicle:
    def __init__(self, ambient: float = 20.0, fuel_level: float = 65.0, odometer_km: float = 12345.6, thermal_scale: float = 1.0) -> None:
        self._lock = threading.RLock()
        self.driver = Driver()
        self.thermal_scale = thermal_scale
        self.overrides: Dict[str, float] = {}
        self.misfire = False  # set by the DME when a P030x fault is active
        self.ignition = Ignition.OFF
        self.engine = EngineState.OFF
        self.ambient = ambient
        self.baro = BARO_KPA
        self.rpm = 0.0
        self.speed = 0.0
        self.pedal = 0.0
        self.brake = 0.0
        self.gear = 0
        self.coolant = ambient
        self.oil = ambient
        self.iat = ambient
        self.cat_temp = ambient
        self.map_kpa = BARO_KPA
        self.throttle = 0.0
        self.load = 0.0
        self.torque = 0.0
        self.maf = 0.0
        self.timing = 0.0
        self.rail_kpa = 300.0
        self.lambda_ = 1.0
        self.lambda_cmd = 1.0
        self.stft = 0.0
        self.ltft = 1.5
        self.battery = 12.6
        self.fuel_level = fuel_level
        self.fuel_rate_lph = 0.0
        self.odometer = odometer_km
        self.run_time = 0.0
        self.warmups = 0
        self.dist_since_clear = 0.0
        self.time_since_clear = 0.0
        self.dist_mil = 0.0
        self.time_mil = 0.0
        self.mil = False
        self._crank_t = 0.0
        self._shift_hold = 0.0
        self._t = 0.0
        self._start_coolant = ambient

    # ---- control ---------------------------------------------------------
    def set_ignition(self, state: Ignition) -> None:
        with self._lock:
            self.ignition = state
            if state != Ignition.ON:
                self.engine = EngineState.OFF

    def start_engine(self) -> bool:
        with self._lock:
            if self.ignition != Ignition.ON:
                self.ignition = Ignition.ON
            if self.engine == EngineState.OFF:
                self.engine = EngineState.CRANKING
                self._crank_t = 0.0
            return True

    def stop_engine(self) -> None:
        with self._lock:
            self.engine = EngineState.OFF

    def set_override(self, name: str, value: float) -> None:
        if name not in SIGNALS:
            raise ValueError(f"unknown signal {name!r}; valid: {', '.join(SIGNALS)}")
        with self._lock:
            self.overrides[name] = float(value)

    def clear_override(self, name: Optional[str] = None) -> None:
        with self._lock:
            if name is None:
                self.overrides.clear()
            else:
                self.overrides.pop(name, None)

    def reset_since_clear(self) -> None:
        with self._lock:
            self.dist_since_clear = 0.0
            self.time_since_clear = 0.0
            self.warmups = 0

    # ---- simulation ------------------------------------------------------
    def step(self, dt: float) -> None:
        with self._lock:
            self._t += dt
            self.driver.update(self, dt)
            if self.engine != EngineState.RUNNING:
                self.pedal = 0.0 if self.engine == EngineState.OFF else self.pedal
            self._engine(dt)
            self._drivetrain(dt)
            self._thermal(dt)
            self._misc(dt)

    def _idle_target(self) -> float:
        return 700.0 + 500.0 * clamp((70.0 - self.coolant) / 70.0, 0, 1)

    def _engine(self, dt: float) -> None:
        if self.engine == EngineState.CRANKING:
            self._crank_t += dt
            self.rpm += (230.0 - self.rpm) * min(1.0, dt * 6)
            if self._crank_t > 1.1:
                self.engine = EngineState.RUNNING
                self.run_time = 0.0
                self._start_coolant = self.coolant
                self.rpm = max(self.rpm, 900.0)
            self.load = 0.0
            self.torque = 0.0
            return
        if self.engine == EngineState.OFF:
            self.rpm = max(0.0, self.rpm - 2500.0 * dt)
            self.load = self.torque = 0.0
            return
        idle = self._idle_target()
        wheel_rpm = (self.speed / 3.6) / WHEEL_RADIUS * 60 / (2 * math.pi)
        ratio = GEAR_RATIOS[self.gear - 1] * FINAL_DRIVE if self.gear else 0.0
        target = max(idle, wheel_rpm * ratio)
        if self.speed < 25:
            target = max(target, idle + self.pedal * 30.0)  # converter slip on launch
        if self.misfire:
            target += random.uniform(-90, 90)
        self.rpm += (target - self.rpm) * min(1.0, dt * 8)
        limiter = self.rpm >= REDLINE_RPM or self.speed >= SPEED_LIMIT_KMH
        if self.rpm >= REDLINE_RPM:
            self.rpm = REDLINE_RPM - 20.0
        curve = _interp(_TORQUE_CURVE, self.rpm) * PEAK_TORQUE_NM
        friction = 20.0 + 0.008 * self.rpm
        demand = 0.0 if limiter else self.pedal / 100.0
        self._drive_torque = demand * curve
        self.torque = self._drive_torque + friction
        self.load = clamp(self.torque / PEAK_TORQUE_NM, 0, 1.2)
        self._friction = friction

    _drive_torque = 0.0
    _friction = 20.0

    def _drivetrain(self, dt: float) -> None:
        running = self.engine == EngineState.RUNNING
        if running:
            self._shift_hold = max(0.0, self._shift_hold - dt)
            wheel_rpm = (self.speed / 3.6) / WHEEL_RADIUS * 60 / (2 * math.pi)
            up_rpm = 1300.0 + self.pedal * 50.0
            down_rpm = 1000.0 + self.pedal * 20.0
            if self.gear == 0:
                self.gear = 1
            elif self._shift_hold <= 0:
                cur = wheel_rpm * GEAR_RATIOS[self.gear - 1] * FINAL_DRIVE
                if self.gear < 8 and cur > up_rpm and wheel_rpm * GEAR_RATIOS[self.gear] * FINAL_DRIVE > down_rpm + 200:
                    self.gear += 1
                    self._shift_hold = 0.7
                elif self.gear > 1 and cur < down_rpm:
                    self.gear -= 1
                    self._shift_hold = 0.7
        else:
            self.gear = 0
        v = self.speed / 3.6
        force = 0.0
        if running and self.gear:
            ratio = GEAR_RATIOS[self.gear - 1] * FINAL_DRIVE
            t = self._drive_torque
            if self.pedal < 1 and self.speed < 7:
                t = 25.0  # creep
            elif self.pedal < 1:
                t = -0.35 * self._friction  # engine braking
            force = t * ratio * 0.92 / WHEEL_RADIUS
        resist = 0.38 * v * v + (0.011 * MASS_KG * 9.81 if v > 0.05 else 0.0)
        brake = self.brake / 100.0 * 16000.0 if v > 0.0 else 0.0
        a = (force - resist - brake) / MASS_KG
        v = max(0.0, v + a * dt)
        if v == 0.0 and force < resist:
            a = 0.0
        self.speed = v * 3.6
        self.odometer += v * dt / 1000.0
        self.dist_since_clear += v * dt / 1000.0
        if self.mil:
            self.dist_mil += v * dt / 1000.0

    def _thermal(self, dt: float) -> None:
        k = dt * self.thermal_scale
        if self.engine == EngineState.RUNNING:
            target = 88.0 + self.load * 12.0
            rate = (1.0 + 3.0 * self.load) / 300.0
            self.coolant += (target - self.coolant) * min(1.0, k * rate)
            self.oil += ((self.coolant + 10 + 15 * self.load) - self.oil) * min(1.0, k / 500.0)
            self.cat_temp += ((300 + 650 * self.load) - self.cat_temp) * min(1.0, dt / 20.0)
        else:
            self.coolant += (self.ambient - self.coolant) * min(1.0, k / 3000.0)
            self.oil += (self.ambient - self.oil) * min(1.0, k / 4000.0)
            self.cat_temp += (self.ambient - self.cat_temp) * min(1.0, k / 400.0)
        boost = max(0.0, self.map_kpa - self.baro)
        target_iat = self.ambient + (self.coolant - self.ambient) * 0.12 + boost * 0.12
        self.iat += (target_iat - self.iat) * min(1.0, dt / 8.0)

    def _misc(self, dt: float) -> None:
        running = self.engine == EngineState.RUNNING
        if running:
            target_map = 30.0 + (240.0 - 30.0) * clamp(self.load, 0, 1) ** 1.5
            if self.torque > 0 and self.pedal < 1 and self.speed > 20:
                target_map = 25.0  # overrun
            rate = 4.0 if target_map > self.map_kpa else 8.0
            self.map_kpa += (target_map - self.map_kpa) * min(1.0, dt * rate)
            self.throttle = clamp(8.0 + self.pedal * 0.92, 0, 100) if self.map_kpa < self.baro else 100.0
            density = (self.map_kpa / BARO_KPA) * (293.15 / (self.iat + 273.15)) * 1.19
            self.maf = DISPLACEMENT_L * density * 0.9 * (self.rpm / 120.0)
            self.timing = 10.0 + self.rpm / 1000.0 * 2.0 - self.load * 18.0
            self.rail_kpa = 5000.0 + 22000.0 * clamp(self.load, 0, 1) + 0.9 * self.rpm
            wot = self.load > 0.9 and self.rpm > 4500
            fuel_cut = self.pedal < 1 and self.rpm > 1500 and self.speed > 20
            self.lambda_cmd = 0.88 if wot else 1.0
            self.lambda_ = 1.6 if fuel_cut else self.lambda_cmd * (1 + 0.01 * math.sin(self._t * 3.0))
            self.stft = 2.0 * math.sin(self._t * 3.0 + 1.0) + random.uniform(-0.3, 0.3)
            fuel_gs = 0.0 if fuel_cut else self.maf / (14.7 * self.lambda_cmd)
            self.fuel_rate_lph = fuel_gs / 745.0 * 3600.0
            self.fuel_level = max(0.0, self.fuel_level - self.fuel_rate_lph / 3600.0 * dt / TANK_L * 100.0)
            self.battery = 14.1 + random.uniform(-0.03, 0.03) - 0.3 * self.load
            self.run_time += dt
            self.time_since_clear += dt
            if self.mil:
                self.time_mil += dt
        else:
            self.map_kpa += (self.baro - self.map_kpa) * min(1.0, dt * 3)
            self.throttle = 0.0
            self.maf = self.timing = 0.0
            self.fuel_rate_lph = 0.0
            self.lambda_ = 1.0
            self.stft = 0.0
            self.rail_kpa += (300.0 - self.rail_kpa) * min(1.0, dt * 2)
            if self.engine == EngineState.CRANKING:
                self.battery = 10.8 + random.uniform(-0.1, 0.1)
                self.rail_kpa = 3000.0
            else:
                self.battery = 12.5 if self.ignition != Ignition.OFF else 12.6

    # ---- read side -------------------------------------------------------
    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            s = {
                "rpm": self.rpm, "speed": self.speed, "coolant": self.coolant, "oil": self.oil, "iat": self.iat,
                "ambient": self.ambient, "baro": self.baro, "map": self.map_kpa,
                "boost": max(0.0, self.map_kpa - self.baro), "throttle": self.throttle, "pedal": self.pedal,
                "brake": self.brake, "load": self.load * 100.0, "torque": self.torque, "maf": self.maf,
                "timing": self.timing, "rail_kpa": self.rail_kpa, "lambda_": self.lambda_,
                "lambda_cmd": self.lambda_cmd, "stft": self.stft, "ltft": self.ltft, "battery": self.battery,
                "fuel_level": self.fuel_level, "fuel_rate_lph": self.fuel_rate_lph, "odometer": self.odometer,
                "run_time": self.run_time, "gear": float(self.gear), "cat_temp": self.cat_temp,
            }
            s.update(self.overrides)
            s["ignition"] = int(self.ignition)
            s["engine"] = int(self.engine)
            s["warmups"] = self.warmups
            s["dist_since_clear"] = self.dist_since_clear
            s["time_since_clear"] = self.time_since_clear
            s["dist_mil"] = self.dist_mil
            s["time_mil"] = self.time_mil
            s["mil"] = self.mil
            return s
