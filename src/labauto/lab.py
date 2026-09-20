"""
The laboratory configuration: instruments by role, registry, calibration
store, archive, database, extra procedure and limit directories.

``lab.toml``::

    [lab]
    id = "LAB1"
    name = "HF cable laboratory, site 1"
    archive = "archive"
    db = "lab.sqlite"
    results_db = "results.sqlite"       # cablecheck's database
    registry = "registry.csv"
    calibration_dir = "calibration"
    procedures_dir = "procedures"       # optional, in addition to the built-in ones
    limits_dir = "limits"               # optional, extra cablecheck limit files

    [instruments.vna]
    driver = "vna.scpi"                 # or vna.sim
    address = "TCPIP::192.168.10.20::5025::SOCKET"
    dialect = "keysight-pna"
    capabilities = { ports = 4, fmax_hz = 8.5e9, features = ["s-parameters", "calsets"] }

    [instruments.chamber]
    driver = "chamber.sim"
    address = "sim"

    [simulation]                         # only read by the *.sim drivers
    ambient_c = 23.0
    fixture = false

Paths are relative to the directory of ``lab.toml``.  The bench for
simulated drivers is created once and shared, so the simulated chamber
heats the sample that the simulated analyser measures.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .archive import Archive
from .barcode import SampleRegistry
from .calibration import CalStore
from .clock import Clock, SimClock, WallClock
from .drivers import make_instrument
from .labdb import LabDB

__all__ = ["LabConfig", "Lab"]


@dataclass
class LabConfig:
    id: str
    name: str
    root: Path
    archive: Path
    db: Path
    results_db: Path
    registry: Path
    calibration_dir: Path
    procedures_dirs: list[Path]
    limits_dirs: list[Path]
    instruments: dict[str, dict]
    simulation: dict = field(default_factory=dict)
    text: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "LabConfig":
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        doc = tomllib.loads(text)
        lab = doc.get("lab", {})
        root = path.parent

        def rel(p, default):
            return (root / lab.get(p, default)).resolve()
        pdirs = [rel("procedures_dir", "procedures")] if "procedures_dir" in lab else []
        ldirs = [rel("limits_dir", "limits")] if "limits_dir" in lab else []
        return cls(lab.get("id", "LAB"), lab.get("name", ""), root, rel("archive", "archive"), rel("db", "lab.sqlite"),
                   rel("results_db", "results.sqlite"), rel("registry", "registry.csv"), rel("calibration_dir", "calibration"),
                   pdirs, ldirs, doc.get("instruments", {}), doc.get("simulation", {}), text)

    @property
    def is_simulated(self) -> bool:
        return any(str(i.get("driver", "")).endswith(".sim") or i.get("driver") == "scanner.queue" for i in self.instruments.values())


class Lab:
    """Open handles: instruments, registry, calibration store, archive, database."""

    def __init__(self, cfg: LabConfig, clock: Clock | None = None, scanner_codes: list[str] | None = None):
        self.cfg = cfg
        self.clock = clock or (SimClock() if cfg.is_simulated else WallClock())
        self.registry = SampleRegistry.load(cfg.registry) if cfg.registry.exists() else SampleRegistry([], str(cfg.registry))
        self.calstore = CalStore(cfg.calibration_dir)
        self.archive = Archive(cfg.archive)
        self.db = LabDB(cfg.db)
        self.bench = None
        self.instruments: dict[str, object] = {}
        if any(str(i.get("driver", "")).endswith(".sim") for i in cfg.instruments.values()):
            from .sim.bench import SimBench
            sim = cfg.simulation
            self.bench = SimBench(self.registry, float(sim.get("ambient_c", 23.0)), bool(sim.get("fixture", False)), int(sim.get("seed", 0)),
                                  str(sim.get("unused_ports", "terminated")))
        for role, spec in cfg.instruments.items():
            kw = {k: v for k, v in spec.items() if k not in ("driver", "address")}
            if spec.get("driver") == "scanner.queue":
                kw["codes"] = list(scanner_codes or [])
            inst = make_instrument(spec["driver"], spec.get("address", "sim"), sim_bench=self.bench, clock=self.clock, **kw)
            inst.role = role
            self.instruments[role] = inst
        if self.bench is not None and "vna" in self.instruments and hasattr(self.instruments["vna"], "sim"):
            # the simulated analyser holds the cal sets that the calibration records refer to
            vsim = self.instruments["vna"].sim
            for rec in self.calstore.records():
                if rec.instrument_serial == vsim.idn.split(",")[2]:
                    vsim.add_calset(rec.instrument_calset, rec.t_cal, rec.temperature_c, rec.ports, rec.type,
                                    float(cfg.simulation.get("cal_quality", 1.0)))

    def instrument(self, role: str):
        return self.instruments.get(role)

    def close(self):
        for inst in self.instruments.values():
            try:
                inst.close()
            except Exception:  # pragma: no cover
                pass
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
