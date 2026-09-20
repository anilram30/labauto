"""
Sample identification: barcodes and the sample registry.

Two label schemes are understood:

* **GS1-128** element strings, e.g. ``(01)04012345678901(10)L24071(21)0042``
  or with the FNC1 separator ``\\x1d``: AI 01 = GTIN (part), 10 = lot,
  21 = serial.  The GTIN check digit (mod-10, weights 3/1) is verified.
* **Internal** ``PART-LOT-SERIALc`` where ``c`` is a mod-36 check character
  (Code-39 character weights, alphanumeric result) over everything before
  it, e.g. ``C1000T1A-L24071-0042K``.

A scan is *parsed* (what the label says), then *resolved* against the
registry (what the lab knows about that sample: cable type, nominal length,
part number, lot).  The registry is a CSV so the production system can
export it; a sample that is not in the registry is refused, because a
measurement without a resolved identity cannot be archived.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Barcode", "parse_barcode", "BarcodeError", "SampleRegistry", "RegistryEntry", "make_internal_barcode"]

_C39 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%"
_ALNUM = _C39[:36]


class BarcodeError(ValueError):
    pass


@dataclass
class Barcode:
    raw: str
    scheme: str                 # gs1 | internal
    part: str
    lot: str
    serial: str
    check_ok: bool = True

    @property
    def sample_id(self) -> str:
        return f"{self.part}-{self.lot}-{self.serial}"

    def to_dict(self) -> dict:
        return {"raw": self.raw, "scheme": self.scheme, "part": self.part, "lot": self.lot,
                "serial": self.serial, "check_ok": self.check_ok}


def _gtin_check_ok(gtin: str) -> bool:
    if not gtin.isdigit() or len(gtin) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in gtin]
    body, chk = digits[:-1], digits[-1]
    s = sum(d * (3 if (len(body) - i) % 2 == 1 else 1) for i, d in enumerate(body))
    return (10 - s % 10) % 10 == chk


def mod36_check(s: str) -> str:
    """Weighted sum of the Code-39 values (weight = position, so transpositions are caught), mod 36."""
    total = sum((i + 1) * _C39.index(c) for i, c in enumerate(s.upper()))
    return _ALNUM[total % 36]


def make_internal_barcode(part: str, lot: str, serial: str) -> str:
    body = f"{part}-{lot}-{serial}".upper()
    return body + mod36_check(body)


def parse_barcode(raw: str) -> Barcode:
    s = raw.strip(" \t\r\n")
    if not s:
        raise BarcodeError("empty scan")
    # GS1 with parentheses or FNC1 (\x1d) separators, or a bare element string starting with AI 01
    if s.startswith("(") or "\x1d" in s or (s.startswith("01") and len(s) > 16 and s[2:16].isdigit()):
        ais = _parse_gs1(s)
        gtin = ais.get("01", "")
        lot, ser = ais.get("10", ""), ais.get("21", "")
        if not gtin or not lot or not ser:
            raise BarcodeError(f"GS1 label without GTIN/lot/serial: {raw!r}")
        return Barcode(raw, "gs1", gtin, lot, ser, _gtin_check_ok(gtin))
    parts = s.upper().split("-")
    if len(parts) != 3:
        raise BarcodeError(f"unrecognised label {raw!r} (expected GS1 or PART-LOT-SERIALc)")
    part, lot, ser_c = parts
    if len(ser_c) < 2 or any(c not in _C39 for c in s.upper()):
        raise BarcodeError(f"unrecognised label {raw!r}")
    serial, chk = ser_c[:-1], ser_c[-1]
    ok = mod36_check(f"{part}-{lot}-{serial}") == chk
    return Barcode(raw, "internal", part, lot, serial, ok)


def _parse_gs1(s: str) -> dict[str, str]:
    out = {}
    if s.startswith("("):
        i = 0
        while i < len(s):
            if s[i] != "(":
                raise BarcodeError(f"bad GS1 element string {s!r}")
            j = s.index(")", i)
            ai = s[i + 1:j]
            k = s.find("(", j)
            val = s[j + 1:] if k < 0 else s[j + 1:k]
            out[ai] = val
            i = len(s) if k < 0 else k
        return out
    # FNC1 separated (or raw numeric): fixed-length AI 01 (14 digits), variable 10 and 21 terminated by \x1d
    s = s.lstrip("\x1d")
    while s:
        ai = s[:2]
        s = s[2:]
        if ai == "01":
            out[ai], s = s[:14], s[14:]
        elif ai in ("10", "21"):
            k = s.find("\x1d")
            if k < 0:
                out[ai], s = s, ""
            else:
                out[ai], s = s[:k], s[k + 1:]
        else:
            raise BarcodeError(f"unsupported GS1 application identifier {ai}")
        s = s.lstrip("\x1d")
    return out


# ---------------------------------------------------------------- registry
@dataclass
class RegistryEntry:
    barcode: str
    sample_id: str
    part_number: str
    lot: str
    cable_type: str
    length_m: float
    design: str = ""
    notes: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"barcode": self.barcode, "sample_id": self.sample_id, "part_number": self.part_number, "lot": self.lot,
             "cable_type": self.cable_type, "length_m": self.length_m, "design": self.design, "notes": self.notes}
        d.update(self.extra)
        return d


class SampleRegistry:
    REQUIRED = ("barcode", "sample_id", "part_number", "lot", "cable_type", "length_m")

    def __init__(self, entries: list[RegistryEntry], source: str = ""):
        self.entries = {e.barcode.upper(): e for e in entries}
        self.by_id = {e.sample_id: e for e in entries}
        self.source = source

    @classmethod
    def load(cls, path: str | Path) -> "SampleRegistry":
        path = Path(path)
        entries = []
        with path.open(newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            missing = [c for c in cls.REQUIRED if c not in (rd.fieldnames or [])]
            if missing:
                raise ValueError(f"{path}: registry lacks columns {missing}")
            for row in rd:
                extra = {k: v for k, v in row.items() if k not in cls.REQUIRED + ("design", "notes")}
                entries.append(RegistryEntry(row["barcode"].strip(), row["sample_id"].strip(), row["part_number"].strip(),
                                             row["lot"].strip(), row["cable_type"].strip(), float(row["length_m"]),
                                             row.get("design", "") or "", row.get("notes", "") or "", extra))
        return cls(entries, str(path))

    def resolve(self, scan: str | Barcode) -> tuple[Barcode, RegistryEntry]:
        bc = parse_barcode(scan) if isinstance(scan, str) else scan
        if not bc.check_ok:
            raise BarcodeError(f"check character of {bc.raw!r} is wrong - rescan")
        e = self.entries.get(bc.raw.strip().upper())
        if e is None:
            e = self.by_id.get(bc.sample_id)
        if e is None:
            raise BarcodeError(f"{bc.raw!r} ({bc.sample_id}) is not in the sample registry {self.source}")
        return bc, e

    def __len__(self):
        return len(self.entries)
