"""`python -m mandate.bench.run`: run every configuration and write bench/results/{latest.json,results.md,raw.jsonl}."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .harness import CONFIGS, run_config
from .report import to_markdown
from .scenarios import build_scenarios
from .scorer import score_config

RESULTS = Path(__file__).resolve().parents[4] / "bench" / "results"


def run(out_dir: Path = RESULTS, quiet: bool = False) -> dict:
    scenarios = build_scenarios()
    doc: dict = {
        "n_scenarios": len(scenarios),
        "n_attacks": sum(s.attack for s in scenarios),
        "n_benign": sum(not s.attack for s in scenarios),
        "n_holdout": sum(s.holdout for s in scenarios),
        "configs": {},
    }
    raw: list[dict] = []
    for name, cfg in CONFIGS.items():
        rows = run_config(cfg, scenarios)
        raw += [{"config": name, **r} for r in rows]
        score = score_config(rows)
        doc["configs"][name] = {"description": cfg.description, "score": score}
        if not quiet:
            a = score["attacks"]["asr"]
            print(f"{name}: ASR {a['k']}/{a['n']} ({100 * a['rate']:.1f}%)  benign FP "
                  f"{score['benign']['false_positive']['k']}/{score['benign']['false_positive']['n']}  "
                  f"errors {score['errors']}")  # fmt: skip
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(doc, indent=1))
    (out_dir / "results.md").write_text(to_markdown(doc))
    (out_dir / "raw.jsonl").write_text("\n".join(json.dumps(r) for r in raw) + "\n")
    return doc


def main() -> int:
    doc = run()
    errors = sum(c["score"]["errors"] for c in doc["configs"].values())
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
