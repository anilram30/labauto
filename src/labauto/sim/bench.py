"""
The simulated bench: what is connected to the instruments, and its physics.

The bench does not know about SCPI.  It answers two questions: *what is on
port N right now* and *what would a perfect instrument see*.  The
instrument simulators add their own imperfections (calibration residuals,
noise, sweep time).

Devices
-------
* a **cable sample**, built from a registry entry: a :class:`cablecheck.synth.PairSpec`
  per pair, derived from the registry's ``design`` columns and a
  deterministic seed from the sample id, with temperature dependence:
      R'(T) = R'_20 (1 + alpha_rho (T - 20)),  tan d(T) = tan d_20 (1 + beta_d (T - 20)),
      C'(T) = C'_20 (1 + gamma_c (T - 20))  (so Z falls slightly when hot)
  Profiles (``sim_profile``): good | lossy | defect | marginal | mislabelled.
* ``CHECK-ATT20``: the calibration verification standard, a 20 dB attenuator
  pair with a certified S-matrix (small slope, -45 dB match).
* ``2XTHRU``: two fixture halves back to back, for fixture characterisation.
* ``OPEN``: nothing connected.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
from cablecheck.network import Network
from cablecheck.synth import (
    FixtureSpec,
    PairSpec,
    embed,
    make_fixture,
    make_pair,
    make_two_pairs,
)

from ..barcode import RegistryEntry

__all__ = ["SimBench", "CableModel", "CHECK_STANDARD_ID"]

CHECK_STANDARD_ID = "CHECK-ATT20"
ALPHA_RHO = 0.00393      # 1/K, copper
BETA_D = 0.004           # 1/K, dielectric loss tangent (PE-like)
GAMMA_C = 1.2e-4         # 1/K, capacitance


def _seed(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


@dataclass
class CableModel:
    sample_id: str
    length_m: float
    pairs: int = 1
    z_diff: float = 100.0
    r_dc: float = 0.09
    tan_delta: float = 0.0015
    nvp: float = 0.68
    profile: str = "good"
    seed: int = 0
    ripple_amp: float = 0.0
    defect_pos_m: float | None = None
    defect_amp: float = 0.0

    @classmethod
    def from_entry(cls, e: RegistryEntry) -> "CableModel":
        x = e.extra
        seed = _seed(e.sample_id)
        rng = np.random.default_rng(seed)
        profile = x.get("sim_profile", "good") or "good"
        z = float(x.get("sim_z_diff", 100.0) or 100.0) + rng.normal(0, 1.0)
        r_dc = float(x.get("sim_r_dc", 0.09) or 0.09) * (1 + rng.normal(0, 0.02))
        td = float(x.get("sim_tan_delta", 0.0015) or 0.0015) * (1 + rng.normal(0, 0.05))
        m = cls(e.sample_id, e.length_m, int(x.get("sim_pairs", 1) or 1), z, r_dc, td,
                float(x.get("sim_nvp", 0.68) or 0.68), profile, seed)
        if profile == "lossy":                # badly foamed / wrong alloy: fails the IL limit at the top of the band
            m.tan_delta *= 7.0
            m.r_dc *= 1.8
        elif profile == "defect":             # a local capacitance bump 40 cm long, 25 % along: impedance dips to ~79 ohm
            m.defect_pos_m, m.defect_amp = 0.25 * e.length_m, 0.6
        elif profile == "marginal":
            m.tan_delta *= 1.45
        elif profile == "mislabelled":       # a different, shorter cable than the registry says
            m.length_m = 0.6 * e.length_m
            m.z_diff = z - 8.0
        m.ripple_amp = 0.004 if profile in ("good", "marginal") else 0.008
        return m

    def pair_spec(self, temperature_c: float, pair: int = 0) -> PairSpec:
        dT = temperature_c - 20.0
        c_scale = 1 + GAMMA_C * dT
        return PairSpec(length_m=self.length_m, z_diff=self.z_diff / np.sqrt(c_scale),
                        z_comm=0.35 * self.z_diff / np.sqrt(c_scale),
                        nvp=self.nvp / np.sqrt(c_scale), nvp_even=(self.nvp - 0.02) / np.sqrt(c_scale),
                        r_dc_ohm_per_m=self.r_dc * (1 + ALPHA_RHO * dT),
                        tan_delta=self.tan_delta * (1 + BETA_D * dT),
                        ripple_amp=self.ripple_amp, ripple_period_m=0.31 + 0.02 * pair,
                        defect_pos_m=self.defect_pos_m, defect_amp=self.defect_amp, defect_width_m=0.4,
                        roughness=0.003, n_segments=48, seed=self.seed + pair)

    def network(self, f: np.ndarray, temperature_c: float) -> Network:
        if self.pairs == 1:
            return make_pair(self.pair_spec(temperature_c, 0), f, name=self.sample_id)
        # inter-pair coupling of a screened/quaded automotive pair: ~1 % (NEXT ~ 45 dB at 100 MHz over 10 m)
        return make_two_pairs(self.pair_spec(temperature_c, 0), self.pair_spec(temperature_c, 1), f,
                              coupling_l=0.008, coupling_c=0.006, name=self.sample_id)

    def loop_resistance(self, temperature_c: float) -> float:
        """DC loop resistance of one pair in ohm (both conductors)."""
        return 2 * self.r_dc * self.length_m * (1 + ALPHA_RHO * (temperature_c - 20.0))

    @property
    def nports(self) -> int:
        return 4 * self.pairs


def check_standard_network(f: np.ndarray, z0: float = 50.0) -> Network:
    """Certified 2-port 20 dB attenuator (the same object the cal-verification reference file holds)."""
    f = np.asarray(f, float)
    a_db = 20.0 + 0.02 * np.sqrt(f / 1e9)        # tiny frequency slope
    phase = -2 * np.pi * f * 60e-12               # 60 ps electrical length
    s21 = 10 ** (-a_db / 20) * np.exp(1j * phase)
    s11 = 10 ** (-45 / 20) * np.exp(1j * (-2 * np.pi * f * 30e-12 + 0.4))
    s = np.zeros((f.size, 2, 2), complex)
    s[:, 0, 0] = s11
    s[:, 1, 1] = s11 * np.exp(1j * 0.9)
    s[:, 0, 1] = s[:, 1, 0] = s21
    return Network(f, s, np.full(2, z0), name="CHECK-ATT20", comments=["certified 20 dB attenuator"])


class SimBench:
    """State of the simulated laboratory bench."""

    def __init__(self, registry=None, ambient_c: float = 23.0, fixture: bool = False, seed: int = 0,
                 unused_ports: str = "terminated"):
        self.registry = registry
        self.unused_ports = unused_ports     # what the operator does with device ports not on the analyser
        self.ambient_c = ambient_c
        self.humidity_pct = 45.0
        self.connected: str = "OPEN"
        self.in_chamber: bool = True         # the connected sample sits in the chamber when one exists
        self.chamber = None                  # ChamberSim sets itself here
        self.fixture = FixtureSpec() if fixture else None
        self.models: dict[str, CableModel] = {}
        self.rng = np.random.default_rng(seed)
        self.events: list[str] = []

    # ---- what is connected
    def connect(self, device: str, hookup: str | None = None) -> None:
        """``hookup`` is the port map of the measurement ("A+near,A-near,B+near,B-near"): which
        device port sits on each analyser port.  Without it, device ports 1..n go to analyser ports 1..n."""
        self.connected = device
        self.hookup = hookup
        self.events.append(f"connect {device} {hookup or ''}".rstrip())

    @staticmethod
    def device_ports(hookup: str) -> list[int]:
        """Port-map tokens -> device port numbers (pair A: 1-4, pair B: 5-8; near +,- then far +,-)."""
        out = []
        for tok in hookup.split(","):
            tok = tok.strip()
            i = max(tok.rfind("+"), tok.rfind("-"))
            pair, pol, end = tok[:i], tok[i], tok[i + 1:]
            out.append(4 * (ord(pair.upper()[-1]) - ord("A")) + (2 if end == "far" else 0) + (1 if pol == "-" else 0) + 1)
        return out

    def model_for(self, sample_id: str) -> CableModel:
        if sample_id not in self.models:
            e = None
            if self.registry is not None:
                e = self.registry.by_id.get(sample_id) or self.registry.entries.get(sample_id.upper())
            if e is None:
                raise KeyError(f"no registry entry for {sample_id}")
            self.models[sample_id] = CableModel.from_entry(e)
        return self.models[sample_id]

    def dut_temperature(self) -> float:
        if self.chamber is not None and self.in_chamber:
            return self.chamber.dut_c
        return self.ambient_c

    # ---- perfect-instrument view
    def true_network(self, f: np.ndarray, ports: list[int]) -> Network | None:
        """S-matrix on the given VNA ports of whatever is connected, or None for OPEN."""
        dev = self.connected
        if dev == "OPEN":
            return None
        if dev == CHECK_STANDARD_ID:
            net = check_standard_network(f)
            # the attenuator is connected between the first two requested ports; others open
            return _place(net, ports, [ports[0], ports[1]] if len(ports) >= 2 else ports, f)
        if dev == "2XTHRU":
            if self.fixture is None:
                raise RuntimeError("2XTHRU requested but the bench has no fixture")
            fx = make_fixture(self.fixture, f)
            thru = embed(_ideal_thru(f), fx, fx)
            return _place(thru, ports, ports[:4], f)
        model = self.model_for(dev)
        net = model.network(f, self.dut_temperature())
        if self.fixture is not None:
            fx = make_fixture(self.fixture, f)
            if net.nports == 4:
                net = embed(net, fx, fx)
            else:
                a = embed(net.subnetwork([0, 1, 2, 3]), fx, fx)
                b = embed(net.subnetwork([4, 5, 6, 7]), fx, fx)
                s = net.s.copy()
                s[:, :4, :4] = a.s
                s[:, 4:, 4:] = b.s
                net = Network(net.f, s, net.z0, name=net.name)
        if getattr(self, "hookup", None):
            dp = self.device_ports(self.hookup)
            return _place_map(net, ports, dp, f, self.unused_ports == "open")
        dev_ports = list(range(1, net.nports + 1))
        return _place(net, ports, dev_ports[:len(ports)] if len(ports) <= net.nports else dev_ports, f)

    def loop_resistance(self) -> float | None:
        dev = self.connected
        if dev in ("OPEN", "2XTHRU"):
            return None
        if dev == CHECK_STANDARD_ID:
            return 61.1     # 20 dB pi attenuator series arm-ish
        return self.model_for(dev).loop_resistance(self.dut_temperature())


def _ideal_thru(f):
    s = np.zeros((f.size, 4, 4), complex)
    s[:, 0, 2] = s[:, 2, 0] = 1.0
    s[:, 1, 3] = s[:, 3, 1] = 1.0
    return Network(f, s, np.full(4, 50.0), name="thru")


def _place_map(net: Network, requested: list[int], device_ports: list[int], f, leave_open: bool = False) -> Network:
    """Analyser port requested[k] is wired to device port device_ports[k].  Device ports not listed are
    terminated in the reference impedance (a well-run hook-up) or, with ``leave_open``, left dangling -
    which is what a forgotten termination looks like and what the trace-noise check catches."""
    n = len(requested)
    s = np.zeros((f.size, n, n), complex)
    for k in range(n):
        s[:, k, k] = 1.0
    listed = [d - 1 for d in device_ports[:n] if 1 <= d <= net.nports]
    sub = net.subnetwork(listed) if listed else None
    if sub is not None:
        # ports of the device that are not connected are open-circuited: absorb them exactly
        others = [i for i in range(net.nports) if i not in listed]
        if others and leave_open:
            sub = _terminate_open(net, listed, others)
        for i in range(len(listed)):
            for j in range(len(listed)):
                s[:, i, j] = sub.s[:, i, j]
    return Network(f, s, np.full(n, 50.0), name=net.name)


def _terminate_open(net: Network, keep: list[int], drop: list[int]) -> Network:
    """Reduce an N-port by leaving the ``drop`` ports open (reflection +1): S' = S_kk + S_kd (I - S_dd)^-1 S_dk."""
    S = net.s
    kk = S[:, keep][:, :, keep]
    kd = S[:, keep][:, :, drop]
    dk = S[:, drop][:, :, keep]
    dd = S[:, drop][:, :, drop]
    eye = np.eye(len(drop))[None]
    red = kk + kd @ np.linalg.solve(eye - dd, dk)
    return Network(net.f, red, net.z0[keep], name=net.name)


def _place(net: Network, requested: list[int], on_ports: list[int], f) -> Network:
    """Embed a device that occupies ``on_ports`` (VNA numbering) into the requested port set; unused ports open."""
    n = len(requested)
    s = np.zeros((f.size, n, n), complex)
    for k in range(n):
        s[:, k, k] = 1.0        # open
    idx = {p: k for k, p in enumerate(requested)}
    m = min(net.nports, len(on_ports))
    for i in range(m):
        for j in range(m):
            if on_ports[i] in idx and on_ports[j] in idx:
                s[:, idx[on_ports[i]], idx[on_ports[j]]] = net.s[:, i, j]
    return Network(f, s, np.full(n, 50.0), name=net.name)
