"""Run Mandate-Bench: python scripts/run_bench.py  (writes bench/results/{latest.json,results.md,raw.jsonl})."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from mandate.bench.run import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
