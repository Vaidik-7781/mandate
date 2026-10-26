"""Liability Matrix v0.1: a deterministic rule set that assigns fault for a disputed agent purchase.

A proposal, not legal advice. Nine true/false facts (P1..P9) go in; the first matching rule (R1..R8) fires and
returns a fault split across user / agent platform / merchant. A model may *explain* the result but cannot change
it: `Attribution` is frozen and the explainer only receives a copy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

MATRIX_VERSION = "0.1"

FACT_DESCRIPTIONS: dict[str, str] = {
    "p1": "The credential chain verifies",
    "p2": "The final mandates satisfy every constraint",
    "p3": "The cart matches the descriptive intent",
    "p4": "No untrusted instruction influenced the choice",
    "p5": "The amount charged equals the signed cart",
    "p6": "Delivery or fulfilment is proven",
    "p7": "The mandate was not revoked at capture",
    "p8": "The merchant is authentic and allowed",
    "p9": "The user confirmed the compiled fields after an ambiguity was flagged",
}

PARTIES = ("user", "agent_platform", "merchant")


@dataclass(frozen=True)
class Facts:
    p1: bool = True
    p2: bool = True
    p3: bool = True
    p4: bool = True
    p5: bool = True
    p6: bool = True
    p7: bool = True
    p8: bool = True
    p9: bool = False
    injected_text_merchant_controlled: bool = False  # refines R5

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    split: tuple[int, int, int]


@dataclass(frozen=True)
class Attribution:
    rule_id: str
    title: str
    split: dict[str, int]  # {"user": n, "agent_platform": n, "merchant": n}, sums to 100
    facts: dict
    trace: tuple[tuple[str, bool], ...] = field(
        default_factory=tuple
    )  # (rule id, matched) in evaluation order
    defend_merchant: bool = False  # R8: valid authorisation, the merchant should defend

    @property
    def merchant_or_platform_share(self) -> int:
        return self.split["merchant"] + self.split["agent_platform"]

    def to_dict(self) -> dict:
        return {
            "matrix_version": MATRIX_VERSION,
            "rule_id": self.rule_id,
            "title": self.title,
            "split": self.split,
            "facts": self.facts,
            "trace": [{"rule": r, "matched": m} for r, m in self.trace],
            "defend_merchant": self.defend_merchant,
        }


def _split(u: int, a: int, m: int) -> dict[str, int]:
    assert u + a + m == 100
    return {"user": u, "agent_platform": a, "merchant": m}


def attribute(f: Facts) -> Attribution:
    """Apply R1..R8 in order; the first match wins. Every rule's outcome is kept in the trace."""
    trace: list[tuple[str, bool]] = []
    result: tuple[str, str, dict[str, int], bool] | None = None

    def rule(rid: str, cond: bool, title: str, split: dict[str, int], defend: bool = False) -> None:
        nonlocal result
        trace.append((rid, bool(cond) and result is None))
        if cond and result is None:
            result = (rid, title, split, defend)

    rule("R1", not f.p7, "Mandate was revoked at capture", _split(0, 100, 0))
    rule("R2", not (f.p1 and f.p2), "Credential chain or constraints failed", _split(0, 100, 0))
    rule("R3", not f.p5, "Amount charged differs from the signed cart", _split(0, 0, 100))
    rule("R4", not f.p8, "Merchant not authentic or not allowed", _split(0, 70, 30))
    rule(
        "R5",
        not f.p4,
        "Untrusted instruction influenced the choice",
        _split(0, 70, 30) if f.injected_text_merchant_controlled else _split(0, 100, 0),
    )
    rule(
        "R6",
        not f.p3,
        "Cart does not match the descriptive intent",
        _split(30, 70, 0) if f.p9 else _split(0, 100, 0),
    )
    rule("R7", not f.p6, "Delivery or fulfilment not proven", _split(0, 0, 100))
    rule("R8", True, "Valid authorisation; defend the merchant", _split(100, 0, 0), defend=True)
    assert result is not None
    rid, title, split, defend = result
    return Attribution(rid, title, split, f.as_dict(), tuple(trace), defend)


def explain(attr: Attribution) -> str:
    """Deterministic plain-language explanation. An LLM may paraphrase this but never alters `attr`."""
    s = attr.split
    who = ", ".join(f"{p.replace('_', ' ')} {pct}%" for p, pct in s.items() if pct)
    failed = [
        FACT_DESCRIPTIONS[k] for k, v in attr.facts.items() if k in FACT_DESCRIPTIONS and k != "p9" and not v
    ]
    base = f"Rule {attr.rule_id} fired: {attr.title}. Fault split: {who}."
    if failed:
        base += " Facts that did not hold: " + "; ".join(failed).lower() + "."
    if attr.defend_merchant:
        base += (
            " All checks passed, so the purchase was authorised and the merchant should defend the dispute."
        )
    return base
