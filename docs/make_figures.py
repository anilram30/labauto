"""Figures for the report: run the simulated laboratory and plot what it did.  ``python docs/make_figures.py``"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from labauto.clock import SimClock
from labauto.demo import init_demo
from labauto.drivers.chamber import StabilityCriterion, TemperatureLog
from labauto.drivers.vna import SweepSettings
from labauto.engine import BatchSpec, Engine, SweepSpec
from labauto.lab import Lab, LabConfig
from labauto.validation import validate_network

C1, C2, C3, C4, CK, CG, CPASS, CFAIL = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#0b0b0b", "#9a9a96", "#008300", "#e34948"
OUT = Path(__file__).parent / "figures"
OUT.mkdir(exist_ok=True)
WORK = Path(__file__).parent / "_figwork"


def style(ax):
    ax.grid(True, color="#e6e6e3", lw=0.7)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def fig_state_machine():
    fig, ax = plt.subplots(figsize=(12, 3.6))
    ax.axis("off")
    states = ["CREATED", "IDENTIFIED", "INSTRUMENTS\nREADY", "CALIBRATED", "FIXTURE\nREADY", "ENVIRONMENT\nREADY", "MEASURED", "VALIDATED", "ARCHIVED", "ANALYSED", "RECORDED", "DONE"]
    gates = ["registry", "capabilities", "cal policy\n+ verification", "fixture files", "ambient /\nchamber", "sweeps +\naux", "checks ->\ntrust", "sidecars +\nmandatory", "A / D / E", "lab DB"]
    x = np.arange(len(states)) * 1.0
    for i, s in enumerate(states):
        ax.add_patch(plt.Rectangle((x[i] - 0.42, 0.55), 0.84, 0.5, fc="#eef4fc" if s != "DONE" else "#e6f4ea", ec=C1 if s != "DONE" else CPASS, lw=1.2))
        ax.text(x[i], 0.8, s, ha="center", va="center", fontsize=7.2, color=CK)
        if i < len(states) - 1:
            ax.annotate("", xy=(x[i + 1] - 0.42, 0.8), xytext=(x[i] + 0.42, 0.8), arrowprops=dict(arrowstyle="->", color=CG, lw=1))
            if i < len(gates):
                ax.text(x[i] + 0.5, 1.12, gates[i], ha="center", va="bottom", fontsize=6.3, color=CG)
    # terminal states
    for xx, w, lab, col in [(2.0, 2.4, "ABORTED\n(a gate refused: nothing measured)", C2), (8.3, 2.4, "REJECTED\n(quarantined raw data, no analysis)", CFAIL), (5.1, 1.8, "ERROR\n(journal preserved)", C4)]:
        ax.add_patch(plt.Rectangle((xx - w / 2, -0.15), w, 0.45, fc="#fff5f0", ec=col, lw=1.2))
        ax.text(xx, 0.075, lab, ha="center", va="center", fontsize=6.8, color=col)
    ax.annotate("", xy=(2.0, 0.3), xytext=(2.5, 0.55), arrowprops=dict(arrowstyle="->", color=C2, lw=1))
    ax.annotate("", xy=(8.3, 0.3), xytext=(8.0, 0.55), arrowprops=dict(arrowstyle="->", color=CFAIL, lw=1))
    ax.set_xlim(-0.6, len(states) - 0.4), ax.set_ylim(-0.25, 1.5)
    ax.set_title("A job is a state machine; every transition is journalled with its evidence", loc="left", fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT / "state_machine.png", dpi=140)
    plt.close(fig)


def fig_chamber(lab):
    ch, bench, clock = lab.instrument("chamber"), lab.bench, lab.clock
    crit = StabilityCriterion(0.5, 300, 0.2, 10, 4 * 3600)
    tlog = TemperatureLog()
    t0 = clock.time()
    dut = []

    def prog(dt, air):
        dut.append((clock.time() - t0, bench.chamber.dut_c))
    ch.set_temperature(85.0)
    ch.wait_stable(85.0, crit, tlog, progress=prog)
    t_stable = clock.time() - t0
    for _ in range(int(60 * 60 / 10)):
        clock.sleep(10)
        tlog.add(clock.time(), ch.temperature(), ch.setpoint())
        dut.append((clock.time() - t0, bench.chamber.dut_c))
    t = np.array(tlog.t) - t0
    dut = np.array(dut)
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.8))
    ax = axes[0]
    ax.plot(t / 60, tlog.setpoint_c, color=CG, lw=1, ls="--", label="setpoint")
    ax.plot(t / 60, tlog.air_c, color=C1, lw=1.5, label="air (what the chamber reports)")
    ax.plot(dut[:, 0] / 60, dut[:, 1], color=C2, lw=1.5, label="sample core (not observable)")
    ax.axvline(t_stable / 60, color=CK, lw=0.8)
    ax.text(t_stable / 60 + 1, 30, "chamber 'stable'", fontsize=8)
    ax.axvline((t_stable + 45 * 60) / 60, color=CK, lw=0.8, ls=":")
    ax.text((t_stable + 45 * 60) / 60 + 1, 30, "45 min soak", fontsize=8)
    ax.set_xlabel("time / min"), ax.set_ylabel("temperature / °C")
    ax.set_title("23 → 85 °C step: two-node thermal model", loc="left", fontsize=10)
    ax.legend(fontsize=8, frameon=False, loc="lower right")
    style(ax)
    ax = axes[1]
    tau = 720.0
    soak = np.linspace(0, 90, 200)
    dT = 62.0
    err = dT * np.exp(-soak * 60 / tau)
    ax.plot(soak, err, color=C2, lw=1.5, label="bound ΔT·exp(−t/τ), τ = 12 min: core still cold when the air settles")
    m = (t_stable + 0 <= dut[:, 0])
    ax.plot((dut[m, 0] - t_stable) / 60, 85.0 - dut[m, 1], color=C1, lw=1.2, ls="--", label="simulated: the core already follows during the 3 K/min ramp")
    ax.axhline(0.5, color=CK, lw=0.8), ax.text(60, 0.7, "0.5 K", fontsize=8)
    ax.set_yscale("log"), ax.set_ylim(0.05, 100)
    ax.set_xlabel("sample soak after the chamber is stable / min"), ax.set_ylabel("temperature label error / K")
    ax.set_title("why the procedure carries a sample soak", loc="left", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    style(ax)
    fig.tight_layout()
    fig.savefig(OUT / "chamber.png", dpi=140)
    plt.close(fig)
    ch.set_temperature(23.0)
    clock.sleep(2 * 3600)


def fig_calibration(lab, info):
    vna, bench, store = lab.instrument("vna"), lab.bench, lab.calstore
    from labauto.calibration import compare_to_reference
    rec = store.get(info["calibration"])
    vna.configure(SweepSettings(1e6, 600e6, 1200, 1e3, 0.0))
    vna.select_calset(rec.instrument_calset)
    ref = store.reference_network("check_att20.s2p")
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.8))
    ax = axes[0]
    for dT, col in [(0, C1), (4, C3), (8, C4), (12, C2)]:
        bench.ambient_c = 23.0 + dT
        bench.connect("CHECK-ATT20")
        net = vna.measure([1, 2])
        r = ref.interpolate(net.f)
        dev = 20 * np.log10(np.abs(net.s[:, 1, 0])) - 20 * np.log10(np.abs(r.s[:, 1, 0]))
        ax.plot(net.f / 1e6, dev, color=col, lw=1, label=f"ΔT = {dT} K  (max |dev| {np.max(np.abs(dev)):.3f} dB)")
    bench.ambient_c = 23.0
    ax.axhline(0.1, color=CK, lw=0.8, ls="--"), ax.axhline(-0.1, color=CK, lw=0.8, ls="--")
    ax.text(10, 0.107, "tolerance ±0.10 dB", fontsize=8)
    ax.set_xlabel("frequency / MHz"), ax.set_ylabel("|S21| deviation from the certificate / dB")
    ax.set_title("check-standard verification as the room drifts from T_cal", loc="left", fontsize=10)
    ax.legend(fontsize=7.5, frameon=False)
    style(ax)
    ax = axes[1]
    dTs = np.linspace(0, 15, 100)
    for age, col in [(0, C1), (3, C3), (7, C4), (14, C2)]:
        ax.plot(dTs, 0.02 + 0.012 * dTs + 0.004 * age, color=col, lw=1.4, label=f"cal age {age} d")
    ax.axhline(0.1, color=CK, lw=0.8, ls="--"), ax.axvline(3.0, color=CG, lw=0.8, ls=":")
    ax.text(3.1, 0.19, "policy: |ΔT| ≤ 3 K", fontsize=8, color=CG), ax.text(0.2, 0.105, "verification tolerance", fontsize=8)
    ax.set_xlabel("|T_ambient − T_cal| / K"), ax.set_ylabel("residual tracking error e_T / dB (model)")
    ax.set_title("the residual-error model behind the policy limits", loc="left", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    style(ax)
    fig.tight_layout()
    fig.savefig(OUT / "calibration.png", dpi=140)
    plt.close(fig)


def fig_noise(lab):
    vna, bench = lab.instrument("vna"), lab.bench
    entry = lab.registry.by_id["C1000T1A-L24071-0041"]
    bench.connect("C1000T1A-L24071-0041")
    vna.select_calset(vna.calsets()[0])
    checks = {"passivity_max": 0.02, "reciprocity_max_db": 0.1, "trace_noise_max_db": 0.05, "connection_min_db": -60,
              "length_tol_pct": 12, "nvp": 0.68, "il_rdc_ratio": [0.8, 4], "il_per_m_100mhz_db": [0.05, 0.6]}
    ifbws = [100, 300, 1e3, 3e3, 10e3, 30e3, 100e3]
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.8))
    ax = axes[0]
    for avg, pwr, col in [(1, 0.0, C1), (4, 0.0, C3), (16, 0.0, C2), (1, -20.0, C4)]:
        vals, times = [], []
        for b in ifbws:
            s = vna.configure(SweepSettings(1e6, 600e6, 1200, b, pwr, avg))
            t0 = lab.clock.time()
            net = vna.measure([1, 2, 3, 4])
            times.append(lab.clock.time() - t0)
            c = {x.name: x for x in validate_network(net, "A+near,A-near,A+far,A-far", s, entry, checks)}
            vals.append(c["trace_noise"].value)
        ax.plot(ifbws, vals, "o-", color=col, lw=1.2, ms=4, label=f"{avg} average(s), {pwr:+.0f} dBm")
        if avg == 1 and pwr == 0.0:
            axes[1].plot(ifbws, times, "o-", color=C1, lw=1.2, ms=4, label="1 average")
        if avg == 16:
            axes[1].plot(ifbws, times, "o-", color=C2, lw=1.2, ms=4, label="16 averages")
    ax.axhline(0.05, color=CK, lw=0.8, ls="--"), ax.text(120, 0.058, "trace_noise check bound 0.05 dB", fontsize=8)
    ax.text(120, 0.00013, "floor: the cable's own fine structure", fontsize=7.5, color=CG)
    ax.set_xscale("log"), ax.set_yscale("log")
    ax.set_xlabel("IF bandwidth / Hz"), ax.set_ylabel("trace-noise estimate on IL / dB")
    ax.set_title("the procedure's IFBW/averaging choice is visible in the check", loc="left", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    style(ax)
    ax = axes[1]
    ax.set_xscale("log"), ax.set_yscale("log")
    ax.set_xlabel("IF bandwidth / Hz"), ax.set_ylabel("4-port sweep time, 1200 points / s")
    ax.set_title("and so is its cost", loc="left", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    style(ax)
    fig.tight_layout()
    fig.savefig(OUT / "noise_vs_ifbw.png", dpi=140)
    plt.close(fig)


def fig_batch(root):
    with Lab(LabConfig.load(root / "lab.toml")) as lab:
        eng = Engine(lab)
        jobs = eng.run_batch(BatchSpec.load(root / "batch_incoming.toml"))
        jobs += eng.run_batch(BatchSpec.load(root / "batch_next.toml"))
        summary = []
        for j in jobs:
            cc = j.analysis.get("cablecheck", {})
            summary.append({"sample": j.entry.sample_id, "profile": j.entry.extra.get("sim_profile"), "state": j.state, "trust": j.trust,
                            "verdict": cc.get("verdict"), "headline": cc.get("headline"), "margin": cc.get("headline_margin"),
                            "attempts": max(f["attempt"] for f in j.files), "reason": j.reason[:160],
                            "checks": {f["name"]: {c["name"]: (c["status"], c["value"]) for c in f["checks"]} for f in j.files},
                            "archive": str(j.archive_dir.relative_to(lab.cfg.root)) if j.archive_dir else None,
                            "calibration": j.calibration.get("decision", {}).get("status"),
                            "verification": j.calibration.get("verification", {}).get("status")})
        (OUT / "batch_summary.json").write_text(json.dumps(summary, indent=1, default=float))
        counts = lab.db.counts()
        replay = [lab.archive.replay(j.archive_dir) for j in jobs]
        (OUT / "replay_summary.json").write_text(json.dumps({"db": counts, "replay": replay}, indent=1, default=str))
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.9))
    ax = axes[0]
    names = [s["sample"].replace("C1000T1", "…") for s in summary]
    y = np.arange(len(summary))
    for i, s in enumerate(summary):
        col = CFAIL if s["trust"] == "rejected" else (CPASS if s["verdict"] == "PASS" else C2)
        m = s["margin"] if s["margin"] is not None else 0
        ax.barh(i, m, color=col, height=0.6)
        txt = f"{s['profile']}: {s['trust']}" + (f", {s['verdict']} on {s['headline']} ({m:+.2f} dB)" if s["verdict"] else " — quarantined (length check)")
        ax.text(0.1 if m >= 0 else 0.1, i, txt, va="center", fontsize=7.5, color=CK)
    ax.set_yticks(y), ax.set_yticklabels(names, fontsize=8), ax.invert_yaxis()
    ax.axvline(0, color=CK, lw=1)
    ax.set_xlabel("headline margin / dB"), ax.set_xlim(-8, 12)
    ax.set_title("incoming-inspection batch on the simulated lab", loc="left", fontsize=10)
    style(ax)
    ax = axes[1]
    ax.axis("off")
    wrong = next(s for s in summary if s["profile"] == "mislabelled")
    good = next(s for s in summary if s["profile"] == "good")
    names_c = ["grid", "passivity", "reciprocity", "connection", "trace_noise", "length", "il_rdc", "il_envelope"]
    bounds = {"grid": "1200 pts", "passivity": "≤ 0.02", "reciprocity": "≤ 0.10 dB", "connection": "≥ −60 dB", "trace_noise": "≤ 0.05 dB",
              "length": "15 m ± 12 %", "il_rdc": "0.8 … 4.0", "il_envelope": "0.05 … 0.6 dB/m"}
    fmt = {"grid": "{:.0f}", "passivity": "{:+.4f}", "reciprocity": "{:.3f}", "connection": "{:+.2f}", "trace_noise": "{:.4f}",
           "length": "{:.2f} m", "il_rdc": "{:.2f}", "il_envelope": "{:.3f}"}
    rows, colours = [], []
    for n in names_c:
        g, w = good["checks"]["pairA"][n], wrong["checks"]["pairA"][n]
        rows.append([n, bounds[n], fmt[n].format(g[1]), g[0], fmt[n].format(w[1]), w[0]])
        colours.append(["white", "white", "white", "#e6f4ea" if g[0] == "pass" else "#fdecea", "white", "#e6f4ea" if w[0] == "pass" else "#fdecea"])
    tb = ax.table(cellText=rows, colLabels=["check", "bound", "good 0041", "", "mislabelled", ""], cellColours=colours, loc="center", cellLoc="center",
                  colWidths=[0.17, 0.2, 0.15, 0.12, 0.15, 0.12])
    tb.auto_set_font_size(False), tb.set_fontsize(7.5), tb.scale(1.0, 1.35)
    ax.set_title("pairA validation checks: the mislabelled sample fails on length", loc="left", fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT / "batch.png", dpi=140)
    plt.close(fig)
    return summary


def fig_sweep(root):
    with Lab(LabConfig.load(root / "lab.toml")) as lab:
        eng = Engine(lab)
        spec = SweepSpec.load(root / "sweep_hot.toml")
        t0 = lab.clock.time()
        jobs, summary = eng.run_sweep(spec)
        rows = [l.split(",") for l in summary.read_text().splitlines()[1:]]
        logs = []
        for j in jobs:
            meta = json.loads((j.archive_dir / f"pairA_T{j.temperature_c:+04.0f}C.s4p.meta.json").read_text())
            logs.append((j.temperature_c, meta["environment"]["chamber_log"], meta["environment"]["chamber"]))
        elapsed = lab.clock.time() - t0
    (OUT / "sweep_summary.csv").write_text(summary.read_text())
    T = np.array([float(r[0]) for r in rows])
    il100 = np.array([float(r[7]) for r in rows])
    il600 = np.array([float(r[8]) for r in rows])
    rl = np.array([float(r[9]) for r in rows])
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 3.8))
    ax = axes[0]
    tt = 0.0
    for Tset, lg, ch in logs:
        t = np.array(lg["t"]) - lg["t"][0] + tt
        ax.plot(t / 3600, lg["air_c"], color=C1, lw=1)
        ax.plot(t / 3600, lg["setpoint_c"], color=CG, lw=0.8, ls="--")
        tt = t[-1] + 60
    ax.set_xlabel("elapsed time / h (simulated clock)"), ax.set_ylabel("chamber air / °C")
    ax.set_title(f"the sweep as the chamber saw it: {elapsed / 3600:.1f} h for 5 points", loc="left", fontsize=10)
    style(ax)
    ax = axes[1]
    ax.plot(T, il600 / il600[1], "o-", color=C2, label="IL(600 MHz) / IL(23 °C)")
    ax.plot(T, il100 / il100[1], "s-", color=C1, label="IL(100 MHz) / IL(23 °C)")
    ax.plot(T, rl / rl[1], "^-", color=C3, label="R_loop / R_loop(23 °C)")
    Tm = np.linspace(-40, 125, 100)
    ax.plot(Tm, (1 + 0.00393 * (Tm - 20)) / (1 + 0.00393 * 3), color=C3, lw=0.8, ls=":", label="copper 1 + 0.00393 (T − 20)")
    ax.set_xlabel("setpoint / °C"), ax.set_ylabel("ratio to 23 °C")
    ax.set_title("what came out: loss and loop resistance vs temperature", loc="left", fontsize=10)
    ax.legend(fontsize=7.5, frameon=False)
    style(ax)
    ax = axes[2]
    lab_err = [abs(float(r[1]) - float(r[0])) for r in rows]
    ax.bar(np.arange(len(T)), [ch["time_to_stable_s"] / 60 for _, _, ch in logs], color=C1, width=0.5, label="time to 'stable' / min")
    ax.bar(np.arange(len(T)), [ch["soak_s"] / 60 for _, _, ch in logs], bottom=[ch["time_to_stable_s"] / 60 for _, _, ch in logs], color=C4, width=0.5, label="sample soak / min")
    ax.set_xticks(np.arange(len(T))), ax.set_xticklabels([f"{t:+.0f} °C" for t in T], fontsize=8)
    ax.set_ylabel("minutes")
    ax.set_title("time budget per point (all of it archived with the data)", loc="left", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    style(ax)
    fig.tight_layout()
    fig.savefig(OUT / "sweep.png", dpi=140)
    plt.close(fig)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    info = init_demo(WORK)
    fig_state_machine()
    with Lab(LabConfig.load(WORK / "lab.toml")) as lab:
        fig_chamber(lab)
        fig_calibration(lab, info)
        fig_noise(lab)
    summary = fig_batch(WORK)
    fig_sweep(WORK)
    # a sample sidecar for the report
    jd = next(p for p in (WORK / "archive").rglob("pairA.s4p.meta.json") if "quarantine" not in str(p))
    meta = json.loads(jd.read_text())
    meta["environment"].pop("chamber_log", None)
    (OUT / "sidecar_example.json").write_text(json.dumps(meta, indent=1)[:20000])
    man = json.loads((jd.parent / "manifest.json").read_text())
    (OUT / "manifest_example.json").write_text(json.dumps({"job": {k: man["job"][k] for k in ("job_id", "state", "trust", "verdict", "headline_margin", "procedure_hash", "calibration_id")},
                                                          "files": man["files"], "sealed": man["sealed"]}, indent=1))
    shutil.rmtree(WORK, ignore_errors=True)
    print("done:", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
