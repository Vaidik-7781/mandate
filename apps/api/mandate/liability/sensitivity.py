"""How much do the Liability Matrix's numbers matter?

The weights in matrix.py (70/30, 30/70, ...) are design choices; no network rule or court assigns them. This module
asks the useful question instead: if they were different, would the *dispute decision* change? The dispute policy
only asks one thing of the split (does the user carry 50% or more?), so most weight changes cannot alter what
the agent does. The experiments below show which can.

`python -m mandate.liability.sensitivity` regenerates spec/liability-sensitivity.md and bench/results/liability-sensitivity.json.
"""

from __future__ import annotations

import itertools
import json
import random
from dataclasses import dataclass
from pathlib import Path

from .matrix import Facts, attribute

Split = tuple[int, int, int]  # user, agent platform, merchant
REFUND_AT = 50  # dispute policy: accept or offer a refund when merchant + platform fault >= 50

BASELINE: dict[str, Split] = {
    "R1": (0, 100, 0), "R2": (0, 100, 0), "R3": (0, 0, 100), "R4": (0, 70, 30),
    "R5": (0, 100, 0), "R5m": (0, 70, 30), "R6": (0, 100, 0), "R6c": (30, 70, 0),
    "R7": (0, 0, 100), "R8": (100, 0, 0),
}  # fmt: skip
FACT_KEYS = [f"p{i}" for i in range(1, 10)] + ["injected_text_merchant_controlled"]


def all_facts() -> list[Facts]:
    return [
        Facts(**dict(zip(FACT_KEYS, bits, strict=True)))
        for bits in itertools.product([True, False], repeat=10)
    ]


def split_for(w: dict[str, Split], f: Facts) -> tuple[str, Split]:
    """Same rule order as matrix.attribute, with the weights injected."""
    if not f.p7:
        return "R1", w["R1"]
    if not (f.p1 and f.p2):
        return "R2", w["R2"]
    if not f.p5:
        return "R3", w["R3"]
    if not f.p8:
        return "R4", w["R4"]
    if not f.p4:
        return "R5", w["R5m"] if f.injected_text_merchant_controlled else w["R5"]
    if not f.p3:
        return "R6", w["R6c"] if f.p9 else w["R6"]
    if not f.p6:
        return "R7", w["R7"]
    return "R8", w["R8"]


def action(s: Split) -> str:
    return "refund" if s[1] + s[2] >= REFUND_AT else "defend"


def _rand_split(rng: random.Random) -> Split:
    a, b = sorted((rng.randint(0, 100), rng.randint(0, 100)))
    return (a, b - a, 100 - b)


@dataclass
class Result:
    cases: int
    baseline_matches_production: bool
    random_draws: int
    flip_rate_all_rules: float  # share of (draw, case) pairs whose action changed
    per_rule_flip_rate: dict[str, float]  # perturb one rule at a time with random splits
    platform_merchant_swaps_that_flip: int
    platform_share_range: tuple[float, float]  # mean platform share across cases under the draws (min, max)
    flip_thresholds: dict[str, str]

    def to_dict(self) -> dict:
        return {**self.__dict__, "platform_share_range": list(self.platform_share_range)}


def analyse(draws: int = 2000, seed: int = 20261110) -> Result:
    facts = all_facts()
    base = {f: split_for(BASELINE, f) for f in facts}
    prod_ok = all(attribute(f).split == {"user": s[1][0], "agent_platform": s[1][1], "merchant": s[1][2]}
                  and attribute(f).rule_id == s[0] for f, s in base.items())  # fmt: skip
    base_act = {f: action(s[1]) for f, s in base.items()}
    rng = random.Random(seed)

    flips = total = 0
    plat: list[float] = []
    for _ in range(draws):
        w = {k: _rand_split(rng) for k in BASELINE}
        plat.append(sum(split_for(w, f)[1][1] for f in facts) / len(facts) / 100)
        for f in facts:
            total += 1
            flips += action(split_for(w, f)[1]) != base_act[f]

    per_rule: dict[str, float] = {}
    for rule in BASELINE:
        n = changed = 0
        rr = random.Random(seed + 1)
        for _ in range(draws):
            w = dict(BASELINE)
            w[rule] = _rand_split(rr)
            for f in facts:
                n += 1
                changed += action(split_for(w, f)[1]) != base_act[f]
        per_rule[rule] = changed / n

    # moving fault between platform and merchant (user share fixed) can never change the action
    swap_flips = 0
    for rule, s in BASELINE.items():
        for shift in range(-100, 101, 5):
            a, m = s[1] + shift, s[2] - shift
            if 0 <= a <= 100 - s[0] and 0 <= m <= 100 - s[0]:
                w = {**BASELINE, rule: (s[0], a, m)}
                swap_flips += sum(action(split_for(w, f)[1]) != base_act[f] for f in facts)

    # for each rule: which user shares (rest to the platform) change at least one case's action
    thresholds: dict[str, str] = {}
    for rule in BASELINE:
        hit = [u for u in range(101)
               if any(action(split_for({**BASELINE, rule: (u, 100 - u, 0)}, f)[1]) != base_act[f] for f in facts)]  # fmt: skip
        thresholds[rule] = f"{hit[0]}% to {hit[-1]}%" if hit else "never"
    return Result(
        len(facts), prod_ok, draws, flips / total, per_rule, swap_flips, (min(plat), max(plat)), thresholds
    )


def render(r: Result) -> str:
    rows = "\n".join(f"| {k} | {v:.1%} |" for k, v in r.per_rule_flip_rate.items())
    thr = "\n".join(f"| {k} | {v} |" for k, v in r.flip_thresholds.items())
    return f"""# Liability Matrix: sensitivity to its own numbers

*Generated by `python -m mandate.liability.sensitivity`. A proposal for discussion, not legal advice.*

The split in each rule (for example 70/30 in R4) is a design choice. Nothing in a card network rulebook, PayPal
policy or case law assigns those numbers, and this project does not claim they do. So the honest question is
whether they matter.

**What the agent does with a split.** The dispute policy refunds (accepts the claim, or offers a refund while it is
still an inquiry) when merchant plus agent-platform fault is {REFUND_AT}% or more, and otherwise defends. It reads
one thing from the split: whether the user's share is above 50%.

## Method

All {r.cases} combinations of the nine facts and the merchant-controlled flag are evaluated under the baseline
weights, then under {r.random_draws} random weight sets (each rule's split drawn uniformly over all
user/platform/merchant splits that sum to 100). We count how often the resulting action differs from baseline.
The baseline reimplementation matches the production matrix on every case: **{r.baseline_matches_production}**.

## Results

* Randomising **every** weight at once changes the action in **{r.flip_rate_all_rules:.1%}** of (weight set, case) pairs. Most of that comes from R1 and R2, which fire on the most fact combinations; uniform random splits are also far more extreme than any proposal anyone would make.
* Moving fault between the platform and the merchant while holding the user's share fixed changed the action
  in **{r.platform_merchant_swaps_that_flip}** of the cases tried. It cannot: the policy never looks at that split.
* Under random weights the average platform share across cases ranged from {r.platform_share_range[0]:.1%} to
  {r.platform_share_range[1]:.1%}, so *who bears the loss* is the part that is sensitive to the numbers, not what the
  agent does.

Randomising one rule at a time, share of cases whose action changes:

| Rule | Cases flipped |
|---|---|
{rows}

User shares (the rest going to the platform) at which a rule's split changes at least one case's action:

| Rule | User share that flips |
|---|---|
{thr}

## What to take from this

1. The agent's refund-or-defend decision is **robust to the platform/merchant weights**; those numbers only
   matter for who pays, which is a commercial question, not a technical one.
2. The decision is sensitive to the **user's share**. R8 is the only rule that gives the user most of the fault,
   and R6 with a confirmed ambiguity (30%) sits 20 points below the flip at 51%. If a reader thinks that 30 should be
   higher, the effect is a defend-instead-of-refund on ambiguous-cart disputes, and it is a one-line change.
3. Weights are exposed in one place (`matrix.py`) and covered by 448 published test vectors, so a different
   policy can be proposed, replayed and diffed.
"""


def main() -> None:  # pragma: no cover
    root = Path(__file__).resolve().parents[4]
    res = analyse()
    (root / "spec" / "liability-sensitivity.md").write_text(render(res))
    out = root / "bench" / "results" / "liability-sensitivity.json"
    out.write_text(json.dumps(res.to_dict(), indent=2) + "\n")
    print(render(res))


if __name__ == "__main__":  # pragma: no cover
    main()
