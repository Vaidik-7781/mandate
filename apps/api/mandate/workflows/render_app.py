"""Optional Render Workflows adapter.

UNVERIFIED: written against Render's public description of Workflows, never run against a live Render account
(the SDK is not a dependency; `pip install render_sdk` is optional). The app never needs it: `LocalRunner` is
the default and is what every test and the replay demo use. If the SDK is missing or its surface differs, this
module degrades to `LocalRunner` and says so, instead of failing the service.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import Any

from .tasks import LocalRunner, TaskRunner

log = logging.getLogger("mandate.render")


class RenderRunner(LocalRunner):
    """Registers each task with Render Workflows when the SDK exposes a `task` decorator; runs locally otherwise."""

    def __init__(self) -> None:
        super().__init__(inline=False)
        self.sdk_available = False
        self._decorator: Callable[..., Any] | None = None
        try:
            mod = importlib.import_module("render_sdk")
            deco = getattr(mod, "task", None)
            if callable(deco):
                self._decorator = deco
                self.sdk_available = True
            else:
                log.warning("render_sdk has no `task` decorator; running tasks locally")
        except ImportError:
            log.info("render_sdk not installed; running tasks locally")
        self.registered: list[str] = []

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        super().register(name, fn)
        if self._decorator is not None:
            try:
                self._decorator(name=name)(fn)
                self.registered.append(name)
            except Exception:  # noqa: BLE001 - an SDK mismatch must not take the service down
                log.warning("could not register %s with Render Workflows; it still runs locally", name)


def build_runner(kind: str = "local") -> TaskRunner:
    return RenderRunner() if kind == "render" else LocalRunner(inline=False)
