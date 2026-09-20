"""
Time as a dependency.

Everything that waits (chamber soak, sweep completion, retry back-off) asks
a :class:`Clock` rather than :mod:`time`, so the same engine runs against
the real laboratory in real time and against the simulators in simulated
time - a full climate-chamber sweep that takes six hours on the bench takes
a fraction of a second in the test-suite, with identical code paths.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timezone

__all__ = ["Clock", "WallClock", "SimClock"]


class Clock:
    def time(self) -> float:            # pragma: no cover - interface
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def iso(self) -> str:
        return datetime.fromtimestamp(self.time(), tz=timezone.utc).isoformat(timespec="seconds")


class WallClock(Clock):
    def time(self) -> float:
        return _time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            _time.sleep(seconds)


class SimClock(Clock):
    """A clock that only advances when someone sleeps on it."""

    def __init__(self, start: float | None = None):
        self._t = float(start if start is not None else _time.time())
        self.listeners: list = []       # objects with .advance_to(t) called on every sleep

    def time(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        target = self._t + seconds
        # step in small increments so listeners (chamber thermal models) integrate smoothly
        step = max(min(seconds / 50.0, 5.0), 1e-3)
        while self._t < target - 1e-9:
            self._t = min(self._t + step, target)
            for l in self.listeners:
                l.advance_to(self._t)
