"""
SCPI-speaking network analyser simulator.

It parses the same command strings the driver sends to a real analyser,
holds instrument state (sweep, IF bandwidth, power, averaging, active
calibration set, error queue) and, on trigger, returns what an imperfect
instrument would measure of whatever the bench has connected.

Imperfection model
------------------
*Uncorrected* (no active cal set): raw error adapter per port,
directivity -20 dB, source match -15 dB, tracking ripple of +-0.5 dB -
the data is obviously wrong, as on a real analyser.

*Corrected*: residual errors that grow with what the calibration cannot
know:  the residual tracking error (dB, peak) is

    e_T = e_0 + k_T |T - T_cal| + k_age (t - t_cal)/day

with e_0 = 0.02 dB, k_T = 0.012 dB/K, k_age = 0.004 dB/day, applied as a
slowly rippling multiplicative error on every transmission term (with a
per-calibration random phase and magnitude factor), a systematic transmission
bias of 0.03 dB per unit of the calibration 'quality' factor above 1 (a worn
test cable losing more than at calibration time), plus a
residual directivity of -52 dB (worsening at the same rate) added to every
reflection term.  The error terms are mostly common to the two ports of a
pair (same calibration, same test cables) with a small port-specific part,
which is what bounds the measurable mode conversion (LCL) at about
-(directivity) + 6 dB.  Cable connectors on the port add a repeatability term of
0.01 dB.  These numbers are typical of a 4-port SOLT/ECal calibration on a
mid-range analyser and are exactly what the calibration policy in
:mod:`labauto.calibration` is designed to catch.

*Trace noise*: complex Gaussian with

    sigma_dB = -100 dBc + 10 log10(IFBW / 1 kHz) - 10 log10(N_avg) - P_dBm

(noise floor at 1 kHz IFBW and 0 dBm), so the procedure's IFBW/averaging
choice has visible consequences in the data and the trust checks.

*Sweep time*: points * (1/IFBW + 25 us) * N_ports * N_avg, spent on the
clock, so the batch planner's time estimates can be checked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..clock import Clock, WallClock
from ..drivers import Capabilities
from ..scpi import normalise, split_commands

__all__ = ["VNASim", "CalSetSim"]

DAY = 86400.0


@dataclass
class CalSetSim:
    name: str
    t_cal: float                # unix time of the calibration
    temperature_c: float        # ambient when calibrated
    ports: list[int] = field(default_factory=lambda: [1, 2, 3, 4])
    kind: str = "SOLT"
    quality: float = 1.0        # multiplies the residuals (2 = a sloppy cal)
    phase: float = 0.0          # per-calibration random phase of the residual ripple
    scale: float = 1.0          # per-calibration random magnitude factor (0.7 .. 1.3)


class VNASim:
    def __init__(self, bench, clock: Clock | None = None, nports: int = 4, fmin: float = 9e3, fmax: float = 8.5e9,
                 seed: int = 11):
        self.bench = bench
        self.clock = clock or WallClock()
        self.nports = nports
        self.fmin, self.fmax = fmin, fmax
        self.rng = np.random.default_rng(seed)
        self.idn = "labauto-sim,VNA4-8500,SIM0001,A.01.20"
        self.errors: list[tuple[int, str]] = []
        self.calsets: dict[str, CalSetSim] = {}
        self.sweeps = 0
        self.reset()
        # per-port residual phase offsets so errors are not identical port to port
        self._port_phase = self.rng.uniform(0, 2 * np.pi, size=nports)

    def capabilities(self) -> Capabilities:
        return Capabilities("vna", ports=self.nports, fmin_hz=self.fmin, fmax_hz=self.fmax, features={"s-parameters", "calsets"})

    def reset(self):
        self.st = {"start": 10e6, "stop": 3e9, "points": 201, "type": "LIN", "ifbw": 1e3, "power": 0.0,
                   "avg_on": False, "avg_count": 1, "calset": "", "corr": False, "mode": "CONT",
                   "format": "ASC", "snp": "RI"}
        self._last = None

    # ---- calibration sets
    def add_calset(self, name: str, t_cal: float, temperature_c: float, ports=None, kind="SOLT", quality=1.0):
        # every calibration has its own residual signature (connector seating, cable position at cal time)
        r = np.random.default_rng((hash(name) ^ int(self.rng.integers(1 << 31))) & 0xFFFFFFFF)
        self.calsets[name] = CalSetSim(name, t_cal, temperature_c, list(ports or range(1, self.nports + 1)), kind, quality,
                                       float(r.uniform(0, 2 * np.pi)), float(r.uniform(0.7, 1.3)))

    # ---- SCPI
    def _err(self, code: int, msg: str):
        self.errors.append((code, msg))

    def handle(self, cmd: str) -> str | None:
        out = None
        for c in split_commands(cmd):
            out = self._one(c)
        return out

    def _one(self, c: str) -> str | None:
        head, _, arg = c.partition(" ")
        h = normalise(head)
        arg = arg.strip()
        st = self.st
        if h == "*IDN?":
            return self.idn
        if h == "*RST":
            self.reset(); return None
        if h == "*CLS":
            self.errors.clear(); return None
        if h == "*OPC?":
            return "1"
        if h == "SYST:ERR?":
            if self.errors:
                code, msg = self.errors.pop(0)
                return f'{code},"{msg}"'
            return '0,"No error"'
        if h == "SYST:CAP:HARD:PORT:COUN?":
            return str(self.nports)
        if h == "SYST:CAP:HARD:FREQ:MIN?":
            return f"{self.fmin:.6g}"
        if h == "SYST:CAP:HARD:FREQ:MAX?":
            return f"{self.fmax:.6g}"
        if h in ("SENS:FREQ:STAR", "SENS:FREQ:STOP"):
            v = float(arg)
            if not (self.fmin <= v <= self.fmax):
                self._err(-222, "Data out of range;" + c); return None
            st["start" if h.endswith("STAR") else "stop"] = v
            return None
        if h == "SENS:FREQ:STAR?":
            return f"{st['start']:.9g}"
        if h == "SENS:FREQ:STOP?":
            return f"{st['stop']:.9g}"
        if h == "SENS:SWE:POIN":
            v = int(float(arg))
            if not (2 <= v <= 100001):
                self._err(-222, "Data out of range;" + c); return None
            st["points"] = v; return None
        if h == "SENS:SWE:POIN?":
            return str(st["points"])
        if h == "SENS:SWE:TYPE":
            st["type"] = arg.upper()[:3]; return None
        if h == "SENS:SWE:TYPE?":
            return st["type"]
        if h == "SENS:BWID":
            v = float(arg)
            if not (1 <= v <= 1e6):
                self._err(-222, "Data out of range;" + c); return None
            st["ifbw"] = v; return None
        if h == "SENS:BWID?":
            return f"{st['ifbw']:.6g}"
        if h == "SOUR:POW":
            v = float(arg)
            if not (-30 <= v <= 10):
                self._err(-222, "Data out of range;" + c); return None
            st["power"] = v; return None
        if h == "SOUR:POW?":
            return f"{st['power']:.6g}"
        if h == "SENS:AVER:STAT":
            st["avg_on"] = arg.upper() in ("ON", "1"); return None
        if h == "SENS:AVER:STAT?":
            return "1" if st["avg_on"] else "0"
        if h == "SENS:AVER:COUN":
            st["avg_count"] = max(1, int(float(arg))); return None
        if h == "SENS:AVER:COUN?":
            return str(st["avg_count"])
        if h == "SENS:AVER:CLE":
            return None
        if h == "SENS:CORR:CSET:ACT":
            name = arg.split(",")[0].strip().strip('"')
            if name not in self.calsets:
                self._err(-224, f"Illegal parameter value;cal set '{name}' not found"); return None
            st["calset"], st["corr"] = name, True
            return None
        if h == "SENS:CORR:CSET:ACT?":
            return f'"{st["calset"]}"'
        if h == "SENS:CORR:CSET:CAT?":
            return ",".join(f'"{n}"' for n in self.calsets) or '""'
        if h == "SENS:CORR:STAT?":
            return "1" if st["corr"] else "0"
        if h == "SENS:CORR:STAT":
            st["corr"] = arg.upper() in ("ON", "1"); return None
        if h == "FORM:DATA":
            st["format"] = arg; return None
        if h == "MMEM:STOR:TRAC:FORM:SNP":
            st["snp"] = arg.upper(); return None
        if h == "SENS:SWE:MODE":
            st["mode"] = arg.upper()
            if st["mode"].startswith("SING"):
                self._sweep()
            return None
        if h == "SENS:SWE:TIME?":
            return f"{self._sweep_time():.6g}"
        if h == "CALC:DATA:SNP:PORT?":
            return self._snp(arg)
        self._err(-113, "Undefined header;" + c)
        return None

    # ---- physics
    def frequencies(self) -> np.ndarray:
        st = self.st
        if st["type"].startswith("LOG"):
            return np.geomspace(st["start"], st["stop"], st["points"])
        return np.linspace(st["start"], st["stop"], st["points"])

    def _sweep_time(self) -> float:
        st = self.st
        n_avg = st["avg_count"] if st["avg_on"] else 1
        return st["points"] * (1.0 / st["ifbw"] + 25e-6) * self.nports * n_avg

    def _sweep(self):
        self.clock.sleep(self._sweep_time())
        self.sweeps += 1
        self._last = self.clock.time()

    def noise_sigma_db(self) -> float:
        st = self.st
        n_avg = st["avg_count"] if st["avg_on"] else 1
        return -100.0 + 10 * np.log10(st["ifbw"] / 1e3) - 10 * np.log10(n_avg) - st["power"]

    def residuals(self) -> tuple[float, float]:
        """(tracking error in dB peak, directivity in dB) for the current correction state."""
        st = self.st
        if not st["corr"] or st["calset"] not in self.calsets:
            return 0.5, -20.0
        cs = self.calsets[st["calset"]]
        dT = abs(self.bench.ambient_c - cs.temperature_c)
        age_d = max(0.0, (self.clock.time() - cs.t_cal) / DAY)
        e_t = (0.02 + 0.012 * dT + 0.004 * age_d) * cs.quality * cs.scale
        d = -52.0 + 20 * np.log10(1 + 0.15 * dT + 0.05 * age_d) * cs.quality
        return e_t, d

    def transmission_bias_db(self) -> float:
        """A worn test cable or connector loses a little more than it did at calibration time: the
        correction cannot know, so transmission reads low by a systematic amount that grows with the
        'quality' factor (0.03 dB per unit above 1)."""
        st = self.st
        if not st["corr"] or st["calset"] not in self.calsets:
            return 0.0
        return 0.03 * max(self.calsets[st["calset"]].quality - 1.0, 0.0)

    def _cal_phase(self) -> float:
        cs = self.calsets.get(self.st["calset"]) if self.st["corr"] else None
        return cs.phase if cs is not None else 0.0

    def _snp(self, arg: str) -> str:
        ports = [int(p) for p in arg.strip().strip('"').split(",")]
        if any(p < 1 or p > self.nports for p in ports):
            self._err(-222, "Data out of range;port"); return "0"
        if self._last is None:
            self._sweep()
        f = self.frequencies()
        net = self.bench.true_network(f, ports)
        n = len(ports)
        if net is None:
            s = np.zeros((f.size, n, n), complex)
            for k in range(n):
                s[:, k, k] = 1.0
        else:
            s = net.s.copy()
        e_t, d_db = self.residuals()
        st = self.st
        # tracking (multiplicative, rippling with frequency) and directivity (additive) residuals
        for i in range(n):
            for j in range(n):
                # error terms are mostly common to the ports of a pair (same cal, same cables), with a small port-specific part
                ph = 0.25 * (self._port_phase[ports[i] - 1] + self._port_phase[ports[j] - 1]) + self._port_phase[0] + self._cal_phase()
                ripple = np.cos(2 * np.pi * f * 0.8e-9 + ph)
                if i == j:
                    s[:, i, i] += 10 ** (d_db / 20) * np.exp(1j * (2 * np.pi * f * 0.5e-9 + ph))
                    s[:, i, i] *= 10 ** (0.5 * e_t * ripple / 20)
                else:
                    s[:, i, j] *= 10 ** ((e_t * ripple - self.transmission_bias_db()) / 20) * np.exp(1j * 0.004 * e_t * ripple * 50)
        if not st["corr"]:
            # uncorrected: strong source-match ripple as well
            for i in range(n):
                for j in range(n):
                    if i != j:
                        s[:, i, j] *= (1 + 0.15 * np.cos(2 * np.pi * f * 2.3e-9 + i))
        sigma = 10 ** (self.noise_sigma_db() / 20) / np.sqrt(2)
        s += sigma * (self.rng.standard_normal(s.shape) + 1j * self.rng.standard_normal(s.shape))
        # connector repeatability: tiny per-sweep transmission scale
        s *= 10 ** (self.rng.normal(0, 0.005) / 20)
        parts = [f]
        for i in range(n):
            for j in range(n):
                parts.append(s[:, i, j].real)
                parts.append(s[:, i, j].imag)
        return ",".join(f"{v:.9g}" for v in np.concatenate(parts))
