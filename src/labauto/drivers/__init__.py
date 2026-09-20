"""
Driver registry.

A laboratory configuration names each instrument by *role* (``vna``,
``chamber``, ``dmm``, ``scanner``) and *driver* (``vna.scpi``, ``vna.sim``,
...).  The engine only ever talks to the role interface, so a second VNA
model is a new driver entry, not a change to the engine.

Capabilities are what a procedure checks against: a procedure that needs
four ports to 600 MHz is refused on a two-port 300 MHz analyser before
anything is measured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

__all__ = ["Capabilities", "DRIVERS", "register", "make_instrument"]


@dataclass
class Capabilities:
    role: str
    ports: int = 0
    fmin_hz: float = 0.0
    fmax_hz: float = 0.0
    tmin_c: float = 0.0
    tmax_c: float = 0.0
    features: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {"role": self.role, "ports": self.ports, "fmin_hz": self.fmin_hz, "fmax_hz": self.fmax_hz,
                "tmin_c": self.tmin_c, "tmax_c": self.tmax_c, "features": sorted(self.features)}


DRIVERS: dict[str, Callable] = {}


def register(name: str):
    def deco(cls):
        DRIVERS[name] = cls
        cls.driver_name = name
        return cls
    return deco


def make_instrument(driver: str, address: str, *, sim_bench=None, clock=None, **kw):
    """Instantiate a driver by name.  Simulated drivers receive the bench and the clock."""
    _load_all()
    if driver not in DRIVERS:
        raise KeyError(f"unknown driver {driver!r}; known: {sorted(DRIVERS)}")
    cls = DRIVERS[driver]
    return cls.open(address, sim_bench=sim_bench, clock=clock, **kw)


def _load_all():
    from . import chamber, dmm, scanner, vna  # noqa: F401  (registration side effect)
