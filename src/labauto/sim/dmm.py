"""SCPI four-wire ohmmeter simulator: reads the loop resistance of the connected sample."""
from __future__ import annotations

import numpy as np

from ..scpi import normalise, split_commands

__all__ = ["DMMSim"]


class DMMSim:
    def __init__(self, bench, seed: int = 3):
        self.bench = bench
        self.rng = np.random.default_rng(seed)
        self.idn = "labauto-sim,DMM-6.5,SIM0003,1.1"
        self.errors: list[tuple[int, str]] = []
        self.range = 100.0
        self.contact_mohm = 2.0

    def handle(self, cmd: str) -> str | None:
        out = None
        for c in split_commands(cmd):
            out = self._one(c)
        return out

    def _one(self, c: str) -> str | None:
        head, _, arg = c.partition(" ")
        h = normalise(head)
        if h == "*IDN?":
            return self.idn
        if h in ("*RST", "*CLS"):
            self.errors.clear(); return None
        if h == "*OPC?":
            return "1"
        if h == "SYST:ERR?":
            if self.errors:
                code, msg = self.errors.pop(0)
                return f'{code},"{msg}"'
            return '0,"No error"'
        if h == "CONF:FRES":
            self.range = float(arg.split(",")[0]); return None
        if h == "READ?":
            r = self.bench.loop_resistance()
            if r is None:
                return "9.9e37"          # overload
            r += 1e-3 * self.contact_mohm * self.rng.normal(1.0, 0.3) + self.rng.normal(0, 2e-4)
            return f"{r:.6e}"
        self.errors.append((-113, "Undefined header;" + c))
        return None
