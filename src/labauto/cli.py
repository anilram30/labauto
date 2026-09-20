"""
Command line::

    labauto init-demo DIR                         a simulated laboratory to try everything on
    labauto run  --lab lab.toml batch.toml        run a batch (barcodes from the file or the scanner)
    labauto sweep --lab lab.toml sweep.toml       climate-chamber temperature sweep
    labauto job  --lab lab.toml BARCODE PROC      one sample, one procedure
    labauto cal  --lab lab.toml list|verify|policy
    labauto procedures [--lab lab.toml]           list / show procedures and check them against the lab
    labauto instruments --lab lab.toml            identify every instrument and read its state
    labauto archive --lab lab.toml verify|replay|list
    labauto scan  --lab lab.toml                  test the scanner and the registry
    labauto idn ADDRESS                           talk to a real instrument

``--dry-run`` walks every gate (identity, capabilities, calibration policy,
fixture, environment) and reports the plan without measuring.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__


def _lab(args, codes=None):
    from .lab import Lab, LabConfig
    cfg = LabConfig.load(args.lab)
    return Lab(cfg, scanner_codes=codes)


def _setup_logging(verbose: bool, logfile: Path | None = None):
    handlers = [logging.StreamHandler(sys.stderr)]
    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=handlers)


def _print_jobs(jobs):
    print(f"{'job':<60} {'state':<9} {'trust':<9} {'verdict':<8} {'headline':<28} margin")
    for j in jobs:
        cc = j.analysis.get("cablecheck", {})
        hm = cc.get("headline_margin")
        print(f"{j.job_id[:60]:<60} {j.state:<9} {j.trust or '-':<9} {cc.get('verdict') or '-':<8} "
              f"{(cc.get('headline') or '-')[:28]:<28} {'' if hm is None else f'{hm:+.2f} dB'}"
              + (f"   [{j.reason[:70]}]" if j.state in ('ABORTED', 'ERROR', 'REJECTED', 'PLANNED') else ""))


def cmd_init_demo(args):
    from .demo import init_demo
    info = init_demo(args.dir, cal_age_days=args.cal_age, ambient_c=args.ambient, verified=args.verified)
    print(json.dumps(info, indent=1))
    return 0


def cmd_demo(args):
    """Write a simulated laboratory and run its incoming-inspection batch through the
    engine: gates, measurement, validation, trust decision, sealed archive and database."""
    from .demo import init_demo
    root = Path(args.dir)
    print(f"==> writing a simulated laboratory into {root}")
    info = init_demo(root, cal_age_days=args.cal_age, ambient_c=args.ambient, verified=args.verified)
    print(json.dumps(info, indent=1))
    batch = root / "batch_incoming.toml"
    if not batch.exists():
        print(f"no batch file at {batch}", file=sys.stderr)
        return 1
    print(f"\n==> running batch {batch.name}")
    args.lab, args.verbose, args.scan, args.dry_run, args.resume = str(root / "lab.toml"), False, None, False, False
    args.batch = str(batch)
    rc = cmd_run(args)
    print(f"\nsealed archive in {root / 'archive'}, database at {root / 'lab.sqlite'}")
    return rc


def cmd_run(args):
    from .engine import BatchSpec, Engine
    spec = BatchSpec.load(args.batch)
    _setup_logging(args.verbose, Path(args.lab).parent / "logs" / f"batch_{spec.id}.log")
    with _lab(args, codes=args.scan) as lab:
        eng = Engine(lab, dry_run=args.dry_run)
        jobs = eng.run_batch(spec, resume=args.resume)
    _print_jobs(jobs)
    return 0 if all(j.state in ("DONE", "PLANNED") for j in jobs) else 1


def cmd_sweep(args):
    from .engine import Engine, SweepSpec
    spec = SweepSpec.load(args.sweep)
    _setup_logging(args.verbose, Path(args.lab).parent / "logs" / f"sweep_{spec.id}.log")
    with _lab(args) as lab:
        eng = Engine(lab, dry_run=args.dry_run)
        jobs, summary = eng.run_sweep(spec)
    _print_jobs(jobs)
    print(f"summary: {summary}")
    print(summary.read_text())
    return 0 if all(j.state == "DONE" for j in jobs) else 1


def cmd_job(args):
    from .engine import Engine
    _setup_logging(args.verbose)
    with _lab(args) as lab:
        eng = Engine(lab, dry_run=args.dry_run)
        job = eng.new_job(args.barcode, args.procedure, args.operator, temperature_c=args.temperature)
        eng.run_job(job)
    _print_jobs([job])
    if args.json:
        print(json.dumps(job.summary(), indent=1, default=str))
    return 0 if job.state == "DONE" else 1


def cmd_cal(args):
    from .calibration import evaluate_policy, verify_calibration
    from .procedure import load_procedure
    _setup_logging(args.verbose)
    with _lab(args) as lab:
        vna = lab.instrument("vna")
        if args.what == "list":
            for r in lab.calstore.records():
                lv = r.last_verification()
                print(f"{r.id:<28} {r.instrument_serial:<10} {r.type:<5} ports {r.ports} {r.date} {r.temperature_c:>5.1f} C  "
                      f"verified: {lv.time + ' ' + lv.status if lv else 'never'}")
            if vna is not None:
                print("cal sets on the analyser:", vna.calsets())
            return 0
        proc = load_procedure(args.procedure, lab.cfg.procedures_dirs)
        pol = proc.calibration
        serial = vna.identity.serial
        ambient = float(lab.bench.ambient_c) if lab.bench is not None else args.ambient
        cands = lab.calstore.candidates(serial, pol.get("type", "SOLT"), [int(p) for p in pol.get("ports", [1, 2, 3, 4])])
        if not cands:
            print("no calibration record for", serial)
            return 1
        rec = cands[0]
        d = evaluate_policy(rec, pol, lab.clock.time(), ambient)
        print(f"policy for {rec.id} under {proc.id}: {d.status}  age {d.age_days:.2f} d, dT {d.delta_t_k:.1f} K  {'; '.join(d.reasons)}")
        if args.what == "verify":
            from .engine import AutoPrompter, ConsolePrompter
            pr = AutoPrompter(lab.bench) if lab.bench is not None else ConsolePrompter()
            vna.configure(proc.sweep)
            vna.select_calset(rec.instrument_calset)

            def connect(dev, ports):
                pr.connect(dev, ports, f"connect {dev} to ports {ports}")
            vr = verify_calibration(vna, rec, lab.calstore, pol, lab.clock, connect, lab.archive.root / "verification")
            print(json.dumps(vr.to_dict(), indent=1))
            return 0 if vr.status == "pass" else 1
    return 0


def cmd_procedures(args):
    from .procedure import list_procedures, load_procedure
    extra = []
    lab = None
    if args.lab:
        lab = _lab(args)
        extra = lab.cfg.procedures_dirs
    table = list_procedures(extra)
    if args.show:
        p = load_procedure(args.show, extra)
        print(p.text)
        print(f"# sha256 {p.hash}")
        return 0
    for pid, path in table.items():
        p = load_procedure(pid, extra)
        line = f"{pid:<40} v{p.version:<5} {p.kind:<18} {p.cable_type:<28} {len(p.files)} file(s)  {p.hash[:12]}"
        if lab is not None:
            probs = []
            for role, req in p.requires.items():
                inst = lab.instrument(role)
                if inst is None:
                    if not req.optional:
                        probs.append(f"no {role}")
                else:
                    probs.extend(req.check(inst.capabilities))
            line += "   " + ("OK on this lab" if not probs else "NOT RUNNABLE: " + "; ".join(probs))
            est = p.estimate_seconds(lab.instrument("vna").capabilities.ports if lab.instrument("vna") else 4)
            line += f"  (~{est / 60:.1f} min/sample)"
        print(line)
    if lab is not None:
        lab.close()
    return 0


def cmd_instruments(args):
    _setup_logging(args.verbose)
    with _lab(args) as lab:
        for role, inst in lab.instruments.items():
            st = inst.state() if hasattr(inst, "state") else {}
            print(f"[{role}] {getattr(inst, 'driver_name', '?')}")
            print(json.dumps(st, indent=1, default=str))
    return 0


def cmd_archive(args):
    with _lab(args) as lab:
        jobs = lab.archive.jobs()
        if args.what == "list":
            for jd in jobs:
                m = json.loads((jd / "manifest.json").read_text())
                j = m["job"]
                print(f"{jd.relative_to(lab.archive.root)}  {j.get('state')}  trust={j.get('trust')}  verdict={j.get('verdict')}  files={len(m['files'])}")
            print(f"{len(jobs)} sealed job(s); database: {lab.db.counts()}")
            return 0
        rc = 0
        sel = [jd for jd in jobs if not args.job or args.job in str(jd)]
        for jd in sel:
            if args.what == "verify":
                r = lab.archive.verify(jd)
                print(f"{jd.name}: {'OK' if r['ok'] else 'FAILED'} {r}")
            else:
                r = lab.archive.replay(jd, [str(d) for d in lab.cfg.limits_dirs] or None)
                if r.get("skipped"):
                    print(f"{jd.name}: SKIPPED ({r['skipped']})")
                    continue
                print(f"{jd.name}: {'REPRODUCED' if r['ok'] else 'DIFFERS'}  archived {r.get('verdict_archived')} / {r.get('headline_archived')}"
                      f"  replayed {r.get('verdict_replayed')} / {r.get('headline_replayed')}  (cablecheck {r.get('software_archived')} -> {r.get('software_now')})")
            rc |= 0 if r["ok"] else 1
    return rc


def cmd_scan(args):
    from .barcode import parse_barcode
    with _lab(args, codes=args.codes) as lab:
        sc = lab.instrument("scanner")
        while True:
            s = sc.scan() if sc is not None else (args.codes.pop(0) if args.codes else None)
            if s is None:
                break
            try:
                bc, e = lab.registry.resolve(s)
                print(f"{s:<32} {bc.scheme:<8} check={'ok' if bc.check_ok else 'BAD'}  -> {e.sample_id} {e.cable_type} {e.length_m} m lot {e.lot}")
            except Exception as ex:
                print(f"{s:<32} REFUSED: {ex}")
                try:
                    print("   parsed:", parse_barcode(s))
                except Exception:
                    pass
    return 0


def cmd_idn(args):  # pragma: no cover - hardware
    from .scpi import ScpiInstrument, open_transport
    inst = ScpiInstrument(open_transport(args.address, timeout=args.timeout))
    print(inst.identity)
    print("errors:", inst.errors())
    inst.close()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="labauto", description="Automated cable measurement laboratory")
    p.add_argument("--version", action="version", version=f"labauto {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, lab_required=True):
        sp.add_argument("--lab", required=lab_required, help="lab.toml")
        sp.add_argument("-v", "--verbose", action="store_true")
    d = sub.add_parser("init-demo", help="write a simulated laboratory")
    d.add_argument("dir"), d.add_argument("--cal-age", type=float, default=2.0), d.add_argument("--ambient", type=float, default=23.0)
    d.add_argument("--verified", action="store_true", help="calibration record already carries a passed verification")
    d.set_defaults(func=cmd_init_demo)
    dm = sub.add_parser("demo", help="simulated laboratory plus a full batch, end to end")
    dm.add_argument("dir", nargs="?", default="demo_lab")
    dm.add_argument("--cal-age", type=float, default=2.0), dm.add_argument("--ambient", type=float, default=23.0)
    dm.add_argument("--verified", action="store_true")
    dm.set_defaults(func=cmd_demo)

    r = sub.add_parser("run", help="run a batch")
    common(r); r.add_argument("batch"); r.add_argument("--dry-run", action="store_true"); r.add_argument("--resume", action="store_true")
    r.add_argument("--scan", nargs="*", help="barcodes to feed a queue scanner (simulation)")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("sweep", help="temperature sweep")
    common(s); s.add_argument("sweep"); s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sweep)
    j = sub.add_parser("job", help="one sample, one procedure")
    common(j); j.add_argument("barcode"); j.add_argument("procedure"); j.add_argument("--operator", default="operator")
    j.add_argument("--temperature", type=float); j.add_argument("--dry-run", action="store_true"); j.add_argument("--json", action="store_true")
    j.set_defaults(func=cmd_job)
    c = sub.add_parser("cal", help="calibration records, policy, verification")
    common(c); c.add_argument("what", choices=["list", "policy", "verify"]); c.add_argument("--procedure", default="sparam-1000base-t1-pair")
    c.add_argument("--ambient", type=float, default=23.0)
    c.set_defaults(func=cmd_cal)
    pr = sub.add_parser("procedures", help="list procedures")
    common(pr, lab_required=False); pr.add_argument("--show")
    pr.set_defaults(func=cmd_procedures)
    i = sub.add_parser("instruments", help="identify and read back every instrument")
    common(i); i.set_defaults(func=cmd_instruments)
    a = sub.add_parser("archive", help="list, verify, replay")
    common(a); a.add_argument("what", choices=["list", "verify", "replay"]); a.add_argument("--job")
    a.set_defaults(func=cmd_archive)
    sc = sub.add_parser("scan", help="scanner + registry test")
    common(sc); sc.add_argument("codes", nargs="*")
    sc.set_defaults(func=cmd_scan)
    n = sub.add_parser("idn", help="*IDN? a real instrument")
    n.add_argument("address"); n.add_argument("--timeout", type=float, default=5.0)
    n.set_defaults(func=cmd_idn)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
