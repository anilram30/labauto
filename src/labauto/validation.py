"""
Measurement-aware validation: is this sweep trustworthy enough to archive
as engineering data?

Every check is a small physical argument about what a correct measurement
of *this* sample with *this* procedure must satisfy; each returns pass /
warn / fail with the number it looked at and the bound it used, so the
archived record shows not only that the data was accepted but why.

    grid          the instrument returned the procedure's frequency grid
    connection    something is connected: max_f |S21| above the floor
    passivity     max singular value of S <= 1 + eps (energy conservation)
    reciprocity   |S_ij| = |S_ji| within tolerance where the signal is above the floor
    trace_noise   high-pass residual of |S21|_dB (second differences) below the bound
    length        electrical length from the S21 group delay matches the registry:
                      L_el = c0 * NVP * tau_g   vs   L_registry   (catches a mislabelled sample)
    il_rdc        low-frequency insertion loss consistent with the DC loop resistance:
                      IL_dc = 8.686 * R_loop / (2 Z_d)   [dB],   ratio IL(f_min)/IL_dc in a band
                  (the ratio scales as rho^-1/2 - skin effect over DC - so a temperature-sweep
                  procedure needs a wider band than a room-temperature one)
    il_envelope   insertion loss per metre at 100 MHz inside the plausible band for a data pair
    far_end_termination
                  (crosstalk files) the far ends really are terminated: low-frequency single-ended
                  return loss above ~8 dB, because an open far end reflects e^{-2 alpha L} ~ 1 there

The trust decision maps the check statuses to  trusted | flagged | rejected
using the procedure's ``repeat_on`` / ``quarantine_on`` lists.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from cablecheck.mixedmode import PortMap, to_mixed_mode
from cablecheck.network import Network

__all__ = ["CheckResult", "TrustDecision", "validate_network", "decide_trust"]

C0 = 299_792_458.0


@dataclass
class CheckResult:
    name: str
    status: str            # pass | warn | fail | skip
    value: float | None
    bound: str
    message: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "value": self.value, "bound": self.bound, "message": self.message}


@dataclass
class TrustDecision:
    level: str                       # trusted | flagged | rejected
    checks: list[CheckResult]
    repeat: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"level": self.level, "repeat": self.repeat, "reasons": self.reasons, "checks": [c.to_dict() for c in self.checks]}


def _status(v, lo, hi=None, warn_frac=0.0):
    """pass/warn/fail for a value against bounds; warn inside a fraction of the bound."""
    if hi is None:
        return "fail" if v > lo else "pass"
    return "fail" if (v < lo or v > hi) else "pass"


def validate_network(net: Network, port_map: str, sweep, entry, checks: dict, aux: dict | None = None,
                     want_quantities: list[str] | None = None) -> list[CheckResult]:
    out: list[CheckResult] = []
    aux = aux or {}
    f = net.f
    # 1. grid
    grid = sweep.frequency_grid()
    ok = f.size == grid.size and np.allclose(f, grid, rtol=1e-6, atol=1.0)
    out.append(CheckResult("grid", "pass" if ok else "fail", float(f.size), f"{grid.size} points {grid[0]:.4g}-{grid[-1]:.4g} Hz",
                           "" if ok else "instrument grid differs from the procedure"))
    # 2. passivity
    pv = float(net.passivity_violation().max())
    out.append(CheckResult("passivity", "pass" if pv <= checks["passivity_max"] else ("warn" if pv <= 2 * checks["passivity_max"] else "fail"),
                           pv, f"<= {checks['passivity_max']}", "max singular value minus one"))
    # 3. reciprocity, where |S| > -50 dB
    err = 0.0
    n = net.nports
    for i in range(n):
        for j in range(i + 1, n):
            a, b = np.abs(net.s[:, i, j]), np.abs(net.s[:, j, i])
            m = (a > 10 ** (-50 / 20)) & (b > 10 ** (-50 / 20))
            if m.any():
                err = max(err, float(np.max(np.abs(20 * np.log10(a[m] / b[m])))))
    out.append(CheckResult("reciprocity", "pass" if err <= checks["reciprocity_max_db"] else ("warn" if err <= 2 * checks["reciprocity_max_db"] else "fail"),
                           err, f"<= {checks['reciprocity_max_db']} dB", "max |S_ij/S_ji| in dB above -50 dB"))
    pm = PortMap.parse(port_map)
    pairs = sorted({a.pair for a in pm.assignments if a.pair})
    thru_pairs = [p for p in pairs if (p, "near") in pm.groups() and (p, "far") in pm.groups()]
    if thru_pairs:
        p = thru_pairs[0]
        mm = to_mixed_mode(net, pm)
        sdd21 = mm.param(("d", p, "far"), ("d", p, "near"))
        il = -20 * np.log10(np.abs(sdd21) + 1e-30)
        # 4. connection
        peak = float(np.max(-il))
        out.append(CheckResult("connection", "pass" if peak >= checks["connection_min_db"] else "fail", peak,
                               f">= {checks['connection_min_db']} dB", "max differential transmission; below the bound = open/unconnected"))
        # 5. trace noise from second differences of IL (dB): sigma = std(d2)/sqrt(6)
        d2 = np.diff(il, 2)
        noise = float(np.std(d2) / np.sqrt(6))
        out.append(CheckResult("trace_noise", "pass" if noise <= checks["trace_noise_max_db"] else ("warn" if noise <= 2 * checks["trace_noise_max_db"] else "fail"),
                               noise, f"<= {checks['trace_noise_max_db']} dB", "std of second differences of IL / sqrt(6)"))
        # 6. length from group delay (middle 60 % of the band)
        ph = np.unwrap(np.angle(sdd21))
        k0, k1 = int(0.2 * f.size), int(0.8 * f.size)
        slope = np.polyfit(f[k0:k1], ph[k0:k1], 1)[0]
        tau = -slope / (2 * np.pi)
        L_el = C0 * float(checks.get("nvp", 0.68)) * tau
        L_reg = float(getattr(entry, "length_m", 0) or 0)
        if L_reg > 0:
            dev = 100 * (L_el - L_reg) / L_reg
            tol = float(checks["length_tol_pct"])
            st = "pass" if abs(dev) <= tol else ("warn" if abs(dev) <= 2 * tol else "fail")
            out.append(CheckResult("length", st, L_el, f"{L_reg} m +- {tol} %",
                                   f"electrical length {L_el:.2f} m from group delay {tau * 1e9:.1f} ns at NVP {checks.get('nvp', 0.68)}"))
        # 7. IL vs R_dc
        r_loop = aux.get("loop_resistance")
        if r_loop is not None and np.isfinite(r_loop) and r_loop < 1e6:
            zd = 100.0
            il_dc = 8.686 * r_loop / (2 * zd)
            lo, hi = checks["il_rdc_ratio"]
            ratio = float(il[0] / il_dc) if il_dc > 0 else np.inf
            out.append(CheckResult("il_rdc", "pass" if lo <= ratio <= hi else "fail", ratio, f"{lo}..{hi}",
                                   f"IL({f[0] / 1e6:.2g} MHz) = {il[0]:.3f} dB vs DC-resistance floor {il_dc:.3f} dB (R_loop {r_loop:.3f} ohm)"))
        # 8. envelope at 100 MHz per metre
        if L_reg > 0 and f[0] <= 100e6 <= f[-1]:
            il100 = float(np.interp(100e6, f, il)) / L_reg
            lo, hi = checks["il_per_m_100mhz_db"]
            out.append(CheckResult("il_envelope", "pass" if lo <= il100 <= hi else "fail", il100, f"{lo}..{hi} dB/m",
                                   "insertion loss per metre at 100 MHz"))
    else:
        # crosstalk-only file (near ends on the analyser, far ends terminated by the operator):
        # an open or missing far-end termination reflects almost everything at low frequency, where the
        # cable loss is negligible: |S_ii| -> e^{-2 alpha L} ~ 1, i.e. RL of a fraction of a dB.
        rl = np.stack([-20 * np.log10(np.abs(net.s[:, k, k]) + 1e-30) for k in range(n)], axis=1)
        k_lo = max(3, int(0.05 * f.size))
        rl_lo = float(np.min(rl[:k_lo]))
        out.append(CheckResult("far_end_termination", "pass" if rl_lo >= checks.get("termination_rl_min_db", 8.0) else "fail", rl_lo,
                               f">= {checks.get('termination_rl_min_db', 8.0)} dB",
                               "minimum single-ended return loss over the lowest 5 % of the band; an open far end reflects ~everything there"))
        out.append(CheckResult("connection", "pass" if np.median(rl) > 3 else "fail", float(np.median(rl)), "> 3 dB",
                               "median single-ended return loss; ~0 dB means nothing connected"))
    return out


def decide_trust(checks: list[CheckResult], policy: dict, attempt: int) -> TrustDecision:
    statuses = {c.status for c in checks}
    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]
    repeat_on = set(policy.get("repeat_on", ["fail"]))
    quarantine_on = set(policy.get("quarantine_on", ["fail"]))
    max_rep = int(policy.get("max_repeats", 2))
    reasons = [f"{c.name}: {c.message or c.bound} (value {c.value})" for c in fails + warns]
    if "fail" in statuses and "fail" in quarantine_on:
        repeat = attempt < max_rep and "fail" in repeat_on
        return TrustDecision("rejected", checks, repeat, reasons)
    if "warn" in statuses:
        repeat = attempt < max_rep and "warn" in repeat_on
        return TrustDecision("flagged", checks, repeat, reasons)
    return TrustDecision("trusted", checks, False, [])
