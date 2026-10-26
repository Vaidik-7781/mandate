"""Generate spec/vectors/*.json from the reference implementation. tests/test_vectors.py replays them, so the
vectors and the code can never drift apart.

    python scripts/gen_vectors.py
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from mandate.agents.dispute import decide  # noqa: E402
from mandate.liability.matrix import MATRIX_VERSION, Facts, attribute  # noqa: E402
from mandate.settings import Settings  # noqa: E402

OUT = ROOT / "spec" / "vectors"
BOOLS = ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8")


def liability() -> dict:
    cases = []
    # every combination of P1..P8, with P9 and the merchant-controlled refinement on the two rules they touch
    for combo in itertools.product([True, False], repeat=len(BOOLS)):
        base = dict(zip(BOOLS, combo, strict=True))
        variants = [{}]
        if not base["p4"]:
            variants = [
                {"injected_text_merchant_controlled": False},
                {"injected_text_merchant_controlled": True},
            ]
        elif not base["p3"]:
            variants = [{"p9": False}, {"p9": True}]
        for extra in variants:
            facts = {**base, **extra}
            a = attribute(Facts(**facts))
            cases.append(
                {"facts": facts, "rule": a.rule_id, "split": a.split, "defend_merchant": a.defend_merchant}
            )
    return {"matrix_version": MATRIX_VERSION, "note": "first matching rule R1..R8 wins", "cases": cases}


def disputes() -> dict:
    cfg = Settings()
    cases = []
    for amount, stage, facts, comp, taint, prot in [
        (4500, "CHARGEBACK", Facts(), 1.0, [], False),
        (4500, "INQUIRY", Facts(p6=False), 1.0, [], False),
        (4500, "CHARGEBACK", Facts(p6=False), 1.0, [], False),
        (4500, "INQUIRY", Facts(p3=False, p9=True), 1.0, [], False),
        (4500, "CHARGEBACK", Facts(), 0.6, [], False),
        (15000, "CHARGEBACK", Facts(), 1.0, [], False),
        (4500, "CHARGEBACK", Facts(), 1.0, ["suspected_instruction"], False),
        (4500, "CHARGEBACK", Facts(), 1.0, [], True),
        (4500, "CHARGEBACK", Facts(), 0.85, [], False),
    ]:
        d = decide(amount_minor=amount, stage=stage, attribution=attribute(facts), completeness=comp,
                   taint_flags=taint, protection_applied=prot, settings=cfg)  # fmt: skip
        cases.append({"amount_minor": amount, "stage": stage, "facts": facts.as_dict(), "completeness": comp,
                      "taint_flags": taint, "protection_applied": prot, "action": d.action})  # fmt: skip
    return {"note": "dispute policy: human review on taint, refund when merchant+platform fault >= 50%, "
                    "fight only when expected value is positive, human above the money gate",
            "dispute_human_threshold_minor": cfg.dispute_human_threshold_minor, "cases": cases}  # fmt: skip


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, doc in (("liability-matrix-v0.1", liability()), ("dispute-policy-v0.1", disputes())):
        (OUT / f"{name}.json").write_text(json.dumps(doc, indent=1) + "\n")
        print(f"wrote {name}.json ({len(doc['cases'])} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
