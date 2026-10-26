import json
from pathlib import Path

import pytest
from mandate.agents.dispute import decide
from mandate.liability.matrix import Facts, attribute
from mandate.settings import Settings

V = Path(__file__).resolve().parents[1] / "spec" / "vectors"


def _load(name):
    return json.loads((V / name).read_text())["cases"]


@pytest.mark.parametrize("case", _load("liability-matrix-v0.1.json"))
def test_liability_vectors(case):
    a = attribute(Facts(**case["facts"]))
    assert (a.rule_id, a.split, a.defend_merchant) == (case["rule"], case["split"], case["defend_merchant"])


@pytest.mark.parametrize("case", _load("dispute-policy-v0.1.json"))
def test_dispute_vectors(case):
    d = decide(amount_minor=case["amount_minor"], stage=case["stage"],
               attribution=attribute(Facts(**case["facts"])), completeness=case["completeness"],
               taint_flags=case["taint_flags"], protection_applied=case["protection_applied"], settings=Settings())  # fmt: skip
    assert d.action == case["action"]
