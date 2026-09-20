"""
The laboratory database (SQLite): the index over the archive.

    jobs            one row per job: sample, procedure, trust level, verdict, archive path, timings
    files           one row per archived raw file with its sha256 (joins to jobs)
    calibrations    every calibration decision taken by the engine (policy + verification)
    instrument_log  identity + state snapshot of every instrument at every job

cablecheck keeps its own results database (traces and margins); this one
answers the laboratory questions: what was measured when, on which
calibration, with what trust, and where the raw data lives.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

__all__ = ["LabDB"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    campaign TEXT, batch TEXT,
    sample_id TEXT NOT NULL, lot TEXT, part_number TEXT, cable_type TEXT,
    procedure_id TEXT NOT NULL, procedure_version TEXT, procedure_hash TEXT,
    operator TEXT, site TEXT,
    started TEXT, finished TEXT, state TEXT,
    trust TEXT, verdict TEXT, headline TEXT, headline_margin REAL,
    temperature_c REAL,
    calibration_id TEXT,
    archive_dir TEXT,
    attempts INTEGER,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    name TEXT, path TEXT, sha256 TEXT, nports INTEGER, port_map TEXT, attempt INTEGER, trust TEXT
);
CREATE TABLE IF NOT EXISTS calibrations (
    id INTEGER PRIMARY KEY,
    job_id TEXT, time TEXT, instrument_serial TEXT, calibration_id TEXT,
    decision TEXT, reasons TEXT, verification_status TEXT, max_dev_db REAL, max_dev_deg REAL, min_rl_db REAL
);
CREATE TABLE IF NOT EXISTS instrument_log (
    id INTEGER PRIMARY KEY,
    job_id TEXT, role TEXT, serial TEXT, model TEXT, state_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_sample ON jobs(sample_id);
CREATE INDEX IF NOT EXISTS ix_jobs_proc ON jobs(procedure_id);
"""


class LabDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_SCHEMA)

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def upsert_job(self, s: dict) -> None:
        cols = ["job_id", "campaign", "batch", "sample_id", "lot", "part_number", "cable_type", "procedure_id",
                "procedure_version", "procedure_hash", "operator", "site", "started", "finished", "state", "trust",
                "verdict", "headline", "headline_margin", "temperature_c", "calibration_id", "archive_dir", "attempts", "summary_json"]
        row = {c: s.get(c) for c in cols}
        row["summary_json"] = json.dumps(s, default=str)
        self.con.execute(f"INSERT OR REPLACE INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [row[c] for c in cols])
        self.con.commit()

    def add_files(self, job_id: str, files: list[dict]) -> None:
        self.con.execute("DELETE FROM files WHERE job_id = ?", (job_id,))
        self.con.executemany("INSERT INTO files (job_id,name,path,sha256,nports,port_map,attempt,trust) VALUES (?,?,?,?,?,?,?,?)",
                             [(job_id, f.get("name"), f.get("path"), f.get("sha256"), f.get("nports"), f.get("port_map"),
                               f.get("attempt"), f.get("trust")) for f in files])
        self.con.commit()

    def add_calibration(self, job_id: str, time: str, serial: str, decision: dict, verification: dict | None) -> None:
        v = verification or {}
        self.con.execute("INSERT INTO calibrations (job_id,time,instrument_serial,calibration_id,decision,reasons,"
                         "verification_status,max_dev_db,max_dev_deg,min_rl_db) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (job_id, time, serial, decision.get("id"), decision.get("status"), json.dumps(decision.get("reasons", [])),
                          v.get("status"), v.get("max_dev_db"), v.get("max_dev_deg"), v.get("min_rl_db")))
        self.con.commit()

    def add_instrument_state(self, job_id: str, role: str, state: dict) -> None:
        ident = state.get("identity", {})
        self.con.execute("INSERT INTO instrument_log (job_id,role,serial,model,state_json) VALUES (?,?,?,?,?)",
                         (job_id, role, ident.get("serial"), ident.get("model"), json.dumps(state, default=str)))
        self.con.commit()

    def jobs(self, sample_id: str | None = None, procedure_id: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        if sample_id:
            q += " AND sample_id = ?"; args.append(sample_id)
        if procedure_id:
            q += " AND procedure_id = ?"; args.append(procedure_id)
        q += " ORDER BY started DESC LIMIT ?"; args.append(limit)
        return list(self.con.execute(q, args))

    def counts(self) -> dict:
        out = {}
        for k, q in {"jobs": "SELECT COUNT(*) FROM jobs", "files": "SELECT COUNT(*) FROM files",
                     "trusted": "SELECT COUNT(*) FROM jobs WHERE trust='trusted'",
                     "rejected": "SELECT COUNT(*) FROM jobs WHERE trust='rejected'"}.items():
            out[k] = self.con.execute(q).fetchone()[0]
        return out
