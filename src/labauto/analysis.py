"""
Downstream analysers: the archive feeds the engineering tools.

``cablecheck`` (project A) is mandatory - it produces the verdict, the
headline margin, the HTML report and the results-database row.  The
others run when their packages are installed and are recorded as
``"skipped"`` otherwise, so an archive record always says which analyses
were attempted with which software versions:

    zprofile        (D) impedance profile of the pair from the archived file
    cableanalytics  (E) three-term loss fit and periodic-signature search
    shieldeval      (C) not applicable to S-parameter procedures; listed for completeness

Analysers never see instrument handles: they read archived files only, so
``labauto replay`` reproduces exactly what the batch produced.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from cablecheck.pipeline import (
    FixtureSpec,
    MeasurementFile,
    SampleInfo,
    run_sample,
    write_json,
)
from cablecheck.report.html import write_html

from .metadata import _default

__all__ = ["run_cablecheck", "run_analysers", "ANALYSERS"]

log = logging.getLogger("labauto.analysis")


def _fixture(d: dict) -> FixtureSpec:
    d = d or {}
    return FixtureSpec(method=d.get("method", "none"), left=d.get("left"), right=d.get("right"), thru=d.get("thru"),
                       delays_ps=d.get("delays_ps"), loss_db_at_1ghz=d.get("loss_db_at_1ghz"))


def run_cablecheck(sample: dict, files: list[dict], cable_type: str, out_dir: Path, extra_limit_dirs=None,
                   write_report: bool = True, db_path: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    si = SampleInfo(sample_id=sample["sample_id"], cable_type=cable_type, length_m=sample.get("length_m"),
                    lot=sample.get("lot", ""), part_number=sample.get("part_number", ""), operator=sample.get("operator", ""),
                    instrument=sample.get("instrument", ""), instrument_serial=sample.get("instrument_serial", ""),
                    calibration_date=sample.get("calibration_date", ""), temperature_c=sample.get("temperature_c"),
                    humidity_pct=sample.get("humidity_pct"), site=sample.get("site", ""), notes=sample.get("notes", ""))
    mfs = [MeasurementFile(f["path"], f["port_map"], _fixture(f.get("fixture")), f.get("quantities")) for f in files]
    res = run_sample(si, mfs, cable_type, extra_limit_dirs)
    out = {"verdict": res.verdict, "software": res.software_version, "warnings": res.warnings + res.evaluation.warnings,
           "results": res.evaluation.summary_rows(), "limit_status": res.cable_type.status}
    hl = res.evaluation.headline
    out["headline_margin"] = float(hl.worst_margin) if hl and hl.worst_margin is not None else None
    out["headline"] = f"{hl.quantity}[{hl.pair}]" if hl else None
    out["headline_x"] = float(hl.x_worst) if hl and hl.x_worst is not None else None
    write_json(res, out_dir / "cablecheck_result.json", include_traces=True)
    out["result_json"] = "cablecheck_result.json"
    if write_report:
        write_html(res, out_dir / "cablecheck_report.html")
        out["report_html"] = "cablecheck_report.html"
    if db_path is not None:
        from cablecheck.db import ResultsDB
        with ResultsDB(db_path) as db:
            out["db_run_id"] = db.insert_run(res, report_path=str(out_dir / "cablecheck_report.html") if write_report else None)
    return out


def run_zprofile(sample: dict, files: list[dict], cable_type: str, out_dir: Path) -> dict:
    try:
        from cablecheck.io import read_touchstone
        from cablecheck.mixedmode import PortMap, to_mixed_mode
        from zprofile.profile import Settings, compute_profile
    except ImportError:
        return {"status": "skipped", "reason": "zprofile not installed"}
    f0 = next((f for f in files if len(f["port_map"].split(",")) == 4 and "far" in f["port_map"] and "near" in f["port_map"]), None)
    if f0 is None:
        return {"status": "skipped", "reason": "no through-pair file"}
    net = read_touchstone(f0["path"])
    pm = PortMap.parse(f0["port_map"])
    mm = to_mixed_mode(net, pm)
    pair = pm.pairs[0]
    d = ("d", pair, "near"), ("d", pair, "far")
    s11, s21 = mm.param(d[0], d[0]), mm.param(d[1], d[0])
    s12, s22 = mm.param(d[0], d[1]), mm.param(d[1], d[1])
    prof = compute_profile(net.f, s11, 100.0, sample.get("length_m"), s21, Settings(), s12, s22)
    out = {"status": "done", "summary": prof.to_dict()}
    (out_dir / "zprofile.json").write_text(json.dumps({"x_m": prof.x.tolist(), "z_ohm": prof.z.tolist(), **out["summary"]},
                                                      indent=1, default=_default), encoding="utf-8")
    out["file"] = "zprofile.json"
    return out


def run_cableanalytics(sample: dict, files: list[dict], cable_type: str, out_dir: Path) -> dict:
    try:
        from cableanalytics.lab import evaluate_touchstone
    except ImportError:
        return {"status": "skipped", "reason": "cableanalytics not installed"}
    f0 = next((f for f in files if "far" in f["port_map"] and "near" in f["port_map"]), None)
    if f0 is None or not sample.get("length_m"):
        return {"status": "skipped", "reason": "no through-pair file or unknown length"}
    r = evaluate_touchstone(f0["path"], float(sample["length_m"]))
    (out_dir / "cableanalytics.json").write_text(json.dumps(r, indent=1, default=_default), encoding="utf-8")
    return {"status": "done", "fit": r["fit"], "period": r.get("period"), "file": "cableanalytics.json"}


ANALYSERS = {"cablecheck": run_cablecheck, "zprofile": run_zprofile, "cableanalytics": run_cableanalytics}


def run_analysers(names: list[str], sample: dict, files: list[dict], cable_type: str, out_dir: Path,
                  extra_limit_dirs=None, db_path: Path | None = None, write_report: bool = True) -> dict:
    out = {}
    for name in names:
        fn = ANALYSERS.get(name)
        if fn is None:
            out[name] = {"status": "skipped", "reason": "unknown analyser"}
            continue
        try:
            if name == "cablecheck":
                out[name] = run_cablecheck(sample, files, cable_type, out_dir, extra_limit_dirs, write_report, db_path)
            else:
                out[name] = fn(sample, files, cable_type, out_dir)
        except Exception as e:  # an optional analyser must not take the job down
            log.exception("analyser %s failed", name)
            out[name] = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
            if name == "cablecheck":
                raise
    return out
