"""
Climate chamber driver.

Chambers rarely speak real SCPI (Vötsch/Weiss use the SimPac/S!MPATI ASCII
protocols, ESPEC its own); the driver therefore has the same dialect-table
shape as the VNA so a protocol adapter is a table, not a rewrite.  The
default dialect is a SCPI-style one that the simulator implements.

The important part is not the commands but :meth:`Chamber.wait_stable`,
which encodes what "at temperature" means: the air temperature has stayed
within ``tol`` of the setpoint for ``stable_s`` seconds *and* its slope is
below ``max_slope_k_per_min``.  A separate *sample soak* (from the procedure)
then lets the cable core follow the air.  The full temperature log of the
wait is returned so it can be archived with the measurement.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..clock import Clock, WallClock
from ..scpi import ScpiError, ScpiInstrument, open_transport
from . import Capabilities, register

__all__ = ["Chamber", "StabilityCriterion", "TemperatureLog"]

log = logging.getLogger("labauto.chamber")

GENERIC_DIALECT = {
    "name": "generic-scpi-chamber",
    "setpoint": "SOUR:TEMP {value}",
    "setpoint?": "SOUR:TEMP?",
    "temperature?": "MEAS:TEMP?",
    "humidity?": "MEAS:HUM?",
    "output": "OUTP {value}",
    "output?": "OUTP?",
    "limits?": "SYST:TEMP:LIM?",
}


@dataclass
class StabilityCriterion:
    tol_k: float = 0.5
    stable_s: float = 300.0
    max_slope_k_per_min: float = 0.2
    sample_s: float = 10.0
    timeout_s: float = 4 * 3600.0


@dataclass
class TemperatureLog:
    t: list[float] = field(default_factory=list)          # unix time
    air_c: list[float] = field(default_factory=list)
    setpoint_c: list[float] = field(default_factory=list)

    def add(self, t, air, sp):
        self.t.append(float(t)), self.air_c.append(float(air)), self.setpoint_c.append(float(sp))

    def to_dict(self, every: int = 1) -> dict:
        return {"t": self.t[::every], "air_c": self.air_c[::every], "setpoint_c": self.setpoint_c[::every]}

    def slope_k_per_min(self, window_s: float) -> float:
        if len(self.t) < 2:
            return float("inf")
        t0 = self.t[-1] - window_s
        idx = [i for i, x in enumerate(self.t) if x >= t0]
        if len(idx) < 2:
            return float("inf")
        i0, i1 = idx[0], idx[-1]
        dt = self.t[i1] - self.t[i0]
        return 60.0 * (self.air_c[i1] - self.air_c[i0]) / dt if dt > 0 else float("inf")


class Chamber(ScpiInstrument):
    role = "chamber"

    def __init__(self, transport, dialect: dict | None = None, clock: Clock | None = None,
                 capabilities: Capabilities | None = None):
        super().__init__(transport)
        self.d = dialect or GENERIC_DIALECT
        self.clock = clock or WallClock()
        self._caps = capabilities

    @classmethod
    def open(cls, address: str, *, sim_bench=None, clock=None, capabilities: dict | None = None,
             timeout: float = 10.0, **_):
        caps = Capabilities(role="chamber", **capabilities) if capabilities else None
        return cls(open_transport(address, timeout=timeout), None, clock, caps)

    @property
    def capabilities(self) -> Capabilities:
        if self._caps is None:
            try:
                lo, hi = (float(x) for x in self.query(self.d["limits?"]).split(","))
            except (ScpiError, ValueError):
                lo, hi = -40.0, 125.0
            self._caps = Capabilities("chamber", tmin_c=lo, tmax_c=hi, features={"temperature"})
        return self._caps

    def set_temperature(self, t_c: float) -> None:
        caps = self.capabilities
        if not (caps.tmin_c <= t_c <= caps.tmax_c):
            raise ValueError(f"setpoint {t_c} C outside chamber range {caps.tmin_c}..{caps.tmax_c} C")
        self.write(self.d["setpoint"].format(value=f"{t_c:.2f}"))
        self.write(self.d["output"].format(value="ON"))
        self.raise_errors()

    def setpoint(self) -> float:
        return self.query_float(self.d["setpoint?"])

    def temperature(self) -> float:
        return self.query_float(self.d["temperature?"])

    def humidity(self) -> float | None:
        try:
            return self.query_float(self.d["humidity?"])
        except (ScpiError, ValueError):
            return None

    def off(self) -> None:
        self.write(self.d["output"].format(value="OFF"))

    def wait_stable(self, target_c: float, crit: StabilityCriterion, tlog: TemperatureLog | None = None,
                    progress=None) -> TemperatureLog:
        """Block (on the clock) until the stability criterion holds; raise TimeoutError otherwise."""
        tlog = tlog or TemperatureLog()
        t0 = self.clock.time()
        in_band_since: float | None = None
        while True:
            now = self.clock.time()
            air = self.temperature()
            tlog.add(now, air, self.setpoint())
            if progress:
                progress(now - t0, air)
            if abs(air - target_c) <= crit.tol_k:
                in_band_since = now if in_band_since is None else in_band_since
                slope = abs(tlog.slope_k_per_min(min(crit.stable_s, now - in_band_since + crit.sample_s)))
                if now - in_band_since >= crit.stable_s and slope <= crit.max_slope_k_per_min:
                    log.info("chamber stable at %.2f C (target %.1f) after %.0f s", air, target_c, now - t0)
                    return tlog
            else:
                in_band_since = None
            if now - t0 > crit.timeout_s:
                raise TimeoutError(f"chamber did not stabilise at {target_c} C within {crit.timeout_s:.0f} s "
                                   f"(last {air:.2f} C)")
            self.clock.sleep(crit.sample_s)

    def state(self) -> dict:
        return {"identity": self.identity.to_dict(), "address": self.address, "dialect": self.d["name"],
                "setpoint_c": self.setpoint(), "air_c": self.temperature(), "humidity_pct": self.humidity(),
                "capabilities": self.capabilities.to_dict()}


@register("chamber.scpi")
class ScpiChamber(Chamber):
    pass


@register("chamber.sim")
class SimChamber(Chamber):
    @classmethod
    def open(cls, address: str, *, sim_bench=None, clock=None, capabilities: dict | None = None, **_):
        from ..scpi import SimTransport
        from ..sim.chamber import ChamberSim
        if sim_bench is None:
            raise ScpiError("chamber.sim needs a simulation bench")
        sim = ChamberSim(sim_bench, clock)
        inst = cls(SimTransport(sim, "SIM::CHAMBER"), None, clock, Capabilities("chamber", **capabilities) if capabilities else sim.capabilities())
        inst.sim = sim
        return inst
