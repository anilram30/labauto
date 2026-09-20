"""
Vector network analyser driver.

The driver speaks a *dialect*: a table of command templates.  The default
dialect is the Keysight PNA/ENA family style (``SENS:FREQ:STAR``,
``CALC:DATA:SNP:PORT?``); R&S ZNB and others differ in a handful of entries
which a lab config can override without touching code.  Every setting is
read back after it is written and the read-back is what goes into the
metadata - the record says what the instrument *was*, not what we asked for.

Returned S-parameters are :class:`cablecheck.network.Network` objects, so
the same code that reads Touchstone files consumes live measurements.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import numpy as np
from cablecheck.network import Network

from ..scpi import ScpiError, ScpiInstrument, open_transport
from . import Capabilities, register

__all__ = ["SweepSettings", "VNA", "KEYSIGHT_DIALECT", "RS_ZNB_DIALECT"]

log = logging.getLogger("labauto.vna")


@dataclass
class SweepSettings:
    start_hz: float
    stop_hz: float
    points: int
    ifbw_hz: float
    power_dbm: float
    averages: int = 1
    sweep_type: str = "LIN"

    def to_dict(self) -> dict:
        return asdict(self)

    def frequency_grid(self) -> np.ndarray:
        if self.sweep_type.upper().startswith("LOG"):
            return np.geomspace(self.start_hz, self.stop_hz, self.points)
        return np.linspace(self.start_hz, self.stop_hz, self.points)

    def sweep_time_estimate(self, nports: int) -> float:
        """Rough sweep time: points * (1/IFBW + settling) per source port, times averages."""
        return self.points * (1.0 / self.ifbw_hz + 25e-6) * nports * self.averages


KEYSIGHT_DIALECT = {
    "name": "keysight-pna",
    "freq_start": "SENS{ch}:FREQ:STAR {value}",
    "freq_start?": "SENS{ch}:FREQ:STAR?",
    "freq_stop": "SENS{ch}:FREQ:STOP {value}",
    "freq_stop?": "SENS{ch}:FREQ:STOP?",
    "points": "SENS{ch}:SWE:POIN {value}",
    "points?": "SENS{ch}:SWE:POIN?",
    "sweep_type": "SENS{ch}:SWE:TYPE {value}",
    "sweep_type?": "SENS{ch}:SWE:TYPE?",
    "ifbw": "SENS{ch}:BWID {value}",
    "ifbw?": "SENS{ch}:BWID?",
    "power": "SOUR{ch}:POW {value}",
    "power?": "SOUR{ch}:POW?",
    "avg_count": "SENS{ch}:AVER:COUN {value}",
    "avg_count?": "SENS{ch}:AVER:COUN?",
    "avg_state": "SENS{ch}:AVER:STAT {value}",
    "avg_state?": "SENS{ch}:AVER:STAT?",
    "avg_clear": "SENS{ch}:AVER:CLE",
    "calset_select": 'SENS{ch}:CORR:CSET:ACT "{value}",1',
    "calset_active?": "SENS{ch}:CORR:CSET:ACT? NAME",
    "calset_catalog?": "SENS{ch}:CORR:CSET:CAT? NAME",
    "corr_state?": "SENS{ch}:CORR:STAT?",
    "format_ascii": "FORM:DATA ASC,0",
    "snp_format": "MMEM:STOR:TRAC:FORM:SNP RI",
    "trigger_single": "SENS{ch}:SWE:MODE SING",
    "trigger_hold": "SENS{ch}:SWE:MODE HOLD",
    "snp_data?": 'CALC{ch}:DATA:SNP:PORT? "{ports}"',
    "snp_order": "row",          # S11,S12,...,S1n,S21,... ; "touchstone" = column-within-row Touchstone order
    "sweep_time?": "SENS{ch}:SWE:TIME?",
}

RS_ZNB_DIALECT = dict(KEYSIGHT_DIALECT, **{
    "name": "rs-znb",
    "calset_select": 'MMEM:LOAD:CORR {ch},"{value}"',
    "calset_active?": "MMEM:LOAD:CORR? {ch}",
    "calset_catalog?": "MMEM:CAT? 'C:\\Users\\Public\\Documents\\Rohde-Schwarz\\Vna\\Calibration\\Data'",
    "trigger_single": "INIT{ch}:CONT OFF;:INIT{ch}:IMM",
    "snp_data?": "CALC{ch}:DATA:SNP:PORT? {ports}",
})

DIALECTS = {"keysight-pna": KEYSIGHT_DIALECT, "rs-znb": RS_ZNB_DIALECT}


class VNA(ScpiInstrument):
    role = "vna"

    def __init__(self, transport, dialect: dict | str = "keysight-pna", channel: int = 1,
                 capabilities: Capabilities | None = None):
        super().__init__(transport)
        self.d = DIALECTS[dialect] if isinstance(dialect, str) else dialect
        self.ch = channel
        self._caps = capabilities

    # ---- construction
    @classmethod
    def open(cls, address: str, *, sim_bench=None, clock=None, dialect="keysight-pna", channel=1,
             capabilities: dict | None = None, timeout: float = 30.0, **_):
        caps = Capabilities(role="vna", **capabilities) if capabilities else None
        return cls(open_transport(address, timeout=timeout), dialect, channel, caps)

    def cmd(self, key: str, **kw) -> str:
        return self.d[key].format(ch=self.ch, **kw)

    # ---- capabilities
    @property
    def capabilities(self) -> Capabilities:
        if self._caps is None:
            # Ask the instrument what it can do; fall back to a conservative guess.
            try:
                nports = int(float(self.query("SYST:CAP:HARD:PORT:COUN?")))
            except (ScpiError, ValueError):
                nports = 2
            try:
                fmin = float(self.query("SYST:CAP:HARD:FREQ:MIN?"))
                fmax = float(self.query("SYST:CAP:HARD:FREQ:MAX?"))
            except (ScpiError, ValueError):
                fmin, fmax = 0.0, 0.0
            self._caps = Capabilities("vna", ports=nports, fmin_hz=fmin, fmax_hz=fmax, features={"s-parameters"})
        return self._caps

    # ---- sweep configuration
    def configure(self, s: SweepSettings) -> SweepSettings:
        """Write the sweep settings and return what the instrument reports back."""
        self.write(self.cmd("trigger_hold"))
        self.write(self.cmd("sweep_type", value=s.sweep_type))
        self.write(self.cmd("freq_start", value=f"{s.start_hz:.6g}"))
        self.write(self.cmd("freq_stop", value=f"{s.stop_hz:.6g}"))
        self.write(self.cmd("points", value=int(s.points)))
        self.write(self.cmd("ifbw", value=f"{s.ifbw_hz:.6g}"))
        self.write(self.cmd("power", value=f"{s.power_dbm:.3g}"))
        self.write(self.cmd("avg_state", value="ON" if s.averages > 1 else "OFF"))
        self.write(self.cmd("avg_count", value=max(1, int(s.averages))))
        self.write(self.cmd("format_ascii"))
        self.write(self.cmd("snp_format"))
        self.raise_errors()
        return self.read_settings()

    def read_settings(self) -> SweepSettings:
        return SweepSettings(
            start_hz=self.query_float(self.cmd("freq_start?")),
            stop_hz=self.query_float(self.cmd("freq_stop?")),
            points=int(float(self.query(self.cmd("points?")))),
            ifbw_hz=self.query_float(self.cmd("ifbw?")),
            power_dbm=self.query_float(self.cmd("power?")),
            averages=int(float(self.query(self.cmd("avg_count?")))) if self.query(self.cmd("avg_state?")).strip() in ("1", "ON") else 1,
            sweep_type=self.query(self.cmd("sweep_type?")).strip(),
        )

    # ---- calibration sets
    def calsets(self) -> list[str]:
        r = self.query(self.cmd("calset_catalog?")).strip()
        return [x.strip().strip('"') for x in r.split(",") if x.strip().strip('"')]

    def select_calset(self, name: str) -> None:
        self.write(self.cmd("calset_select", value=name))
        self.raise_errors()
        active = self.active_calset()
        if active != name:
            raise ScpiError(f"calibration set {name!r} did not become active (instrument reports {active!r})")

    def active_calset(self) -> str:
        return self.query(self.cmd("calset_active?")).strip().strip('"')

    def correction_on(self) -> bool:
        return self.query(self.cmd("corr_state?")).strip() in ("1", "ON")

    # ---- measurement
    def measure(self, ports: list[int], z0: float = 50.0, name: str = "") -> Network:
        """Single triggered sweep (with averaging if enabled) -> N-port Network on the given ports."""
        n = len(ports)
        s = self.read_settings()
        self.write(self.cmd("avg_clear"))
        self.write(self.cmd("trigger_single"))
        if not self.opc():
            raise ScpiError("*OPC? did not return 1 after the sweep")
        self.raise_errors()
        raw = self.query(self.cmd("snp_data?", ports=",".join(str(p) for p in ports)))
        data = np.array([float(x) for x in raw.split(",") if x.strip()], float)
        npts = s.points
        expected = npts * (1 + 2 * n * n)
        if data.size != expected:
            raise ScpiError(f"SNP data has {data.size} values, expected {expected} for {n} ports x {npts} points")
        f = data[:npts]
        rest = data[npts:].reshape(n * n, 2, npts)
        smat = np.empty((npts, n, n), complex)
        k = 0
        if self.d.get("snp_order", "row") == "row":
            for i in range(n):
                for j in range(n):
                    smat[:, i, j] = rest[k, 0] + 1j * rest[k, 1]
                    k += 1
        else:  # Touchstone order for n>2 is row-major too; for 2-port it is S11 S21 S12 S22
            order = [(0, 0), (1, 0), (0, 1), (1, 1)] if n == 2 else [(i, j) for i in range(n) for j in range(n)]
            for (i, j) in order:
                smat[:, i, j] = rest[k, 0] + 1j * rest[k, 1]
                k += 1
        return Network(f, smat, np.full(n, z0), name=name or f"vna_{'-'.join(map(str, ports))}",
                       comments=[f"{self.identity.raw}", f"ports {ports}", f"IFBW {s.ifbw_hz:g} Hz, {s.averages} avg, {s.power_dbm:g} dBm"])

    def state(self) -> dict:
        """Everything about the instrument that belongs in the metadata."""
        return {"identity": self.identity.to_dict(), "address": self.address, "dialect": self.d["name"],
                "channel": self.ch, "sweep": self.read_settings().to_dict(),
                "calset_active": self.active_calset(), "correction_on": self.correction_on(),
                "capabilities": self.capabilities.to_dict()}


@register("vna.scpi")
class ScpiVNA(VNA):
    pass


@register("vna.sim")
class SimVNA(VNA):
    @classmethod
    def open(cls, address: str, *, sim_bench=None, clock=None, dialect="keysight-pna", channel=1,
             capabilities: dict | None = None, serial: str | None = None, seed: int = 11, **_):
        from ..scpi import SimTransport
        from ..sim.vna import VNASim
        if sim_bench is None:
            raise ScpiError("vna.sim needs a simulation bench")
        sim = VNASim(sim_bench, clock, seed=seed)
        if serial:
            sim.idn = f"labauto-sim,VNA4-8500,{serial},A.01.20"
        inst = cls(SimTransport(sim, "SIM::VNA"), dialect, channel, Capabilities("vna", **capabilities) if capabilities else sim.capabilities())
        inst.sim = sim
        return inst
