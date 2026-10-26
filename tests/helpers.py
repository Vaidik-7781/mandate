"""Shared builders for flow and API tests."""

from __future__ import annotations

from mandate import clock
from mandate.db import Database
from mandate.runtime import Runtime
from mandate.settings import Settings

T0 = 1_790_000_000
PROMPT = "Buy me black running shoes under $120, max 2 pairs, only from RunFast or TrailCo, valid for 7 days"


def build(**over) -> Runtime:
    clock.set_clock(clock.FrozenClock(T0))
    cfg = Settings(
        database_url="sqlite://", replay_mode=True, secret_seed="flow-seed", _env_file=None, **over
    )
    return Runtime.build(cfg, db=Database("sqlite://"))
