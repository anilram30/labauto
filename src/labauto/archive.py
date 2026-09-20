"""
The raw-data archive: write once, hash everything, replay later.

Layout
------
    <root>/<year>/<job_id>/
        <file>.s4p               the raw instrument data, exactly as measured (fixture NOT removed)
        <file>.meta.json         the sidecar
        aux.json                 auxiliary measurements
        journal.jsonl            every state transition of the job with timestamps
        procedure.toml           the procedure text that ran (hash in the manifest)
        analysis/                what the analysers produced (cablecheck result, report, ...)
        manifest.json            sha256 of every file above + the job summary; written last

Reproducibility contract
------------------------
``verify`` recomputes every hash; ``replay`` re-runs the analysers on the
archived raw files with the archived procedure and compares verdict and
headline margin with what was recorded.  A replay that disagrees means a
software change altered the engineering result - which is exactly what
one wants to know six months later, and why the archive keeps the raw
data rather than the processed result as the source of truth.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .metadata import _default, sha256_file

__all__ = ["Archive", "ArchiveError"]


class ArchiveError(RuntimeError):
    pass


class Archive:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str, t: float | None = None) -> Path:
        year = datetime.fromtimestamp(t, tz=timezone.utc).year if t else datetime.now(timezone.utc).year
        return self.root / str(year) / job_id

    def seal(self, job_dir: Path, summary: dict) -> dict:
        """Write the manifest with a hash of every file in the job directory (except the manifest)."""
        files = {}
        for p in sorted(job_dir.rglob("*")):
            if p.is_file() and p.name != "manifest.json":
                files[str(p.relative_to(job_dir)).replace("\\", "/")] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
        manifest = {"job": summary, "files": files, "sealed": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, default=_default), encoding="utf-8")
        return manifest

    def verify(self, job_dir: Path) -> dict:
        m = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        bad, missing, extra = [], [], []
        for rel, info in m["files"].items():
            p = job_dir / rel
            if not p.exists():
                missing.append(rel)
            elif sha256_file(p) != info["sha256"]:
                bad.append(rel)
        known = set(m["files"])
        for p in job_dir.rglob("*"):
            if p.is_file() and p.name != "manifest.json":
                rel = str(p.relative_to(job_dir)).replace("\\", "/")
                if rel not in known:
                    extra.append(rel)
        return {"ok": not (bad or missing), "modified": bad, "missing": missing, "unlisted": extra, "files": len(m["files"])}

    def jobs(self) -> list[Path]:
        return sorted(p.parent for p in self.root.rglob("manifest.json"))

    def replay(self, job_dir: Path, extra_limit_dirs=None) -> dict:
        """Re-run the cablecheck analysis from the archived raw files and compare with the sealed result."""
        from .analysis import run_cablecheck
        m = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        summ = m["job"]
        integrity = self.verify(job_dir)
        if not integrity["ok"]:
            return {"ok": False, "integrity": integrity, "reason": "archive integrity failure"}
        if summ.get("trust") == "rejected" or not summ.get("analysis", {}).get("cablecheck"):
            return {"ok": True, "integrity": integrity, "skipped": "quarantined job: raw data only, no analysis to reproduce",
                    "verdict_archived": None, "verdict_replayed": None}
        metas = {p.name[:-len(".meta.json")]: json.loads(p.read_text(encoding="utf-8")) for p in job_dir.glob("*.meta.json")}
        files = []
        for stem, meta in metas.items():
            files.append({"path": str(job_dir / meta["measurement"]["file"]), "port_map": meta["measurement"]["port_map"],
                          "quantities": meta["measurement"].get("quantities"), "fixture": meta.get("fixture", {"method": "none"})})
        sample = summ["sample"]
        tmp = job_dir / "_replay"
        if tmp.exists():
            shutil.rmtree(tmp)
        res = run_cablecheck(sample, files, summ["procedure"]["cable_type"], tmp, extra_limit_dirs, write_report=False)
        shutil.rmtree(tmp, ignore_errors=True)
        old = summ.get("analysis", {}).get("cablecheck", {})
        same_verdict = res["verdict"] == old.get("verdict")
        hm_new, hm_old = res.get("headline_margin"), old.get("headline_margin")
        same_margin = (hm_new is None and hm_old is None) or (hm_new is not None and hm_old is not None and abs(hm_new - hm_old) < 1e-6)
        return {"ok": same_verdict and same_margin, "integrity": integrity, "verdict_archived": old.get("verdict"),
                "verdict_replayed": res["verdict"], "headline_archived": hm_old, "headline_replayed": hm_new,
                "software_archived": summ.get("software", {}).get("cablecheck"), "software_now": res["software"]}
