"""
SCPI plumbing: transports, the instrument base class, and a mnemonic
normaliser shared by the drivers and the simulators.

Transports
----------
* :class:`SocketTransport` - raw TCP (HiSLIP-less "socket" mode, port 5025 on
  most Keysight/R&S/Anritsu instruments).  Line-oriented, newline terminated.
* :class:`VisaTransport` - pyvisa, when installed (``pip install labauto[visa]``).
* :class:`SimTransport` - talks to an in-process simulator object that
  implements ``handle(command: str) -> str | None``.  The driver code does not
  know which transport it has, so the simulators exercise the *exact* command
  strings that go to the real instrument.

Mnemonics
---------
SCPI accepts long and short forms (``SENSe1:FREQuency:STARt`` = ``SENS:FREQ:STAR``)
and optional numeric suffixes.  :func:`normalise` reduces a command header to
its canonical short form so a simulator can match on one spelling.  The
short-form rule (IEEE 488.2 / SCPI-99 §6.2): take the first four characters
of the long mnemonic; if the fourth is a vowel drop it.
"""
from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

__all__ = ["Transport", "SocketTransport", "VisaTransport", "SimTransport", "ScpiInstrument",
           "ScpiError", "InstrumentError", "normalise", "short_form", "split_commands"]

log = logging.getLogger("labauto.scpi")


class ScpiError(RuntimeError):
    """Transport or protocol failure."""


class InstrumentError(RuntimeError):
    """The instrument reported an error in its SYST:ERR? queue."""

    def __init__(self, errors: list[tuple[int, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{c}: {m}" for c, m in errors))


# ---------------------------------------------------------------- mnemonics
def short_form(mnemonic: str) -> str:
    m = mnemonic.strip().upper()
    if m.startswith("*"):
        return m
    digits = ""
    while m and m[-1].isdigit():
        digits = m[-1] + digits
        m = m[:-1]
    if len(m) > 4:
        m = m[:3] if m[3] in "AEIOU" else m[:4]
    elif len(m) == 4 and m[3] in "AEIOU" and not m.isalpha():
        pass
    return m


def normalise(header: str) -> str:
    """``SENSe1:FREQuency:STARt?`` -> ``SENS:FREQ:STAR?`` (numeric suffixes removed)."""
    h = header.strip()
    q = h.endswith("?")
    if q:
        h = h[:-1]
    h = h.lstrip(":")
    parts = [short_form(p) for p in h.split(":") if p]
    return ":".join(parts) + ("?" if q else "")


def split_commands(line: str) -> list[str]:
    """Split a program message at semicolons, respecting quotes."""
    out, cur, quote = [], [], None
    for ch in line:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == ";":
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur).strip())
    return [c for c in out if c]


# ---------------------------------------------------------------- transports
class Transport:
    address: str = ""

    def write(self, cmd: str) -> None:       # pragma: no cover - interface
        raise NotImplementedError

    def read(self) -> str:                   # pragma: no cover - interface
        raise NotImplementedError

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read()

    def close(self) -> None:
        pass


class SocketTransport(Transport):
    def __init__(self, host: str, port: int = 5025, timeout: float = 10.0, terminator: str = "\n"):
        self.address = f"TCPIP::{host}::{port}::SOCKET"
        self.term = terminator
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""

    def write(self, cmd: str) -> None:
        log.debug("-> %s", cmd)
        self.sock.sendall((cmd + self.term).encode("ascii"))

    def read(self) -> str:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ScpiError("connection closed by instrument")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        s = line.decode("ascii", errors="replace").rstrip("\r")
        log.debug("<- %s", s[:120])
        return s

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:  # pragma: no cover
            pass


class VisaTransport(Transport):  # pragma: no cover - needs hardware
    def __init__(self, resource: str, timeout_ms: int = 10000):
        try:
            import pyvisa
        except ImportError as e:
            raise ScpiError("pyvisa is not installed; pip install labauto[visa]") from e
        self.address = resource
        self.rm = pyvisa.ResourceManager("@py")
        self.inst = self.rm.open_resource(resource)
        self.inst.timeout = timeout_ms
        self.inst.read_termination = "\n"
        self.inst.write_termination = "\n"

    def write(self, cmd: str) -> None:
        self.inst.write(cmd)

    def read(self) -> str:
        return self.inst.read().rstrip()

    def close(self) -> None:
        self.inst.close()


class SimTransport(Transport):
    """Routes every command to ``sim.handle`` and keeps the replies queued like a real device."""

    def __init__(self, sim, address: str = "SIM"):
        self.sim = sim
        self.address = address
        self._replies: list[str] = []
        self.log: list[str] = []          # every command sent, for the tests

    def write(self, cmd: str) -> None:
        self.log.append(cmd)
        for c in split_commands(cmd):
            r = self.sim.handle(c)
            if r is not None:
                self._replies.append(r)

    def read(self) -> str:
        if not self._replies:
            raise ScpiError("read with no pending reply (query timeout)")
        return self._replies.pop(0)


def open_transport(address: str, sim=None, timeout: float = 10.0) -> Transport:
    """``sim`` -> SimTransport(sim); ``TCPIP::host::5025::SOCKET`` or ``host:port`` -> socket; else VISA."""
    a = address.strip()
    if a.lower() == "sim" or sim is not None:
        if sim is None:
            raise ScpiError("simulated address but no simulator object supplied")
        return SimTransport(sim, "SIM")
    if a.upper().startswith("TCPIP") and a.upper().endswith("SOCKET"):
        parts = a.split("::")
        host = parts[1]
        port = int(parts[2]) if len(parts) > 3 else 5025
        return SocketTransport(host, port, timeout)
    if ":" in a and "::" not in a:
        host, port = a.rsplit(":", 1)
        return SocketTransport(host, int(port), timeout)
    return VisaTransport(a, int(timeout * 1000))


# ---------------------------------------------------------------- instrument base
@dataclass
class Identity:
    manufacturer: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    raw: str = ""

    @classmethod
    def parse(cls, idn: str) -> "Identity":
        parts = [p.strip() for p in idn.split(",")]
        parts += [""] * (4 - len(parts))
        return cls(parts[0], parts[1], parts[2], parts[3], idn)

    def to_dict(self) -> dict:
        return {"manufacturer": self.manufacturer, "model": self.model, "serial": self.serial,
                "firmware": self.firmware, "idn": self.raw}


class ScpiInstrument:
    """Common behaviour: identification, error queue, synchronisation."""

    role: str = "instrument"

    def __init__(self, transport: Transport):
        self.t = transport
        self._identity: Identity | None = None

    @property
    def address(self) -> str:
        return self.t.address

    def write(self, cmd: str) -> None:
        self.t.write(cmd)

    def query(self, cmd: str) -> str:
        return self.t.query(cmd)

    def query_float(self, cmd: str) -> float:
        return float(self.query(cmd))

    @property
    def identity(self) -> Identity:
        if self._identity is None:
            self._identity = Identity.parse(self.query("*IDN?"))
        return self._identity

    def reset(self) -> None:
        self.write("*RST")
        self.write("*CLS")

    def opc(self) -> bool:
        return self.query("*OPC?").strip() in ("1", "+1")

    def errors(self) -> list[tuple[int, str]]:
        """Drain SYST:ERR? (up to 50 entries)."""
        out = []
        for _ in range(50):
            r = self.query("SYST:ERR?")
            code, _, msg = r.partition(",")
            try:
                code_i = int(code.strip().strip("+"))
            except ValueError:
                code_i = -1
            if code_i == 0:
                break
            out.append((code_i, msg.strip().strip('"')))
        return out

    def raise_errors(self) -> None:
        errs = self.errors()
        if errs:
            raise InstrumentError(errs)

    def close(self) -> None:
        self.t.close()
