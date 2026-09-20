"""
Climate chamber simulator with a two-node thermal model.

The controller ramps its internal setpoint towards the commanded value at
``ramp_k_per_min`` (chambers limit their own slew rate).  The air follows
the internal setpoint as a first-order lag with time constant tau_air, and
the *sample* (a cable coil on a rack) follows the air with a much longer
tau_dut:

    dT_air/dt = (T_ramp - T_air) / tau_air
    dT_dut/dt = (T_air  - T_dut) / tau_dut

The chamber reports the air temperature - it does not know the sample's.
That gap is why procedures carry a sample-soak time: with tau_dut = 12 min,
the core is within 0.5 K of the air 5 tau = 60 min after the air settles.
Measuring earlier gives a systematically low temperature label on the
data, which is what the temperature-sweep validation looks for.
"""
from __future__ import annotations

import numpy as np

from ..clock import Clock, SimClock, WallClock
from ..drivers import Capabilities
from ..scpi import normalise, split_commands

__all__ = ["ChamberSim"]


class ChamberSim:
    def __init__(self, bench, clock: Clock | None = None, tmin=-70.0, tmax=180.0, tau_air_s=90.0, tau_dut_s=720.0,
                 ramp_k_per_min=3.0, noise_k=0.04, seed=5):
        self.bench = bench
        self.clock = clock or WallClock()
        self.tmin, self.tmax = tmin, tmax
        self.tau_air, self.tau_dut, self.ramp = tau_air_s, tau_dut_s, ramp_k_per_min / 60.0
        self.noise = noise_k
        self.rng = np.random.default_rng(seed)
        self.idn = "labauto-sim,CHAMBER-180,SIM0002,2.3"
        self.errors: list[tuple[int, str]] = []
        self.setpoint = bench.ambient_c
        self.ramp_sp = bench.ambient_c
        self.air_c = bench.ambient_c
        self.dut_c = bench.ambient_c
        self.output = False
        self._t = self.clock.time()
        bench.chamber = self
        if isinstance(self.clock, SimClock):
            self.clock.listeners.append(self)

    def capabilities(self) -> Capabilities:
        return Capabilities("chamber", tmin_c=self.tmin, tmax_c=self.tmax, features={"temperature", "humidity"})

    # ---- thermal model
    def advance_to(self, t: float):
        dt_total = t - self._t
        if dt_total <= 0:
            return
        n = max(1, int(np.ceil(dt_total / 5.0)))
        dt = dt_total / n
        for _ in range(n):
            target = self.setpoint if self.output else self.bench.ambient_c
            step = np.clip(target - self.ramp_sp, -self.ramp * dt, self.ramp * dt)
            self.ramp_sp += step
            tau_air = self.tau_air if self.output else 4 * self.tau_air
            self.air_c += (self.ramp_sp - self.air_c) * (1 - np.exp(-dt / tau_air))
            self.dut_c += (self.air_c - self.dut_c) * (1 - np.exp(-dt / self.tau_dut))
        self._t = t

    def _sync(self):
        self.advance_to(self.clock.time())

    # ---- SCPI
    def handle(self, cmd: str) -> str | None:
        out = None
        for c in split_commands(cmd):
            out = self._one(c)
        return out

    def _one(self, c: str) -> str | None:
        head, _, arg = c.partition(" ")
        h = normalise(head)
        self._sync()
        if h == "*IDN?":
            return self.idn
        if h == "*RST":
            self.output = False; self.setpoint = self.bench.ambient_c; return None
        if h == "*CLS":
            self.errors.clear(); return None
        if h == "*OPC?":
            return "1"
        if h == "SYST:ERR?":
            if self.errors:
                code, msg = self.errors.pop(0)
                return f'{code},"{msg}"'
            return '0,"No error"'
        if h == "SYST:TEMP:LIM?":
            return f"{self.tmin:g},{self.tmax:g}"
        if h == "SOUR:TEMP":
            v = float(arg)
            if not (self.tmin <= v <= self.tmax):
                self.errors.append((-222, "Data out of range;" + c)); return None
            self.setpoint = v; return None
        if h == "SOUR:TEMP?":
            return f"{self.setpoint:.2f}"
        if h == "MEAS:TEMP?":
            return f"{self.air_c + self.rng.normal(0, self.noise):.3f}"
        if h == "MEAS:HUM?":
            return f"{max(2.0, self.bench.humidity_pct * np.exp(-(self.air_c - 23) / 40)):.1f}"
        if h == "OUTP":
            self.output = arg.strip().upper() in ("ON", "1"); return None
        if h == "OUTP?":
            return "1" if self.output else "0"
        self.errors.append((-113, "Undefined header;" + c))
        return None
