"""
Four-wire resistance meter (DMM) driver - the second measurement instrument.

It exists to show that the engine is not a VNA script: a procedure can ask
for the loop resistance of the pair as a *cross-check* on the S-parameter
data.  The DC loop resistance fixes the low-frequency insertion loss
(alpha_dc = R'/(2 Z_d)), so a pair whose measured IL at the lowest
frequencies is inconsistent with its measured R_dc has a connection,
calibration or labelling problem - and the trust checks say so.
"""
from __future__ import annotations

from ..scpi import ScpiError, ScpiInstrument, open_transport
from . import Capabilities, register

__all__ = ["DMM"]

GENERIC_DIALECT = {
    "name": "generic-scpi-dmm",
    "configure_fres": "CONF:FRES {range},{resolution}",
    "read?": "READ?",
}


class DMM(ScpiInstrument):
    role = "dmm"

    def __init__(self, transport, dialect: dict | None = None, capabilities: Capabilities | None = None):
        super().__init__(transport)
        self.d = dialect or GENERIC_DIALECT
        self._caps = capabilities or Capabilities("dmm", features={"4-wire-resistance"})

    @classmethod
    def open(cls, address: str, *, sim_bench=None, clock=None, capabilities: dict | None = None, timeout: float = 10.0, **_):
        return cls(open_transport(address, timeout=timeout), None, Capabilities("dmm", **capabilities) if capabilities else None)

    @property
    def capabilities(self) -> Capabilities:
        return self._caps

    def resistance_4w(self, range_ohm: float = 100.0, resolution_ohm: float = 1e-4) -> float:
        self.write(self.d["configure_fres"].format(range=f"{range_ohm:g}", resolution=f"{resolution_ohm:g}"))
        r = self.query_float(self.d["read?"])
        self.raise_errors()
        return r

    def state(self) -> dict:
        return {"identity": self.identity.to_dict(), "address": self.address, "dialect": self.d["name"],
                "capabilities": self.capabilities.to_dict()}


@register("dmm.scpi")
class ScpiDMM(DMM):
    pass


@register("dmm.sim")
class SimDMM(DMM):
    @classmethod
    def open(cls, address: str, *, sim_bench=None, clock=None, capabilities: dict | None = None, **_):
        from ..scpi import SimTransport
        from ..sim.dmm import DMMSim
        if sim_bench is None:
            raise ScpiError("dmm.sim needs a simulation bench")
        sim = DMMSim(sim_bench)
        inst = cls(SimTransport(sim, "SIM::DMM"))
        inst.sim = sim
        return inst
