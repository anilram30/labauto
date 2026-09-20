"""Barcodes and the registry, procedures and capability checks, calibration policy and verification."""
import time

import pytest

from labauto.barcode import (
    BarcodeError,
    SampleRegistry,
    make_internal_barcode,
    parse_barcode,
)
from labauto.calibration import CalRecord, evaluate_policy, verify_calibration
from labauto.drivers import Capabilities
from labauto.drivers.vna import SweepSettings
from labauto.procedure import (
    ProcedureError,
    list_procedures,
    load_procedure,
    parse_procedure,
)


# ---------------------------------------------------------------- barcodes
def test_internal_barcode_round_trip_and_check_character():
    bc = make_internal_barcode("C1000T1A", "L24071", "0042")
    b = parse_barcode(bc)
    assert (b.part, b.lot, b.serial, b.check_ok) == ("C1000T1A", "L24071", "0042", True)
    assert b.sample_id == "C1000T1A-L24071-0042"
    # a single substitution and a transposition are both caught
    bad = bc[:-2] + ("7" if bc[-2] != "7" else "8") + bc[-1]
    assert not parse_barcode(bad).check_ok
    body = bc[:-1]
    swapped = body[:-2] + body[-1] + body[-2] + bc[-1]
    if swapped != bc:
        assert not parse_barcode(swapped).check_ok


def test_gs1_labels():
    b = parse_barcode("(01)04012345678901(10)L24071(21)0042")
    assert (b.scheme, b.part, b.lot, b.serial, b.check_ok) == ("gs1", "04012345678901", "L24071", "0042", True)
    b2 = parse_barcode("\x1d0104012345678901" + "10L24071\x1d" + "210042")
    assert b2.sample_id == b.sample_id and b2.check_ok
    assert not parse_barcode("(01)04012345678903(10)L1(21)7").check_ok    # wrong GTIN check digit
    with pytest.raises(BarcodeError):
        parse_barcode("(01)04012345678901(10)L24071")                     # no serial
    with pytest.raises(BarcodeError):
        parse_barcode("hello world")


def test_registry_refuses_unknown_and_bad_scans(demo):
    root, info = demo
    reg = SampleRegistry.load(root / "registry.csv")
    bc, e = reg.resolve(info["barcodes"][0])
    assert e.sample_id == "C1000T1A-L24071-0041" and e.length_m == 15.0
    assert e.extra["sim_profile"] == "good"
    with pytest.raises(BarcodeError, match="not in the sample registry"):
        reg.resolve(make_internal_barcode("C1000T1A", "L24071", "9999"))
    with pytest.raises(BarcodeError, match="check character"):
        reg.resolve(info["barcodes"][0][:-1] + ("A" if info["barcodes"][0][-1] != "A" else "B"))


# ---------------------------------------------------------------- procedures
def test_builtin_procedures_parse_and_hash():
    table = list_procedures()
    assert {"sparam-1000base-t1-pair", "tempsweep-1000base-t1", "sparam-1000base-t1-two-pair-next", "sparam-generic-stp"} <= set(table)
    p = load_procedure("sparam-1000base-t1-pair")
    assert p.sweep.points == 1200 and p.files[0].port_map.startswith("A+near")
    assert len(p.hash) == 64
    q = parse_procedure(p.text.replace("points = 1200", "points = 1201"), "x")
    assert q.hash != p.hash and q.sweep.points == 1201
    assert p.aux[0].instrument == "dmm" and p.requires["dmm"].optional


def test_procedure_validation_errors():
    p = load_procedure("sparam-1000base-t1-pair")
    with pytest.raises(ProcedureError, match="requires.vna"):
        parse_procedure(p.text.replace("[requires.vna]", "[requires.vnx]"), "x")
    with pytest.raises(ProcedureError, match="port_map"):
        parse_procedure(p.text.replace('port_map = "A+near,A-near,A+far,A-far"', 'port_map = "A+near,A-near"'), "x")
    with pytest.raises(ProcedureError, match="chamber"):
        parse_procedure(p.text.replace('kind = "s-parameters"', 'kind = "temperature-sweep"'), "x")
    with pytest.raises(ProcedureError, match="not found"):
        load_procedure("no-such-procedure")


def test_requirements_against_capabilities():
    p = load_procedure("sparam-1000base-t1-pair")
    req = p.requires["vna"]
    assert req.check(Capabilities("vna", ports=4, fmin_hz=9e3, fmax_hz=8.5e9, features={"s-parameters", "calsets"})) == []
    probs = req.check(Capabilities("vna", ports=2, fmin_hz=9e3, fmax_hz=300e6, features={"s-parameters"}))
    assert len(probs) == 3 and any("ports" in x for x in probs) and any("MHz" in x for x in probs) and any("calsets" in x for x in probs)
    ch = load_procedure("tempsweep-1000base-t1").requires["chamber"]
    assert ch.check(Capabilities("chamber", tmin_c=-20, tmax_c=150, features={"temperature"})) == ["chamber: needs -40 C, reaches -20 C"]


# ---------------------------------------------------------------- calibration
def _rec(age_days, temp=23.0, verified=None):
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc) - timedelta(days=age_days)
    r = CalRecord("CAL-X", "SIM0001", "SOLT4_X", "SOLT", [1, 2, 3, 4], t.isoformat(timespec="seconds"), temp, "op")
    if verified is not None:
        from labauto.calibration import VerificationResult
        r.verifications.append(VerificationResult((datetime.now(timezone.utc) - timedelta(hours=verified)).isoformat(timespec="seconds"),
                                                  "CHECK-ATT20", "pass", [[1, 2]], 0.02, 0.5, 40, 0.1, 3, 30))
    return r


def test_calibration_policy():
    pol = {"max_age_days": 7, "max_delta_t_k": 3.0, "verify": True, "verify_every_hours": 8}
    now = time.time()
    assert evaluate_policy(None, pol, now, 23).status == "invalid"
    d = evaluate_policy(_rec(2), pol, now, 23)
    assert d.status == "needs-verification" and "never verified" in d.reasons[0]
    assert evaluate_policy(_rec(2, verified=1), pol, now, 23).status == "valid"
    assert evaluate_policy(_rec(2, verified=9), pol, now, 23).status == "needs-verification"
    d = evaluate_policy(_rec(9, verified=1), pol, now, 23)
    assert d.status == "invalid" and "days old" in d.reasons[0]
    d = evaluate_policy(_rec(2, verified=1), pol, now, 27.5)
    assert d.status == "invalid" and "ambient differs" in d.reasons[0]
    assert evaluate_policy(_rec(2), dict(pol, verify=False), now, 23).status == "valid"


def test_verification_passes_on_a_good_cal_and_fails_on_a_drifted_one(lab):
    l, info = lab
    vna, bench, store = l.instrument("vna"), l.bench, l.calstore
    rec = store.get(info["calibration"])
    pol = load_procedure("sparam-1000base-t1-pair").calibration
    vna.configure(SweepSettings(1e6, 600e6, 601, 1e3, 0.0))
    vna.select_calset(rec.instrument_calset)
    vr = verify_calibration(vna, rec, store, pol, l.clock, lambda dev, ports: bench.connect(dev))
    assert vr.status == "pass" and vr.max_dev_db < 0.1 and vr.min_rl_db > 30
    assert store.get(rec.id).last_verification().status == "pass"
    # now the room warms up by 12 K: residual tracking error ~0.16 dB peak -> fails the 0.1 dB tolerance
    bench.ambient_c = 35.0
    vr2 = verify_calibration(vna, rec, store, pol, l.clock, lambda dev, ports: bench.connect(dev))
    assert vr2.status == "fail" and vr2.max_dev_db > 0.1
    assert evaluate_policy(store.get(rec.id), pol, l.clock.time(), 23.0).status == "needs-verification"
