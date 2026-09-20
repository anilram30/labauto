"""
Metadata: the record that makes a measurement defensible.

Every measured file gets a sidecar ``<file>.meta.json`` with the sections
below.  The procedure lists which dotted paths are *mandatory*; the engine
refuses to archive a record with a mandatory field missing or empty, so a
file cannot enter the archive without, for example, a calibration id and a
verification status.

    sample        identity from the barcode and the registry
    procedure     id, version, sha256 of the procedure text
    instrument    per role: *IDN? fields, address, dialect, full read-back state
    calibration   record id, date, type, ports, policy decision, verification result
    fixture       method and files used (with hashes)
    environment   ambient, humidity, chamber log and stability, sample temperature and how it was inferred
    measurement   file name, sha256, ports, port map, sweep settings as read back, attempt number, timing
    validation    every check with value and bound; the trust decision
    aux           auxiliary measurements (loop resistance ...)
    software      labauto, cablecheck, numpy, python, platform, git commit of labauto if available
    operator      who ran it, and the site
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

import numpy as np

from . import __version__

__all__ = ["software_versions", "sha256_file", "check_mandatory", "MetadataError", "write_sidecar"]


class MetadataError(ValueError):
    pass


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_commit() -> str | None:
    try:
        root = Path(__file__).resolve().parents[2]
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or None
    except Exception:  # pragma: no cover
        return None


def software_versions() -> dict:
    import cablecheck
    d = {"labauto": __version__, "cablecheck": cablecheck.__version__, "numpy": np.__version__,
         "python": sys.version.split()[0], "platform": platform.platform(), "labauto_git": _git_commit()}
    for opt in ("zprofile", "cableanalytics"):
        try:
            mod = __import__(opt)
            d[opt] = getattr(mod, "__version__", "?")
        except ImportError:
            d[opt] = None
    return d


def _get(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def check_mandatory(meta: dict, mandatory: list[str]) -> list[str]:
    """Return the mandatory dotted paths that are missing or empty."""
    missing = []
    for path in mandatory:
        v = _get(meta, path)
        if v is None or v == "" or v == [] or v == {}:
            missing.append(path)
    return missing


def write_sidecar(meta: dict, path: Path) -> Path:
    path.write_text(json.dumps(meta, indent=1, default=_default), encoding="utf-8")
    return path


def _default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, set):
        return sorted(o)
    if hasattr(o, "to_dict"):
        return o.to_dict()
    return str(o)
