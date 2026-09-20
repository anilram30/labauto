"""
``labauto init-demo DIR``: a complete simulated laboratory to run the examples against.

Writes lab.toml (simulated analyser, chamber, DMM, queue scanner), a sample
registry with a mix of good, lossy, marginal, defective and mislabelled
samples, a calibration record with its check-standard reference file, and
batch / sweep specifications.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from cablecheck.io.touchstone import write_touchstone

from .barcode import make_internal_barcode
from .calibration import CalRecord, CalStore
from .sim.bench import check_standard_network

__all__ = ["init_demo", "DEMO_SAMPLES"]

# (part, lot, serial, cable_type, length_m, design, profile)
DEMO_SAMPLES = [
    ("C1000T1A", "L24071", "0041", "1000base-t1-link-segment", 15.0, "D100-PE-035", "good"),
    ("C1000T1A", "L24071", "0042", "1000base-t1-link-segment", 15.0, "D100-PE-035", "good"),
    ("C1000T1A", "L24071", "0043", "1000base-t1-link-segment", 15.0, "D100-PE-035", "marginal"),
    ("C1000T1A", "L24072", "0007", "1000base-t1-link-segment", 15.0, "D100-PE-035", "lossy"),
    ("C1000T1A", "L24072", "0008", "1000base-t1-link-segment", 15.0, "D100-PE-035", "defect"),
    ("C1000T1A", "L24072", "0009", "1000base-t1-link-segment", 15.0, "D100-PE-035", "mislabelled"),
    ("C1000T1B", "L24080", "0101", "1000base-t1-link-segment", 10.0, "D100-PP-040", "good"),
    ("CSTPGEN", "L24090", "0003", "lab-generic-100ohm-stp", 20.0, "STP-generic", "good"),
]


def init_demo(root: str | Path, cal_age_days: float = 2.0, cal_temperature_c: float = 23.0, ambient_c: float = 23.0,
              now: float | None = None, verified: bool = False) -> dict:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc) if now else datetime.now(timezone.utc)
    # registry
    rows = ["barcode,sample_id,part_number,lot,cable_type,length_m,design,notes,sim_profile,sim_pairs"]
    codes = []
    for part, lot, ser, ct, L, design, prof in DEMO_SAMPLES:
        bc = make_internal_barcode(part, lot, ser)
        codes.append(bc)
        pairs = 2 if part == "C1000T1B" else 1
        rows.append(f"{bc},{part}-{lot}-{ser},{part},{lot},{ct},{L},{design},,{prof},{pairs}")
    (root / "registry.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    # calibration record + reference
    store = CalStore(root / "calibration")
    f = np.linspace(1e6, 1e9, 1000)
    write_touchstone(check_standard_network(f), store.reference_path("check_att20.s2p"),
                     comments=["certified reference of the check standard CHECK-ATT20 (demo)"])
    t_cal = now_dt - timedelta(days=cal_age_days)
    rec = CalRecord(id=f"CAL-{t_cal.strftime('%Y%m%d')}-SOLT4", instrument_serial="SIM0001",
                    instrument_calset=f"SOLT4_{t_cal.strftime('%Y%m%d')}", type="SOLT", ports=[1, 2, 3, 4],
                    date=t_cal.isoformat(timespec="seconds"), temperature_c=cal_temperature_c, operator="sanil",
                    kit_serial="85052D-MY12345", kit_due=(now_dt + timedelta(days=200)).isoformat(timespec="seconds"),
                    notes="demo record: 4-port SOLT with the mechanical kit, torque wrench, 3.5 mm")
    if verified:
        from .calibration import VerificationResult
        rec.verifications.append(VerificationResult(now_dt.isoformat(timespec="seconds"), "CHECK-ATT20", "pass", [[1, 2], [3, 4]],
                                                    0.03, 0.8, 41.0, 0.1, 3.0, 30.0))
    store.save(rec)
    # lab config
    (root / "lab.toml").write_text(f'''[lab]
id = "LAB1"
name = "HF cable laboratory, site 1 (simulated)"
archive = "archive"
db = "lab.sqlite"
results_db = "results.sqlite"
registry = "registry.csv"
calibration_dir = "calibration"

[instruments.vna]
driver = "vna.sim"
address = "sim"
dialect = "keysight-pna"

[instruments.chamber]
driver = "chamber.sim"
address = "sim"

[instruments.dmm]
driver = "dmm.sim"
address = "sim"

[instruments.scanner]
driver = "scanner.queue"

[simulation]
ambient_c = {ambient_c}
fixture = false
seed = 0
cal_quality = 1.0
''', encoding="utf-8")
    (root / "batch_incoming.toml").write_text(f'''[batch]
id = "B-2026-38-01"
procedure = "sparam-1000base-t1-pair"
operator = "sanil"
site = "LAB1"
campaign = "incoming-inspection-wk38"
samples = {json.dumps(codes[:6])}
notes = "demo batch: six samples of two lots"
''', encoding="utf-8")
    (root / "batch_next.toml").write_text(f'''[batch]
id = "B-2026-38-02"
procedure = "sparam-1000base-t1-two-pair-next"
operator = "sanil"
site = "LAB1"
campaign = "incoming-inspection-wk38"
samples = {json.dumps(codes[6:7])}
''', encoding="utf-8")
    (root / "sweep_hot.toml").write_text(f'''[sweep]
id = "S-2026-38-01"
procedure = "tempsweep-1000base-t1"
sample = "{codes[1]}"
operator = "sanil"
site = "LAB1"
campaign = "derating-D100-PE-035"
temperatures = [-40, 23, 85, 105, 125]
''', encoding="utf-8")
    return {"root": str(root), "barcodes": codes, "calibration": rec.id}
