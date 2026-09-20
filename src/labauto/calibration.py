"""
Calibration management: records, policy, verification.

A calibration is *known* by a record (who, when, which kit, which ports,
at what temperature, on which instrument) and *trusted* by a policy:

    age      t_now - t_cal            <= max_age_days
    thermal  |T_ambient - T_cal|      <= max_delta_t_k
    scope    procedure ports          subset of calibrated ports, type matches
    proof    a verification against a check standard, not older than
             verify_every_hours, that passed

Verification measures a certified two-port (a 20 dB attenuator here) on
each port pair and compares with its reference file:

    dA  = max_f | 20 log10 |S21_meas| - 20 log10 |S21_ref| |     <= tol_db
    dphi = max_f | arg S21_meas - arg S21_ref |  (unwrapped)     <= tol_deg
    RL  = min_f -20 log10 |S11_meas|                           >= rl_min_db

The instrument's own "cal is on" flag is necessary but not sufficient;
the engine refuses to measure on a calibration whose *proof* is missing
or stale, which is what turns "the VNA said corrected" into "the data are
traceable".
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from cablecheck.io import read_touchstone
from cablecheck.network import Network

from .clock import Clock

__all__ = ["CalRecord", "CalStore", "CalDecision", "evaluate_policy", "verify_calibration", "VerificationResult"]

log = logging.getLogger("labauto.cal")
DAY = 86400.0


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


@dataclass
class VerificationResult:
    time: str
    standard: str
    status: str                       # pass | fail
    port_pairs: list[list[int]]
    max_dev_db: float
    max_dev_deg: float
    min_rl_db: float
    tol_db: float
    tol_deg: float
    rl_min_db: float
    details: dict = field(default_factory=dict)
    file: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CalRecord:
    id: str
    instrument_serial: str
    instrument_calset: str            # name of the cal set on the instrument
    type: str                         # SOLT | ECAL | TRL
    ports: list[int]
    date: str                         # ISO 8601 UTC
    temperature_c: float
    operator: str
    kit_serial: str = ""
    kit_due: str = ""
    notes: str = ""
    verifications: list[VerificationResult] = field(default_factory=list)

    @property
    def t_cal(self) -> float:
        return _parse_iso(self.date)

    def last_verification(self) -> VerificationResult | None:
        return self.verifications[-1] if self.verifications else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verifications"] = [v.to_dict() for v in self.verifications]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CalRecord":
        v = [VerificationResult(**x) for x in d.get("verifications", [])]
        return cls(d["id"], d["instrument_serial"], d["instrument_calset"], d["type"], list(d["ports"]), d["date"],
                   float(d["temperature_c"]), d.get("operator", ""), d.get("kit_serial", ""), d.get("kit_due", ""),
                   d.get("notes", ""), v)


class CalStore:
    """A directory of ``<id>.json`` calibration records plus reference files for check standards."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def records(self) -> list[CalRecord]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            out.append(CalRecord.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        return out

    def get(self, cal_id: str) -> CalRecord:
        p = self.root / f"{cal_id}.json"
        if not p.exists():
            raise KeyError(f"no calibration record {cal_id!r} in {self.root}")
        return CalRecord.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save(self, rec: CalRecord) -> Path:
        p = self.root / f"{rec.id}.json"
        p.write_text(json.dumps(rec.to_dict(), indent=1), encoding="utf-8")
        return p

    def candidates(self, instrument_serial: str, cal_type: str, ports: list[int]) -> list[CalRecord]:
        """Records for this instrument whose type and ports cover the request, newest first."""
        out = [r for r in self.records()
               if r.instrument_serial == instrument_serial and r.type.upper() == cal_type.upper()
               and set(ports) <= set(r.ports)]
        return sorted(out, key=lambda r: r.t_cal, reverse=True)

    def reference_path(self, name: str) -> Path:
        return self.root / name

    def reference_network(self, name: str) -> Network:
        p = self.reference_path(name)
        if not p.exists():
            raise FileNotFoundError(f"check-standard reference {p} is missing")
        return read_touchstone(p)


@dataclass
class CalDecision:
    status: str                     # valid | needs-verification | invalid
    record: CalRecord | None
    reasons: list[str]
    age_days: float | None = None
    delta_t_k: float | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "id": self.record.id if self.record else None, "reasons": self.reasons,
                "age_days": self.age_days, "delta_t_k": self.delta_t_k}


def evaluate_policy(rec: CalRecord | None, policy: dict, now: float, ambient_c: float) -> CalDecision:
    if rec is None:
        return CalDecision("invalid", None, ["no calibration record covers this instrument/type/ports"])
    reasons = []
    age = (now - rec.t_cal) / DAY
    dT = abs(ambient_c - rec.temperature_c)
    if age < -0.01:
        reasons.append(f"calibration date {rec.date} is in the future")
    if age > float(policy.get("max_age_days", 7)):
        reasons.append(f"calibration is {age:.1f} days old (limit {policy.get('max_age_days', 7)})")
    if dT > float(policy.get("max_delta_t_k", 3.0)):
        reasons.append(f"ambient differs from calibration temperature by {dT:.1f} K (limit {policy.get('max_delta_t_k', 3.0)})")
    if rec.kit_due:
        try:
            if _parse_iso(rec.kit_due) < now:
                reasons.append(f"calibration kit {rec.kit_serial} was due for recalibration on {rec.kit_due}")
        except ValueError:
            reasons.append(f"kit due date {rec.kit_due!r} unreadable")
    if reasons:
        return CalDecision("invalid", rec, reasons, age, dT)
    if policy.get("verify", True):
        lv = rec.last_verification()
        every = float(policy.get("verify_every_hours", 8)) * 3600.0
        if lv is None:
            return CalDecision("needs-verification", rec, ["never verified against the check standard"], age, dT)
        if lv.status != "pass":
            return CalDecision("needs-verification", rec, [f"last verification at {lv.time} failed"], age, dT)
        if now - _parse_iso(lv.time) > every:
            return CalDecision("needs-verification", rec, [f"last verification at {lv.time} is older than {every / 3600:.0f} h"], age, dT)
    return CalDecision("valid", rec, [], age, dT)


def compare_to_reference(meas: Network, ref: Network) -> dict:
    """Deviation metrics of a measured 2-port from its reference (interpolated onto the measurement grid)."""
    r = ref.interpolate(meas.f) if not np.array_equal(ref.f, meas.f) else ref
    a_m = 20 * np.log10(np.abs(meas.s[:, 1, 0]) + 1e-30)
    a_r = 20 * np.log10(np.abs(r.s[:, 1, 0]) + 1e-30)
    ph = np.degrees(np.angle(meas.s[:, 1, 0] * np.conj(r.s[:, 1, 0])))
    rl = -20 * np.log10(np.maximum(np.abs(meas.s[:, 0, 0]), np.abs(meas.s[:, 1, 1])) + 1e-30)
    return {"max_dev_db": float(np.max(np.abs(a_m - a_r))), "max_dev_deg": float(np.max(np.abs(ph))),
            "min_rl_db": float(np.min(rl)), "mean_dev_db": float(np.mean(a_m - a_r))}


def verify_calibration(vna, rec: CalRecord, store: CalStore, policy: dict, clock: Clock, connect, out_dir: Path | None = None) -> VerificationResult:
    """Measure the check standard on every port pair of the policy and judge it.

    ``connect(device_id, ports)`` is the prompt/bench hook that gets the standard on the ports.
    """
    ref = store.reference_network(policy.get("verify_reference", "check_att20.s2p"))
    std = policy.get("verify_standard", "CHECK-ATT20")
    pairs = [list(map(int, p)) for p in policy.get("verify_port_pairs", [[1, 2]])]
    tol_db, tol_deg, rl_min = float(policy.get("verify_tol_db", 0.1)), float(policy.get("verify_tol_deg", 3.0)), float(policy.get("verify_rl_min_db", 30))
    details, worst_db, worst_deg, worst_rl = {}, 0.0, 0.0, np.inf
    for pp in pairs:
        connect(std, pp)
        net = vna.measure(pp, name=f"verify_{pp[0]}{pp[1]}")
        m = compare_to_reference(net, ref)
        details[f"{pp[0]}-{pp[1]}"] = m
        worst_db, worst_deg, worst_rl = max(worst_db, m["max_dev_db"]), max(worst_deg, m["max_dev_deg"]), min(worst_rl, m["min_rl_db"])
        if out_dir is not None:
            from cablecheck.io.touchstone import write_touchstone
            out_dir.mkdir(parents=True, exist_ok=True)
            write_touchstone(net, out_dir / f"verify_{rec.id}_{pp[0]}{pp[1]}_{int(clock.time())}.s2p")
    ok = worst_db <= tol_db and worst_deg <= tol_deg and worst_rl >= rl_min
    vr = VerificationResult(_iso(clock.time()), std, "pass" if ok else "fail", pairs, worst_db, worst_deg,
                            float(worst_rl), tol_db, tol_deg, rl_min, details)
    rec.verifications.append(vr)
    store.save(rec)
    log.info("calibration %s verification %s: dA=%.3f dB, dphi=%.2f deg, RL=%.1f dB", rec.id, vr.status, worst_db, worst_deg, worst_rl)
    return vr
