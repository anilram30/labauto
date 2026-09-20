"""
The orchestration engine: a job is a state machine, not a script.

    CREATED -> IDENTIFIED -> INSTRUMENTS_READY -> CALIBRATED -> FIXTURE_READY
            -> ENVIRONMENT_READY -> MEASURED -> VALIDATED -> ARCHIVED -> ANALYSED
            -> RECORDED -> DONE
    with the terminal states  REJECTED (data archived in quarantine, not analysed,
    not entered into the engineering database), ABORTED (a gate refused before
    measuring: unknown sample, unmet capability, invalid calibration, environment
    out of range, operator abort) and ERROR (an exception; journal preserved).

Each transition is journalled with a timestamp and its evidence (the
calibration decision, the check results, the chamber log ...).  The journal
is archived with the data, so the record of *why* a measurement was
accepted survives as long as the measurement itself.

The engine knows nothing about cables or analysers: what to measure, what
must hold and what runs afterwards come from the procedure; how to measure
comes from the drivers; whether the data can be trusted comes from
:mod:`validation`.  Adding an instrument is a driver; adding a test is a
procedure file; changing what "trustworthy" means is a check.
"""
from __future__ import annotations

import json
import logging
import tomllib
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from cablecheck.io.touchstone import write_touchstone

from . import __version__
from .analysis import run_analysers
from .barcode import BarcodeError
from .calibration import evaluate_policy, verify_calibration
from .drivers.chamber import StabilityCriterion, TemperatureLog
from .lab import Lab
from .metadata import (
    MetadataError,
    check_mandatory,
    sha256_file,
    software_versions,
    write_sidecar,
)
from .procedure import Procedure, load_procedure
from .validation import decide_trust, validate_network

__all__ = ["Engine", "Job", "JobState", "Prompter", "AutoPrompter", "ConsolePrompter", "BatchSpec", "SweepSpec"]

log = logging.getLogger("labauto.engine")


class JobState:
    CREATED, IDENTIFIED, INSTRUMENTS_READY, CALIBRATED, FIXTURE_READY, ENVIRONMENT_READY = (
        "CREATED", "IDENTIFIED", "INSTRUMENTS_READY", "CALIBRATED", "FIXTURE_READY", "ENVIRONMENT_READY")
    MEASURED, VALIDATED, ARCHIVED, ANALYSED, RECORDED, DONE = "MEASURED", "VALIDATED", "ARCHIVED", "ANALYSED", "RECORDED", "DONE"
    REJECTED, ABORTED, ERROR, PLANNED = "REJECTED", "ABORTED", "ERROR", "PLANNED"
    TERMINAL = {"DONE", "REJECTED", "ABORTED", "ERROR", "PLANNED"}


class Abort(Exception):
    """A gate refused the job before any data was taken."""


# ---------------------------------------------------------------- prompting
class Prompter:
    """How the engine asks the operator to do something physical."""

    def connect(self, device: str, ports: list[int], message: str, hookup: str | None = None) -> bool:   # pragma: no cover
        raise NotImplementedError

    def confirm(self, message: str) -> bool:                                  # pragma: no cover - interface
        raise NotImplementedError

    def notify(self, message: str) -> None:
        log.info(message)


class AutoPrompter(Prompter):
    """Simulation: every request is granted and the bench is re-wired accordingly."""

    def __init__(self, bench, fail_on: set[str] | None = None):
        self.bench = bench
        self.fail_on = fail_on or set()
        self.requests: list[tuple[str, list[int]]] = []

    def connect(self, device: str, ports: list[int], message: str, hookup: str | None = None) -> bool:
        self.requests.append((device, list(ports)))
        if device in self.fail_on:
            return False
        if self.bench is not None:
            self.bench.connect(device, hookup)
        return True

    def confirm(self, message: str) -> bool:
        return True


class ConsolePrompter(Prompter):  # pragma: no cover - interactive
    def connect(self, device: str, ports: list[int], message: str, hookup: str | None = None) -> bool:
        print(f"\n>>> {message}\n    [{device} on ports {ports}{' as ' + hookup if hookup else ''}]  press Enter when done, or type 'abort': ", end="")
        return input().strip().lower() != "abort"

    def confirm(self, message: str) -> bool:
        print(f"\n>>> {message}  [Enter = yes, 'n' = no]: ", end="")
        return input().strip().lower() not in ("n", "no", "abort")


# ---------------------------------------------------------------- jobs
@dataclass
class Job:
    job_id: str
    scan: str
    procedure: Procedure
    operator: str
    site: str = ""
    batch: str = ""
    campaign: str = ""
    temperature_c: float | None = None          # sweep setpoint (None = ambient procedure)
    notes: str = ""
    state: str = JobState.CREATED
    journal: list[dict] = field(default_factory=list)
    entry: object = None
    barcode: object = None
    instrument_states: dict = field(default_factory=dict)
    calibration: dict = field(default_factory=dict)
    fixture: dict = field(default_factory=dict)
    environment: dict = field(default_factory=dict)
    aux: dict = field(default_factory=dict)
    files: list[dict] = field(default_factory=list)      # per measured file: name, path, network, checks, trust, attempt
    trust: str = ""
    analysis: dict = field(default_factory=dict)
    archive_dir: Path | None = None
    started: str = ""
    finished: str = ""
    error: str = ""
    reason: str = ""

    def summary(self) -> dict:
        cc = self.analysis.get("cablecheck", {})
        return {"job_id": self.job_id, "campaign": self.campaign, "batch": self.batch, "scan": self.scan,
                "sample": self.entry.to_dict() if self.entry is not None else None,
                "barcode": self.barcode.to_dict() if self.barcode is not None else None,
                "sample_id": self.entry.sample_id if self.entry is not None else None,
                "lot": self.entry.lot if self.entry is not None else None,
                "part_number": self.entry.part_number if self.entry is not None else None,
                "cable_type": self.procedure.cable_type,
                "procedure": self.procedure.to_dict(), "procedure_id": self.procedure.id,
                "procedure_version": self.procedure.version, "procedure_hash": self.procedure.hash,
                "operator": self.operator, "site": self.site, "started": self.started, "finished": self.finished,
                "state": self.state, "trust": self.trust, "reason": self.reason, "error": self.error,
                "verdict": cc.get("verdict"), "headline": cc.get("headline"), "headline_margin": cc.get("headline_margin"),
                "temperature_c": self.environment.get("sample_temperature_c"),
                "calibration_id": self.calibration.get("id"), "calibration": self.calibration,
                "archive_dir": str(self.archive_dir) if self.archive_dir else None,
                "attempts": max((f.get("attempt", 1) for f in self.files), default=0),
                "files": [{k: v for k, v in f.items() if k != "network"} for f in self.files],
                "aux": self.aux, "analysis": {k: {kk: vv for kk, vv in v.items() if kk != "results"} for k, v in self.analysis.items()},
                "environment": {k: v for k, v in self.environment.items() if k != "chamber_log"},
                "software": software_versions()}


@dataclass
class BatchSpec:
    id: str
    procedure: str
    operator: str
    site: str = ""
    campaign: str = ""
    samples: list[str] = field(default_factory=list)
    notes: str = ""
    path: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "BatchSpec":
        d = tomllib.loads(Path(path).read_text(encoding="utf-8"))["batch"]
        return cls(d["id"], d["procedure"], d["operator"], d.get("site", ""), d.get("campaign", ""), list(d.get("samples", [])),
                   d.get("notes", ""), str(path))


@dataclass
class SweepSpec:
    id: str
    procedure: str
    sample: str
    operator: str
    site: str = ""
    campaign: str = ""
    temperatures: list[float] | None = None
    notes: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "SweepSpec":
        d = tomllib.loads(Path(path).read_text(encoding="utf-8"))["sweep"]
        return cls(d["id"], d["procedure"], d["sample"], d["operator"], d.get("site", ""), d.get("campaign", ""),
                   d.get("temperatures"), d.get("notes", ""))


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, lab: Lab, prompter: Prompter | None = None, dry_run: bool = False):
        self.lab = lab
        self.clock = lab.clock
        self.prompter = prompter or (AutoPrompter(lab.bench) if lab.bench is not None else ConsolePrompter())
        self.dry_run = dry_run
        self._verified_this_session: dict[str, float] = {}

    # ---- journal helpers
    def _log(self, job: Job, event: str, **data):
        entry = {"t": self.clock.time(), "iso": self.clock.iso(), "state": job.state, "event": event, "data": data}
        job.journal.append(entry)
        log.info("[%s] %s %s", job.job_id, event, json.dumps(data, default=str)[:200] if data else "")

    def _set(self, job: Job, state: str, **data):
        job.state = state
        self._log(job, f"-> {state}", **data)

    def new_job(self, scan: str, procedure: Procedure | str, operator: str, site: str = "", batch: str = "",
                campaign: str = "", temperature_c: float | None = None, notes: str = "") -> Job:
        proc = procedure if isinstance(procedure, Procedure) else load_procedure(procedure, self.lab.cfg.procedures_dirs)
        stamp = datetime.fromtimestamp(self.clock.time(), tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        sid = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in scan)
        suffix = f"_T{temperature_c:+04.0f}C" if temperature_c is not None else ""
        job = Job(f"{sid}__{proc.id}{suffix}__{stamp}", scan, proc, operator, site, batch, campaign, temperature_c, notes)
        self._log(job, "created", labauto=__version__, procedure=proc.to_dict())
        return job

    # ---- the state machine
    def run_job(self, job: Job) -> Job:
        job.started = self.clock.iso()
        try:
            self.identify(job)
            self.resolve_instruments(job)
            self.check_calibration(job)
            self.check_fixture(job)
            self.check_environment(job)
            if self.dry_run:
                est = job.procedure.estimate_seconds(self.lab.instrument("vna").capabilities.ports)
                job.reason = f"dry run: all gates passed; estimated {est / 60:.1f} min of measurement"
                self._set(job, JobState.PLANNED, reason=job.reason, estimate_s=est)
                return job
            self.measure(job)
            self.validate(job)
            self.archive(job)
            if job.trust == "rejected":
                self._set(job, JobState.REJECTED, reason=job.reason)
            else:
                self.analyse(job)
                self.record(job)
                self._set(job, JobState.DONE)
            job.finished = self.clock.iso()
            self._seal(job)
        except Abort as e:
            job.reason = str(e)
            self._set(job, JobState.ABORTED, reason=str(e))
        except Exception as e:  # noqa: BLE001 - the journal must record it
            job.error = f"{type(e).__name__}: {e}"
            job.reason = job.error
            self._log(job, "exception", traceback=traceback.format_exc())
            self._set(job, JobState.ERROR, error=job.error)
            log.exception("job %s failed", job.job_id)
        finally:
            job.finished = self.clock.iso()
            if job.state in (JobState.REJECTED, JobState.ABORTED, JobState.ERROR, JobState.PLANNED):
                self._record_terminal(job)
        return job

    # ---- gates
    def identify(self, job: Job):
        try:
            bc, entry = self.lab.registry.resolve(job.scan)
        except BarcodeError as e:
            raise Abort(f"identification failed: {e}") from e
        job.barcode, job.entry = bc, entry
        if entry.cable_type != job.procedure.cable_type:
            raise Abort(f"registry says {entry.sample_id} is {entry.cable_type!r} but the procedure "
                        f"{job.procedure.id} tests {job.procedure.cable_type!r}")
        self._set(job, JobState.IDENTIFIED, barcode=bc.to_dict(), sample=entry.to_dict())

    def resolve_instruments(self, job: Job):
        unmet = []
        for role, req in job.procedure.requires.items():
            inst = self.lab.instrument(role)
            if inst is None:
                if req.optional:
                    self._log(job, "optional instrument absent", role=role)
                    continue
                raise Abort(f"procedure needs a {role!r} and the lab has none")
            problems = req.check(inst.capabilities)
            if problems:
                unmet.extend(problems)
                continue
            if role == "vna":
                sw = job.procedure.sweep
                caps = inst.capabilities
                if caps.fmax_hz and sw.stop_hz > caps.fmax_hz or caps.fmin_hz and sw.start_hz < caps.fmin_hz:
                    unmet.append(f"sweep {sw.start_hz:.3g}-{sw.stop_hz:.3g} Hz outside the analyser's range")
                if caps.ports < job.procedure.vna_ports_needed:
                    unmet.append(f"files use port {job.procedure.vna_ports_needed}, analyser has {caps.ports} ports")
            job.instrument_states[role] = inst.state() if hasattr(inst, "state") else {}
        if unmet:
            raise Abort("capability check failed: " + "; ".join(unmet))
        self._set(job, JobState.INSTRUMENTS_READY, roles=list(job.instrument_states))

    def check_calibration(self, job: Job):
        vna = self.lab.instrument("vna")
        pol = job.procedure.calibration
        ports = [int(p) for p in pol.get("ports", [1, 2, 3, 4])]
        serial = vna.identity.serial
        ambient = self._ambient()
        cands = self.lab.calstore.candidates(serial, pol.get("type", "SOLT"), ports)
        decision, rec = None, None
        for cand in cands:                      # newest first; take the first that the policy accepts
            d = evaluate_policy(cand, pol, self.clock.time(), ambient)
            if d.status != "invalid":
                decision, rec = d, cand
                break
            decision = decision or d
        if rec is None:
            d = decision.to_dict() if decision else {"status": "invalid", "reasons": ["no candidate"]}
            self.lab.db.add_calibration(job.job_id, self.clock.iso(), serial, d, None)
            raise Abort("no valid calibration: " + "; ".join(d["reasons"]))
        if rec.instrument_calset not in vna.calsets():
            raise Abort(f"calibration record {rec.id} refers to cal set {rec.instrument_calset!r} which is not on the analyser")
        vna.configure(job.procedure.sweep)
        vna.select_calset(rec.instrument_calset)
        verification = None
        if decision.status == "needs-verification":
            if self.dry_run:
                self._log(job, "dry run: verification would be required", reasons=decision.reasons)
            else:
                self.prompter.notify(f"calibration {rec.id} needs verification: {'; '.join(decision.reasons)}")
                vr = verify_calibration(vna, rec, self.lab.calstore, pol, self.clock, self._connect_standard(job),
                                        self.lab.archive.root / "verification")
                verification = vr.to_dict()
                decision = evaluate_policy(rec, pol, self.clock.time(), ambient)
                if decision.status != "valid":
                    self.lab.db.add_calibration(job.job_id, self.clock.iso(), serial, decision.to_dict(), verification)
                    raise Abort(f"calibration {rec.id} failed verification: dA={vr.max_dev_db:.3f} dB (tol {vr.tol_db}), "
                                f"dphi={vr.max_dev_deg:.2f} deg (tol {vr.tol_deg}), RL={vr.min_rl_db:.1f} dB (min {vr.rl_min_db})")
        elif rec.last_verification() is not None:
            verification = rec.last_verification().to_dict()
        job.calibration = {"id": rec.id, "date": rec.date, "type": rec.type, "ports": rec.ports, "temperature_c": rec.temperature_c,
                           "operator": rec.operator, "kit_serial": rec.kit_serial, "instrument_calset": rec.instrument_calset,
                           "decision": decision.to_dict(), "verification": verification or {"status": "not-required"},
                           "age_days": decision.age_days, "delta_t_k": decision.delta_t_k}
        self.lab.db.add_calibration(job.job_id, self.clock.iso(), serial, decision.to_dict(), verification)
        job.instrument_states["vna"] = vna.state()
        self._set(job, JobState.CALIBRATED, calibration=job.calibration["id"], decision=decision.status)

    def _connect_standard(self, job: Job):
        def connect(device: str, ports: list[int]):
            ok = self.prompter.connect(device, ports, f"Connect the check standard {device} between ports {ports[0]} and {ports[1]}")
            if not ok:
                raise Abort("operator aborted at calibration verification")
        return connect

    def check_fixture(self, job: Job):
        fx = dict(job.procedure.fixture or {"method": "none"})
        method = fx.get("method", "none")
        if method == "2xthru":
            thru = self.lab.cfg.calibration_dir / "fixtures" / fx.get("thru", "2xthru.s4p")
            if not thru.exists():
                if self.dry_run:
                    self._log(job, "dry run: fixture characterisation would be required", thru=str(thru))
                else:
                    ok = self.prompter.connect("2XTHRU", [1, 2, 3, 4], "Connect the two fixture halves back to back (2x-thru)")
                    if not ok:
                        raise Abort("operator aborted at fixture characterisation")
                    net = self.lab.instrument("vna").measure([1, 2, 3, 4], name="2xthru")
                    thru.parent.mkdir(parents=True, exist_ok=True)
                    write_touchstone(net, thru)
                    self._log(job, "fixture characterised", thru=str(thru))
            fx["thru"] = str(thru)
            fx["thru_sha256"] = sha256_file(thru) if thru.exists() else None
        elif method in ("files",):
            for k in ("left", "right"):
                p = self.lab.cfg.calibration_dir / "fixtures" / fx[k]
                if not p.exists():
                    raise Abort(f"fixture file {p} missing")
                fx[k], fx[k + "_sha256"] = str(p), sha256_file(p)
        elif method == "port-extension":
            if not fx.get("delays_ps"):
                raise Abort("port-extension fixture without delays_ps")
        elif method != "none":
            raise Abort(f"unknown fixture method {method!r}")
        job.fixture = fx
        self._set(job, JobState.FIXTURE_READY, method=method)

    def _ambient(self) -> float:
        if self.lab.bench is not None:
            return float(self.lab.bench.ambient_c)
        ch = self.lab.instrument("chamber")
        if ch is not None:
            try:
                return float(ch.temperature())
            except Exception:  # pragma: no cover
                pass
        return float(self.lab.cfg.simulation.get("ambient_c", 23.0))

    def check_environment(self, job: Job):
        env = job.procedure.environment
        ambient = self._ambient()
        hum = None
        ch = self.lab.instrument("chamber")
        if self.lab.bench is not None:
            hum = self.lab.bench.humidity_pct
        elif ch is not None:
            hum = ch.humidity()
        lo, hi = float(env.get("ambient_min_c", -1e9)), float(env.get("ambient_max_c", 1e9))
        if not (lo <= ambient <= hi):
            raise Abort(f"ambient {ambient:.1f} C outside the procedure's {lo}-{hi} C")
        job.environment = {"ambient_c": ambient, "humidity_pct": hum, "sample_temperature_c": ambient,
                           "sample_temperature_method": "ambient (no chamber)"}
        if job.temperature_c is not None:
            if ch is None:
                raise Abort("temperature point requested but the lab has no chamber")
            tp = job.procedure.temperatures
            crit = StabilityCriterion(float(tp.get("tol_k", 0.5)), float(tp.get("stable_s", 300)), float(tp.get("max_slope_k_per_min", 0.2)),
                                      float(tp.get("sample_s", 10)), float(tp.get("timeout_s", 4 * 3600)))
            if self.dry_run:
                job.environment.update({"chamber": {"setpoint_c": job.temperature_c, "stable": None}, "sample_temperature_c": job.temperature_c,
                                        "sample_temperature_method": "dry run"})
            else:
                tlog = TemperatureLog()
                ch.set_temperature(job.temperature_c)
                t0 = self.clock.time()
                ch.wait_stable(job.temperature_c, crit, tlog)
                t_stable = self.clock.time()
                soak = 60.0 * float(env.get("sample_soak_min", 0))
                # keep logging during the soak
                remaining = soak
                while remaining > 0:
                    step = min(crit.sample_s, remaining)
                    self.clock.sleep(step)
                    tlog.add(self.clock.time(), ch.temperature(), ch.setpoint())
                    remaining -= step
                air = float(np.mean(tlog.air_c[-max(3, int(60 / crit.sample_s)):]))
                job.environment.update({
                    "chamber": {"setpoint_c": job.temperature_c, "stable": True, "time_to_stable_s": t_stable - t0,
                                "soak_s": soak, "air_c_at_measurement": air, "humidity_pct": ch.humidity(),
                                "criterion": crit.__dict__, "identity": ch.identity.to_dict()},
                    "chamber_log": tlog.to_dict(every=max(1, len(tlog.t) // 400)),
                    "sample_temperature_c": air,
                    "sample_temperature_method": f"chamber air after {soak / 60:.0f} min sample soak",
                })
                job.instrument_states["chamber"] = ch.state()
        self._set(job, JobState.ENVIRONMENT_READY, ambient_c=ambient, sample_temperature_c=job.environment["sample_temperature_c"])

    # ---- measurement + validation
    def measure(self, job: Job):
        proc = job.procedure
        vna = self.lab.instrument("vna")
        readback = vna.configure(proc.sweep)
        job.instrument_states["vna"] = vna.state()
        if abs(readback.start_hz - proc.sweep.start_hz) > 1 or abs(readback.stop_hz - proc.sweep.stop_hz) > 1 or readback.points != proc.sweep.points:
            raise Abort(f"analyser did not accept the sweep: reads back {readback}")
        sample_id = job.entry.sample_id
        # auxiliary measurements first (their hook-ups differ; their values feed the checks)
        for a in proc.aux:
            inst = self.lab.instrument(a.instrument)
            if inst is None:
                job.aux[a.name] = None
                self._log(job, "aux skipped (instrument absent)", name=a.name)
                continue
            if not self.prompter.connect(sample_id, [], a.prompt or f"Connect {sample_id} for {a.name}"):
                raise Abort(f"operator aborted at {a.name}")
            val = getattr(inst, a.method)(**a.args)
            job.aux[a.name] = float(val)
            job.aux[a.name + "_state"] = inst.state()
            self._log(job, "aux measured", name=a.name, value=float(val))
        for spec in proc.files:
            attempt, accepted, last = 0, None, None
            while True:
                attempt += 1
                msg = spec.prompt or f"Connect {sample_id} to ports {spec.ports}"
                if attempt > 1:
                    msg = f"Repeat {attempt}: check the connections and torque; {msg}"
                if not self.prompter.connect(sample_id, spec.ports, msg, spec.port_map):
                    raise Abort(f"operator aborted at file {spec.name}")
                t0 = self.clock.time()
                net = vna.measure(spec.ports, name=f"{sample_id}_{spec.name}")
                dt = self.clock.time() - t0
                errs = vna.errors()
                checks = validate_network(net, spec.port_map, readback, job.entry, proc.checks, job.aux, spec.quantities)
                if errs:
                    checks.append(type(checks[0])("instrument_errors", "fail", float(len(errs)), "none", "; ".join(f"{c}: {m}" for c, m in errs)))
                trust = decide_trust(checks, proc.checks, attempt)
                rec = {"name": spec.name, "ports": spec.ports, "port_map": spec.port_map, "quantities": spec.quantities,
                       "network": net, "attempt": attempt, "sweep_s": dt, "checks": [c.to_dict() for c in checks],
                       "trust": trust.level, "reasons": trust.reasons, "measured_at": self.clock.iso(),
                       "sweep_readback": readback.to_dict()}
                self._log(job, "sweep", file=spec.name, attempt=attempt, trust=trust.level, reasons=trust.reasons,
                          checks={c.name: c.status for c in checks})
                last = rec
                if trust.level != "rejected":
                    accepted = rec
                    break
                if not trust.repeat:
                    break
                self.prompter.notify(f"{spec.name}: {'; '.join(trust.reasons)} - repeating")
            job.files.append(accepted or last)
        self._set(job, JobState.MEASURED, files=[f["name"] for f in job.files])

    def validate(self, job: Job):
        order = {"trusted": 0, "flagged": 1, "rejected": 2}
        worst = max(job.files, key=lambda f: order[f["trust"]]) if job.files else None
        job.trust = worst["trust"] if worst else "rejected"
        reasons = [f"{f['name']}: {r}" for f in job.files for r in f["reasons"]]
        job.reason = "; ".join(reasons)
        self._set(job, JobState.VALIDATED, trust=job.trust, reasons=reasons)

    # ---- archive, analyse, record
    def _sidecar(self, job: Job, f: dict, fname: str) -> dict:
        return {
            "schema": "labauto.measurement/1",
            "job_id": job.job_id, "campaign": job.campaign, "batch": job.batch,
            "operator": job.operator, "site": job.site, "notes": job.notes,
            "sample": {**job.entry.to_dict(), "barcode_parsed": job.barcode.to_dict()},
            "procedure": {**job.procedure.to_dict(), "file": f["name"], "sweep": job.procedure.sweep.to_dict(),
                          "checks": job.procedure.checks},
            "instrument": job.instrument_states,
            "calibration": job.calibration,
            "fixture": job.fixture,
            "environment": job.environment,
            "measurement": {"file": fname, "sha256": None, "ports": f["ports"], "port_map": f["port_map"], "quantities": f["quantities"],
                            "attempt": f["attempt"], "sweep_s": f["sweep_s"], "measured_at": f["measured_at"],
                            "sweep_readback": f["sweep_readback"], "nports": len(f["ports"]), "raw": True,
                            "note": "raw instrument data; fixture not removed"},
            "validation": {"trust": f["trust"], "reasons": f["reasons"], "checks": f["checks"], "job_trust": job.trust},
            "aux": {k: v for k, v in job.aux.items() if not k.endswith("_state")},
            "software": software_versions(),
        }

    def archive(self, job: Job):
        jd = self.lab.archive.job_dir(job.job_id, self.clock.time())
        if job.trust == "rejected":
            jd = jd.parent / "quarantine" / job.job_id
        jd.mkdir(parents=True, exist_ok=True)
        job.archive_dir = jd
        (jd / "procedure.toml").write_text(job.procedure.text, encoding="utf-8")
        tsuffix = f"_T{job.temperature_c:+04.0f}C" if job.temperature_c is not None else ""
        for f in job.files:
            net = f["network"]
            fname = f"{f['name']}{tsuffix}.s{net.nports}p"
            path = jd / fname
            write_touchstone(net, path, comments=[f"labauto {__version__} job {job.job_id}", f"sample {job.entry.sample_id}",
                                                   f"procedure {job.procedure.id} v{job.procedure.version} {job.procedure.hash[:12]}",
                                                   f"calibration {job.calibration.get('id')}", f"trust {f['trust']}"])
            f["path"], f["sha256"], f["nports"] = str(path), sha256_file(path), net.nports
            meta = self._sidecar(job, f, fname)
            meta["measurement"]["sha256"] = f["sha256"]
            missing = check_mandatory(meta, job.procedure.metadata.get("mandatory", []))
            if missing:
                raise MetadataError(f"mandatory metadata missing for {fname}: {missing}")
            write_sidecar(meta, jd / f"{fname}.meta.json")
        (jd / "aux.json").write_text(json.dumps({k: v for k, v in job.aux.items()}, indent=1, default=str), encoding="utf-8")
        self._set(job, JobState.ARCHIVED, dir=str(jd), quarantine=job.trust == "rejected")

    def _seal(self, job: Job):
        jd = job.archive_dir
        with (jd / "journal.jsonl").open("w", encoding="utf-8") as fh:
            for e in job.journal:
                fh.write(json.dumps(e, default=str) + "\n")
        self.lab.archive.seal(jd, job.summary())

    def analyse(self, job: Job):
        jd = job.archive_dir
        sample = {**job.entry.to_dict(), "operator": job.operator, "site": job.site,
                  "instrument": job.instrument_states.get("vna", {}).get("identity", {}).get("model", ""),
                  "instrument_serial": job.instrument_states.get("vna", {}).get("identity", {}).get("serial", ""),
                  "calibration_date": job.calibration.get("date", ""), "temperature_c": job.environment.get("sample_temperature_c"),
                  "humidity_pct": job.environment.get("humidity_pct"), "notes": f"labauto job {job.job_id}; trust {job.trust}"}
        files = [{"path": f["path"], "port_map": f["port_map"], "quantities": f["quantities"], "fixture": job.fixture} for f in job.files]
        names = list(job.procedure.downstream.get("analysers", ["cablecheck"]))
        job.analysis = run_analysers(names, sample, files, job.procedure.cable_type, jd / "analysis",
                                     [str(d) for d in self.lab.cfg.limits_dirs] or None, self.lab.cfg.results_db,
                                     write_report=bool(job.procedure.downstream.get("report", True)))
        self._set(job, JobState.ANALYSED, verdict=job.analysis["cablecheck"]["verdict"],
                  headline_margin=job.analysis["cablecheck"]["headline_margin"],
                  analysers={k: v.get("status", "done") for k, v in job.analysis.items()})

    def record(self, job: Job):
        self.lab.db.upsert_job(job.summary())
        self.lab.db.add_files(job.job_id, [{"name": f["name"], "path": f.get("path"), "sha256": f.get("sha256"), "nports": f.get("nports"),
                                            "port_map": f["port_map"], "attempt": f["attempt"], "trust": f["trust"]} for f in job.files])
        for role, st in job.instrument_states.items():
            self.lab.db.add_instrument_state(job.job_id, role, st)
        self._set(job, JobState.RECORDED)

    def _record_terminal(self, job: Job):
        try:
            self.lab.db.upsert_job(job.summary())
            if job.files and job.archive_dir is not None:
                self.lab.db.add_files(job.job_id, [{"name": f["name"], "path": f.get("path"), "sha256": f.get("sha256"), "nports": f.get("nports"),
                                                    "port_map": f["port_map"], "attempt": f["attempt"], "trust": f["trust"]} for f in job.files])
        except Exception:  # pragma: no cover
            log.exception("could not record terminal state of %s", job.job_id)

    # ---- batches and sweeps
    def run_batch(self, spec: BatchSpec, resume: bool = False, state_path: Path | None = None) -> list[Job]:
        proc = load_procedure(spec.procedure, self.lab.cfg.procedures_dirs)
        state_path = state_path or (self.lab.cfg.root / f"batch_{spec.id}.state.json")
        done: dict[str, str] = {}
        if resume and state_path.exists():
            done = json.loads(state_path.read_text(encoding="utf-8")).get("done", {})
            log.info("resuming batch %s: %d samples already done", spec.id, len(done))
        scans = list(spec.samples)
        scanner = self.lab.instrument("scanner")
        jobs = []

        def next_scan():
            if scans:
                return scans.pop(0)
            if scanner is not None:
                return scanner.scan(f"batch {spec.id}: scan the next sample")
            return None
        while True:
            scan = next_scan()
            if scan is None:
                break
            if scan in done and done[scan] in ("DONE",):
                log.info("skipping %s (already %s)", scan, done[scan])
                continue
            job = self.new_job(scan, proc, spec.operator, spec.site, spec.id, spec.campaign, notes=spec.notes)
            self.run_job(job)
            jobs.append(job)
            done[scan] = job.state
            state_path.write_text(json.dumps({"batch": spec.id, "procedure": proc.id, "done": done, "updated": self.clock.iso()}, indent=1), encoding="utf-8")
        return jobs

    def run_sweep(self, spec: SweepSpec) -> tuple[list[Job], Path]:
        proc = load_procedure(spec.procedure, self.lab.cfg.procedures_dirs)
        if proc.kind != "temperature-sweep":
            raise ValueError(f"{proc.id} is not a temperature-sweep procedure")
        temps = [float(t) for t in (spec.temperatures or proc.temperatures["values"])]
        jobs = []
        for T in temps:
            job = self.new_job(spec.sample, proc, spec.operator, spec.site, spec.id, spec.campaign or spec.id, T, spec.notes)
            self.run_job(job)
            jobs.append(job)
            if job.state == JobState.ABORTED and "calibration" in job.reason:
                break               # no point heating on with no calibration
        ch = self.lab.instrument("chamber")
        if ch is not None and not self.dry_run:
            try:
                ch.set_temperature(self._ambient())
            except Exception:  # pragma: no cover
                pass
        summary = self.lab.cfg.root / "sweeps" / f"{spec.id}_summary.csv"
        summary.parent.mkdir(parents=True, exist_ok=True)
        lines = ["setpoint_c,sample_temperature_c,state,trust,verdict,headline,headline_margin_db,il_100mhz_db,il_600mhz_db,r_loop_ohm,archive_dir"]
        for j in jobs:
            il100 = il600 = ""
            f0 = next((f for f in j.files if "far" in f["port_map"]), None)
            if f0 is not None:
                from cablecheck.mixedmode import PortMap, to_mixed_mode
                net = f0["network"]
                pm = PortMap.parse(f0["port_map"])
                mm = to_mixed_mode(net, pm)
                p = pm.pairs[0]
                s21 = mm.param(("d", p, "far"), ("d", p, "near"))
                il = -20 * np.log10(np.abs(s21) + 1e-30)
                il100 = f"{np.interp(100e6, net.f, il):.3f}" if net.f[0] <= 100e6 <= net.f[-1] else ""
                il600 = f"{np.interp(600e6, net.f, il):.3f}" if net.f[0] <= 600e6 <= net.f[-1] else ""
            cc = j.analysis.get("cablecheck", {})
            hm = cc.get("headline_margin")
            lines.append(",".join(map(str, [j.temperature_c, f"{j.environment.get('sample_temperature_c', ''):.2f}" if j.environment.get("sample_temperature_c") is not None else "",
                                            j.state, j.trust, cc.get("verdict", ""), cc.get("headline", ""), f"{hm:.3f}" if hm is not None else "",
                                            il100, il600, j.aux.get("loop_resistance", ""), j.archive_dir or ""])))
        summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return jobs, summary
