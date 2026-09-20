"""The orchestration engine end to end on the simulated laboratory: gates, trust, archive, replay, sweeps, CLI."""
import json

import numpy as np
import pytest

from labauto.cli import main
from labauto.demo import init_demo
from labauto.engine import AutoPrompter, BatchSpec, Engine, JobState, SweepSpec
from labauto.lab import Lab, LabConfig
from labauto.procedure import load_procedure, parse_procedure


def _lab(root, **kw):
    return Lab(LabConfig.load(root / "lab.toml"), **kw)


def test_batch_end_to_end(demo):
    root, info = demo
    with _lab(root) as lab:
        eng = Engine(lab)
        jobs = eng.run_batch(BatchSpec.load(root / "batch_incoming.toml"))
        by = {j.entry.sample_id: j for j in jobs}
        assert len(jobs) == 6
        good, lossy, defect, wrong = by["C1000T1A-L24071-0041"], by["C1000T1A-L24072-0007"], by["C1000T1A-L24072-0008"], by["C1000T1A-L24072-0009"]
        # the calibration was unverified -> verified once, then reused
        assert good.calibration["decision"]["status"] == "valid" and good.calibration["verification"]["status"] == "pass"
        assert lab.calstore.get(info["calibration"]).last_verification().status == "pass"
        assert sum(1 for r in lab.instrument("vna").t.log if "verify" in r) == 0   # verification is ordinary measurement
        # trust and verdicts
        assert good.state == JobState.DONE and good.trust == "trusted" and good.analysis["cablecheck"]["verdict"] == "PASS"
        assert lossy.state == JobState.DONE and lossy.analysis["cablecheck"]["verdict"] == "FAIL"
        assert lossy.analysis["cablecheck"]["headline"] == "insertion_loss[A]"
        assert defect.analysis["cablecheck"]["verdict"] == "FAIL" and defect.analysis["cablecheck"]["headline"].startswith("impedance_min")
        # the mislabelled sample: length check fails, repeated once, then quarantined
        assert wrong.state == JobState.REJECTED and wrong.trust == "rejected"
        assert [f["attempt"] for f in wrong.files] == [2]
        assert any(c["name"] == "length" and c["status"] == "fail" for c in wrong.files[0]["checks"])
        assert "quarantine" in str(wrong.archive_dir) and not (wrong.archive_dir / "analysis").exists()
        assert wrong.analysis == {}
        # archive contents of a good job
        jd = good.archive_dir
        for name in ("pairA.s4p", "pairA.s4p.meta.json", "aux.json", "journal.jsonl", "procedure.toml", "manifest.json",
                     "analysis/cablecheck_report.html", "analysis/cablecheck_result.json"):
            assert (jd / name).exists(), name
        # optional analysers: used when installed, recorded as skipped otherwise
        for pkg, fname in (("zprofile", "analysis/zprofile.json"), ("cableanalytics", "analysis/cableanalytics.json")):
            try:
                __import__(pkg)
                assert (jd / fname).exists() and good.analysis[pkg]["status"] == "done"
            except ImportError:
                assert good.analysis[pkg]["status"] == "skipped" and "not installed" in good.analysis[pkg]["reason"]
        meta = json.loads((jd / "pairA.s4p.meta.json").read_text())
        assert meta["measurement"]["sha256"] == good.files[0]["sha256"]
        assert meta["instrument"]["vna"]["identity"]["serial"] == "SIM0001"
        assert meta["instrument"]["vna"]["sweep"]["points"] == 1200
        assert meta["calibration"]["id"] == info["calibration"]
        assert meta["procedure"]["hash"] == good.procedure.hash
        assert meta["validation"]["trust"] == "trusted" and {c["name"] for c in meta["validation"]["checks"]} >= {"grid", "passivity", "reciprocity", "connection", "trace_noise", "length", "il_rdc", "il_envelope"}
        assert meta["aux"]["loop_resistance"] > 2.0
        assert meta["software"]["cablecheck"] and meta["software"]["labauto"]
        man = json.loads((jd / "manifest.json").read_text())
        assert man["job"]["state"] == "DONE" and "analysis/cablecheck_report.html" in man["files"]
        # journal has the whole path
        states = [e["state"] for e in good.journal]
        for s in ("IDENTIFIED", "INSTRUMENTS_READY", "CALIBRATED", "FIXTURE_READY", "ENVIRONMENT_READY", "MEASURED", "VALIDATED", "ARCHIVED", "ANALYSED", "RECORDED", "DONE"):
            assert s in states
        # databases
        assert lab.db.counts() == {"jobs": 6, "files": 6, "trusted": 5, "rejected": 1}
        from cablecheck.db import ResultsDB
        with ResultsDB(lab.cfg.results_db) as rdb:
            assert len(rdb.runs()) == 5                       # quarantined data never reached the engineering database
        # archive integrity and replay
        for j in jobs:
            assert lab.archive.verify(j.archive_dir)["ok"]
        r = lab.archive.replay(good.archive_dir)
        assert r["ok"] and r["verdict_replayed"] == "PASS" and r["headline_replayed"] == pytest.approx(good.analysis["cablecheck"]["headline_margin"])
        assert lab.archive.replay(wrong.archive_dir)["skipped"]
        # tampering is detected
        p = good.archive_dir / "pairA.s4p"
        p.write_text(p.read_text().replace("labauto", "lababuto", 1))
        v = lab.archive.verify(good.archive_dir)
        assert not v["ok"] and v["modified"] == ["pairA.s4p"]
        assert lab.archive.replay(good.archive_dir)["ok"] is False


def test_resume_skips_finished_samples(demo):
    root, info = demo
    spec = BatchSpec.load(root / "batch_incoming.toml")
    spec.samples = spec.samples[:2]
    with _lab(root) as lab:
        jobs = Engine(lab).run_batch(spec)
        assert [j.state for j in jobs] == ["DONE", "DONE"]
    spec.samples = BatchSpec.load(root / "batch_incoming.toml").samples[:3]
    with _lab(root) as lab:
        jobs = Engine(lab).run_batch(spec, resume=True)
        assert len(jobs) == 1 and jobs[0].entry.sample_id == "C1000T1A-L24071-0043"


def test_gates_refuse_before_measuring(demo):
    root, info = demo
    proc = load_procedure("sparam-1000base-t1-pair")
    # 1. no valid calibration (30 days old)
    old = init_demo(root.parent / "old", cal_age_days=30)
    with _lab(root.parent / "old") as lab:
        j = Engine(lab).run_job(Engine(lab).new_job(old["barcodes"][0], proc, "op"))
        assert j.state == JobState.ABORTED and "days old" in j.reason and j.files == []
        assert lab.instrument("vna").sim.sweeps == 0
    # 2. thermal drift: calibrated at 23 C, room at 28 C
    warm = init_demo(root.parent / "warm", ambient_c=28.0)
    with _lab(root.parent / "warm") as lab:
        j = Engine(lab).run_job(Engine(lab).new_job(warm["barcodes"][0], proc, "op"))
        assert j.state == JobState.ABORTED and ("ambient differs" in j.reason or "ambient" in j.reason)
    # 3. a sloppy calibration fails verification
    (root / "lab.toml").write_text((root / "lab.toml").read_text().replace("cal_quality = 1.0", "cal_quality = 8.0"))
    with _lab(root) as lab:
        j = Engine(lab).run_job(Engine(lab).new_job(info["barcodes"][0], proc, "op"))
        assert j.state == JobState.ABORTED and "failed verification" in j.reason
        assert lab.db.con.execute("SELECT verification_status FROM calibrations").fetchall()[-1][0] == "fail"
    (root / "lab.toml").write_text((root / "lab.toml").read_text().replace("cal_quality = 8.0", "cal_quality = 1.0"))
    # 4. capability: a procedure that needs 8 ports / 3 GHz
    big = parse_procedure(proc.text.replace("ports = 4\nfmin_hz = 1e6\nfmax_hz = 600e6", "ports = 8\nfmin_hz = 1e6\nfmax_hz = 600e6"), "x")
    with _lab(root) as lab:
        j = Engine(lab).run_job(Engine(lab).new_job(info["barcodes"][0], big, "op"))
        assert j.state == JobState.ABORTED and "needs 8 ports" in j.reason
    # 5. wrong cable type for the procedure
    with _lab(root) as lab:
        j = Engine(lab).run_job(Engine(lab).new_job(info["barcodes"][7], proc, "op"))     # the generic STP sample
        assert j.state == JobState.ABORTED and "registry says" in j.reason
    # 6. unknown sample
    with _lab(root) as lab:
        from labauto.barcode import make_internal_barcode
        j = Engine(lab).run_job(Engine(lab).new_job(make_internal_barcode("X", "Y", "1"), proc, "op"))
        assert j.state == JobState.ABORTED and "not in the sample registry" in j.reason
    # 7. operator abort at the hook-up prompt
    with _lab(root) as lab:
        eng = Engine(lab, prompter=AutoPrompter(lab.bench, fail_on={"C1000T1A-L24071-0041"}))
        j = eng.run_job(eng.new_job(info["barcodes"][0], proc, "op"))
        assert j.state == JobState.ABORTED and "operator aborted" in j.reason


def test_mandatory_metadata_is_enforced(demo):
    root, info = demo
    proc = load_procedure("sparam-1000base-t1-pair")
    strict = parse_procedure(proc.text.replace('"software.cablecheck",', '"software.cablecheck", "environment.chamber.setpoint_c",'), "x")
    with _lab(root) as lab:
        j = Engine(lab).run_job(Engine(lab).new_job(info["barcodes"][0], strict, "op"))
        assert j.state == JobState.ERROR and "mandatory metadata missing" in j.error and "environment.chamber.setpoint_c" in j.error
        assert not any(p.name == "manifest.json" for p in lab.archive.root.rglob("*"))


def test_dry_run_plans_without_measuring(demo):
    root, info = demo
    with _lab(root) as lab:
        jobs = Engine(lab, dry_run=True).run_batch(BatchSpec.load(root / "batch_incoming.toml"))
        assert all(j.state == JobState.PLANNED for j in jobs)
        assert lab.instrument("vna").sim.sweeps == 0
        assert lab.archive.jobs() == []


def test_forgotten_termination_is_caught_and_repeated(demo):
    root, info = demo
    (root / "lab.toml").write_text((root / "lab.toml").read_text() + 'unused_ports = "open"\n')
    with _lab(root) as lab:
        eng = Engine(lab)
        jobs = eng.run_batch(BatchSpec.load(root / "batch_next.toml"))
        j = jobs[0]
        assert j.state == JobState.REJECTED
        f = {x["name"]: x for x in j.files}
        # the through files barely notice the open pair (1 % coupling); the NEXT file does, at low frequency
        assert f["pairA"]["trust"] == "trusted" and f["pairB"]["trust"] == "trusted"
        assert f["nextAB"]["attempt"] == 2 and any(c["name"] == "far_end_termination" and c["status"] == "fail" for c in f["nextAB"]["checks"])
        assert "far_end_termination" in j.reason
    (root / "lab.toml").write_text((root / "lab.toml").read_text().replace('unused_ports = "open"\n', ""))
    with _lab(root) as lab:
        j = Engine(lab).run_batch(BatchSpec.load(root / "batch_next.toml"))[0]
        assert j.state == JobState.DONE and [f["name"] for f in j.files] == ["pairA", "pairB", "nextAB"]
        assert any(r["quantity"] == "next" for r in j.analysis["cablecheck"]["results"])


def test_temperature_sweep(demo):
    root, info = demo
    spec = SweepSpec.load(root / "sweep_hot.toml")
    spec.temperatures = [-40, 23, 105]
    with _lab(root) as lab:
        eng = Engine(lab)
        t0 = lab.clock.time()
        jobs, summary = eng.run_sweep(spec)
        assert [j.state for j in jobs] == ["DONE"] * 3
        assert lab.clock.time() - t0 > 3 * 45 * 60                       # the soaks were really waited for (on the sim clock)
        il600 = []
        for j in jobs:
            env = j.environment
            assert env["chamber"]["stable"] and env["chamber"]["soak_s"] == 45 * 60
            assert abs(env["sample_temperature_c"] - j.temperature_c) < 0.6
            assert "after 45 min sample soak" in env["sample_temperature_method"]
            meta = json.loads((j.archive_dir / f"pairA_T{j.temperature_c:+04.0f}C.s4p.meta.json").read_text())
            assert len(meta["environment"]["chamber_log"]["t"]) > 20
            assert meta["environment"]["chamber"]["identity"]["serial"] == "SIM0002"
            from cablecheck.io import read_touchstone
            from cablecheck.mixedmode import PortMap, to_mixed_mode
            net = read_touchstone(j.files[0]["path"])
            mm = to_mixed_mode(net, PortMap.single_pair())
            il600.append(-20 * np.log10(abs(mm.param(("d", "A", "far"), ("d", "A", "near"))[-1])))
        assert il600[0] < il600[1] < il600[2]                              # loss rises with temperature
        rl = [j.aux["loop_resistance"] for j in jobs]
        assert rl[0] < rl[1] < rl[2]
        assert rl[2] / rl[1] == pytest.approx((1 + 0.00393 * 85) / (1 + 0.00393 * 3), rel=0.03)
        text = summary.read_text().splitlines()
        assert text[0].startswith("setpoint_c,") and len(text) == 4
        # each temperature point is a separately sealed, replayable job
        assert all(lab.archive.replay(j.archive_dir)["ok"] for j in jobs)


def test_cli_smoke(tmp_path, capsys):
    d = tmp_path / "cli"
    assert main(["init-demo", str(d), "--verified"]) == 0
    assert main(["procedures", "--lab", str(d / "lab.toml")]) == 0
    out = capsys.readouterr().out
    assert "sparam-1000base-t1-pair" in out and "OK on this lab" in out
    assert main(["procedures", "--show", "sparam-generic-stp"]) == 0
    assert main(["cal", "--lab", str(d / "lab.toml"), "list"]) == 0
    assert main(["cal", "--lab", str(d / "lab.toml"), "policy"]) == 0
    assert "valid" in capsys.readouterr().out
    assert main(["instruments", "--lab", str(d / "lab.toml")]) == 0
    assert main(["run", "--lab", str(d / "lab.toml"), str(d / "batch_incoming.toml"), "--dry-run"]) == 0
    assert "PLANNED" in capsys.readouterr().out
    rc = main(["run", "--lab", str(d / "lab.toml"), str(d / "batch_incoming.toml")])
    assert rc == 1                                                      # one sample was quarantined
    out = capsys.readouterr().out
    assert out.count("DONE") == 5 and "REJECTED" in out
    assert main(["archive", "--lab", str(d / "lab.toml"), "list"]) == 0
    assert main(["archive", "--lab", str(d / "lab.toml"), "verify"]) == 0
    assert main(["archive", "--lab", str(d / "lab.toml"), "replay"]) == 0
    out = capsys.readouterr().out
    assert out.count("REPRODUCED") == 5 and "SKIPPED" in out
    codes = json.loads((d / "batch_incoming.toml").read_text().split("samples = ")[1].splitlines()[0])
    assert main(["scan", "--lab", str(d / "lab.toml"), codes[0], "NOT-A-LABEL"]) == 0
    out = capsys.readouterr().out
    assert "C1000T1A-L24071-0041" in out and "REFUSED" in out
    assert main(["job", "--lab", str(d / "lab.toml"), codes[1], "sparam-1000base-t1-pair", "--json"]) == 0
    assert '"trust": "trusted"' in capsys.readouterr().out
