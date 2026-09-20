"""
Barcode scanner sources.

Most USB scanners are keyboard wedges (the scan arrives as typed text plus
Enter), so the console scanner is simply ``input()``.  Serial scanners are
a line-oriented socket/serial transport.  The queue scanner feeds a fixed
list (batch files and tests).
"""
from __future__ import annotations

from . import Capabilities, register

__all__ = ["Scanner", "QueueScanner", "ConsoleScanner"]


class Scanner:
    role = "scanner"
    capabilities = Capabilities("scanner", features={"barcode"})

    def scan(self, prompt: str = "scan sample") -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    def state(self) -> dict:
        return {"driver": getattr(self, "driver_name", "scanner"), "capabilities": self.capabilities.to_dict()}

    def close(self):
        pass


@register("scanner.queue")
class QueueScanner(Scanner):
    """Returns the queued codes in order, then None."""

    def __init__(self, codes: list[str]):
        self.codes = list(codes)
        self.history: list[str] = []

    @classmethod
    def open(cls, address: str, *, codes=None, **_):
        return cls(codes or [])

    def scan(self, prompt: str = "scan sample") -> str | None:
        if not self.codes:
            return None
        c = self.codes.pop(0)
        self.history.append(c)
        return c


@register("scanner.console")
class ConsoleScanner(Scanner):  # pragma: no cover - interactive
    @classmethod
    def open(cls, address: str, **_):
        return cls()

    def scan(self, prompt: str = "scan sample") -> str | None:
        try:
            s = input(f"{prompt} (empty to finish): ").strip()
        except EOFError:
            return None
        return s or None
