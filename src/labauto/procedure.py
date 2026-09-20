"""
Test procedures: what a measurement *is*.

A procedure is a TOML document that says which instruments it needs and
with what capabilities, how the analyser is to be set, what a valid
calibration is, which files are to be measured with which port map, which
auxiliary measurements cross-check them, what the data must satisfy to be
trusted, which metadata is mandatory, and which analysers run afterwards.
The engine executes procedures; it contains no procedure-specific code.

The SHA-256 of the procedure text is part of every record it produces, so
"which version of the procedure produced this" is never a question.
"""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .drivers import Capabilities
from .drivers.vna import SweepSettings

__all__ = ["Procedure", "load_procedure", "list_procedures", "ProcedureError", "Requirement", "FileSpec", "AuxSpec"]


class ProcedureError(ValueError):
    pass


@dataclass
class Requirement:
    role: str
    optional: bool = False
    ports: int = 0
    fmin_hz: float = 0.0
    fmax_hz: float = 0.0
    tmin_c: float | None = None
    tmax_c: float | None = None
    features: set[str] = field(default_factory=set)

    def check(self, caps: Capabilities) -> list[str]:
        """Return the list of unmet constraints (empty = satisfied)."""
        bad = []
        if self.ports and caps.ports < self.ports:
            bad.append(f"{self.role}: needs {self.ports} ports, has {caps.ports}")
        if self.fmax_hz and caps.fmax_hz and caps.fmax_hz < self.fmax_hz:
            bad.append(f"{self.role}: needs {self.fmax_hz / 1e6:.0f} MHz, reaches {caps.fmax_hz / 1e6:.0f} MHz")
        if self.fmin_hz and caps.fmin_hz and caps.fmin_hz > self.fmin_hz:
            bad.append(f"{self.role}: needs {self.fmin_hz / 1e6:.3g} MHz start, starts at {caps.fmin_hz / 1e6:.3g} MHz")
        if self.tmin_c is not None and caps.tmin_c > self.tmin_c:
            bad.append(f"{self.role}: needs {self.tmin_c} C, reaches {caps.tmin_c} C")
        if self.tmax_c is not None and caps.tmax_c < self.tmax_c:
            bad.append(f"{self.role}: needs {self.tmax_c} C, reaches {caps.tmax_c} C")
        missing = self.features - set(caps.features)
        if missing:
            bad.append(f"{self.role}: lacks features {sorted(missing)}")
        return bad


@dataclass
class FileSpec:
    name: str
    ports: list[int]
    port_map: str
    prompt: str = ""
    quantities: list[str] | None = None
    fixture: dict = field(default_factory=dict)


@dataclass
class AuxSpec:
    name: str
    instrument: str
    method: str
    prompt: str = ""
    args: dict = field(default_factory=dict)


@dataclass
class Procedure:
    id: str
    version: str
    title: str
    kind: str                       # s-parameters | temperature-sweep
    cable_type: str
    description: str
    requires: dict[str, Requirement]
    sweep: SweepSettings | None
    calibration: dict
    fixture: dict
    environment: dict
    files: list[FileSpec]
    aux: list[AuxSpec]
    checks: dict
    metadata: dict
    downstream: dict
    temperatures: dict
    text: str
    path: str
    hash: str

    @property
    def vna_ports_needed(self) -> int:
        return max((max(f.ports) for f in self.files), default=0)

    def to_dict(self) -> dict:
        return {"id": self.id, "version": self.version, "title": self.title, "kind": self.kind,
                "cable_type": self.cable_type, "hash": self.hash, "path": self.path}

    def estimate_seconds(self, nports_instrument: int) -> float:
        if self.sweep is None:
            return 0.0
        per = self.sweep.sweep_time_estimate(nports_instrument)
        n = len(self.files) * (1 + self.checks.get("max_repeats", 0) * 0.2)
        return per * n + 30.0 * len(self.files) + 20.0 * len(self.aux)


def _req(role: str, d: dict) -> Requirement:
    return Requirement(role, bool(d.get("optional", False)), int(d.get("ports", 0)), float(d.get("fmin_hz", 0)),
                       float(d.get("fmax_hz", 0)), d.get("tmin_c"), d.get("tmax_c"), set(d.get("features", [])))


def parse_procedure(text: str, path: str = "<text>") -> Procedure:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProcedureError(f"{path}: {e}") from e
    p = doc.get("procedure")
    if not p:
        raise ProcedureError(f"{path}: missing [procedure] table")
    for k in ("id", "version", "kind", "cable_type"):
        if k not in p:
            raise ProcedureError(f"{path}: [procedure] lacks {k!r}")
    if p["kind"] not in ("s-parameters", "temperature-sweep"):
        raise ProcedureError(f"{path}: unknown kind {p['kind']!r}")
    req = {role: _req(role, d) for role, d in doc.get("requires", {}).items()}
    if "vna" not in req:
        raise ProcedureError(f"{path}: every procedure needs a [requires.vna] table")
    sw = doc.get("sweep")
    sweep = SweepSettings(float(sw["start_hz"]), float(sw["stop_hz"]), int(sw["points"]), float(sw["ifbw_hz"]),
                          float(sw.get("power_dbm", 0.0)), int(sw.get("averages", 1)), sw.get("sweep_type", "LIN")) if sw else None
    if sweep is None:
        raise ProcedureError(f"{path}: missing [sweep]")
    files = [FileSpec(f["name"], [int(x) for x in f["ports"]], f["port_map"], f.get("prompt", ""), f.get("quantities"),
                      f.get("fixture", {})) for f in doc.get("files", [])]
    if not files:
        raise ProcedureError(f"{path}: no [[files]] to measure")
    for f in files:
        if len(f.port_map.split(",")) != len(f.ports):
            raise ProcedureError(f"{path}: file {f.name}: port_map has {len(f.port_map.split(','))} entries for {len(f.ports)} ports")
    aux = [AuxSpec(a["name"], a["instrument"], a["method"], a.get("prompt", ""), a.get("args", {})) for a in doc.get("aux", [])]
    for a in aux:
        if a.instrument not in req:
            raise ProcedureError(f"{path}: aux {a.name} uses instrument role {a.instrument!r} not in [requires]")
    if p["kind"] == "temperature-sweep":
        if "chamber" not in req or req["chamber"].optional:
            raise ProcedureError(f"{path}: a temperature sweep needs a non-optional chamber")
        if not doc.get("temperatures", {}).get("values"):
            raise ProcedureError(f"{path}: a temperature sweep needs [temperatures].values")
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    checks = {"frequency_grid": True, "passivity_max": 0.02, "reciprocity_max_db": 0.1, "trace_noise_max_db": 0.05,
              "connection_min_db": -60.0, "length_tol_pct": 12.0, "nvp": 0.68, "il_rdc_ratio": [0.8, 4.0],
              "il_per_m_100mhz_db": [0.05, 0.6], "max_repeats": 2, "repeat_on": ["fail"], "quarantine_on": ["fail"]}
    checks.update(doc.get("checks", {}))
    return Procedure(p["id"], str(p["version"]), p.get("title", p["id"]), p["kind"], p["cable_type"],
                     p.get("description", ""), req, sweep, doc.get("calibration", {}), doc.get("fixture", {"method": "none"}),
                     doc.get("environment", {}), files, aux, checks, doc.get("metadata", {"mandatory": []}),
                     doc.get("downstream", {"analysers": ["cablecheck"]}), doc.get("temperatures", {}), text, path, h)


def _builtin_dir() -> Path:
    return Path(str(resources.files("labauto").joinpath("procedures")))


def list_procedures(extra_dirs: list[str | Path] | None = None) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for d in [_builtin_dir()] + [Path(x) for x in (extra_dirs or [])]:
        if d.is_dir():
            for p in sorted(d.glob("*.toml")):
                try:
                    pr = parse_procedure(p.read_text(encoding="utf-8"), str(p))
                except ProcedureError:
                    continue
                out[pr.id] = p
    return out


def load_procedure(name_or_path: str | Path, extra_dirs: list[str | Path] | None = None) -> Procedure:
    p = Path(name_or_path)
    if p.suffix == ".toml" and p.exists():
        return parse_procedure(p.read_text(encoding="utf-8"), str(p))
    table = list_procedures(extra_dirs)
    if str(name_or_path) in table:
        path = table[str(name_or_path)]
        return parse_procedure(path.read_text(encoding="utf-8"), str(path))
    raise ProcedureError(f"procedure {name_or_path!r} not found; known: {sorted(table)}")
