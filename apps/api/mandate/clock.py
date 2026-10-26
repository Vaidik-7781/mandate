"""Injectable clock so expiry, velocity and revocation-race logic is testable and replayable."""

from __future__ import annotations

import time
from collections.abc import Callable

_now: Callable[[], float] = time.time


def now() -> int:
    """Current unix time in whole seconds."""
    return int(_now())


def now_f() -> float:
    return float(_now())


def set_clock(fn: Callable[[], float] | None) -> None:
    """Install a fake clock (tests, replay). Pass None to restore the real one."""
    global _now
    _now = fn or time.time


class FrozenClock:
    """Context manager / helper: `with FrozenClock(1_800_000_000) as c: c.advance(60)`."""

    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def __enter__(self) -> FrozenClock:
        set_clock(self)
        return self

    def __exit__(self, *exc: object) -> None:
        set_clock(None)
