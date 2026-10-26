"""Kept for old links: the sandbox check now lives in `python -m mandate.sandbox_check`."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))
runpy.run_module("mandate.sandbox_check", run_name="__main__")
