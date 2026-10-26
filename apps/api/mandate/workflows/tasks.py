"""Task runner abstraction. Background work (webhook processing, evidence assembly) is submitted by name.

`LocalRunner` runs tasks inline (default, deterministic, used in replay mode and tests) or on a thread pool.
`render_app.py` registers the same functions with Render Workflows when that SDK is installed; the app never
depends on it (see docs/architecture.md: the adapter is unverified against a live Render account).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol

log = logging.getLogger("mandate.tasks")


class TaskRunner(Protocol):
    def register(self, name: str, fn: Callable[..., Any]) -> None: ...

    def submit(self, name: str, *args: Any) -> Any: ...


class LocalRunner:
    def __init__(self, inline: bool = True, workers: int = 4) -> None:
        self.inline = inline
        self._fns: dict[str, Callable[..., Any]] = {}
        self._pool = None if inline else ThreadPoolExecutor(max_workers=workers)
        self.failures: list[tuple[str, str]] = []

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self._fns[name] = fn

    def _run(self, name: str, args: tuple) -> Any:
        try:
            return self._fns[name](*args)
        except Exception as exc:  # noqa: BLE001 - a failed task must never break the webhook ack
            log.exception("task %s failed", name)
            self.failures.append((name, f"{type(exc).__name__}: {exc}"))
            return None

    def submit(self, name: str, *args: Any) -> Any:
        if name not in self._fns:
            raise KeyError(f"unknown task {name}")
        if self._pool is None:
            return self._run(name, args)
        fut: Future = self._pool.submit(self._run, name, args)
        return fut
