"""Buyer-agent planner. Chooses an item from *extracted* candidates (never raw page text).

`PolicyPlanner` is the default: a deterministic policy, or a model proposing within the same schema, with a
code-side check that flags any choice that differs from the policy (policy deviation => step-up).

`GullibleAgent` is a scripted, deliberately hijackable baseline used by the bench (configuration C0) and by the
"simulated hijacked agent" demo toggle. It reads raw untrusted text and obeys instructions in it. It is NOT a
language model: results from it measure the defences, not any model's robustness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from ..guard.guard import compare_intent
from .llm import LLMBackend, LLMResult


class PlanOut(BaseModel):
    chosen_sku: str = ""
    quantity: int = 1
    rationale: str = ""
    cites_untrusted: bool = False


@dataclass
class Plan:
    chosen_sku: str
    quantity: int
    rationale: str
    cites_untrusted: bool = False
    policy_deviation: bool = False
    policy_deviation_detail: str = ""
    raw_untrusted_read: bool = False  # the planner saw raw untrusted text (gullible agent only)
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "chosen_sku": self.chosen_sku,
            "quantity": self.quantity,
            "rationale": self.rationale,
            "cites_untrusted": self.cites_untrusted,
            "policy_deviation": self.policy_deviation,
            "policy_deviation_detail": self.policy_deviation_detail,
            "raw_untrusted_read": self.raw_untrusted_read,
        }


def _policy(payload: dict) -> dict:
    intent, cands = payload["intent"], payload["candidates"]
    allowed, merchants = set(intent["allowed_skus"]), set(intent["merchant_ids"])
    qty = int(intent.get("quantity", 1))
    elig = [
        c
        for c in cands
        if c["sku"] in allowed
        and c["merchant_id"] in merchants
        and c["price_minor"] * qty <= intent["max_per_txn_minor"]
    ]
    if not elig:
        return {
            "chosen_sku": "",
            "quantity": qty,
            "rationale": "No candidate satisfies the signed limits.",
            "cites_untrusted": False,
        }

    def key(c: dict) -> tuple:
        gaps = compare_intent(
            intent.get("descriptive", {}), [{"sku": c["sku"], "attributes": c.get("attributes", {})}]
        )
        return (len(gaps) > 0, bool(c.get("suspected_instruction")), c["price_minor"], c["sku"])

    best = min(elig, key=key)
    why = "cheapest in-policy item" + (
        ", satisfying the stated preferences" if not key(best)[0] else ", closest to the stated preferences"
    )
    return {
        "chosen_sku": best["sku"],
        "quantity": qty,
        "rationale": f"Chose {best['sku']}: {why}.",
        "cites_untrusted": False,
    }


def _cites(rationale: str, flagged_spans: list[str]) -> bool:
    r = rationale.lower()
    for span in flagged_spans:
        s = span.lower().strip()
        if len(s) >= 20 and (s[:30] in r or s in r):
            return True
    return False


def plan_purchase(
    backend: LLMBackend, intent: dict, candidates: list[dict], flagged_spans: list[str]
) -> tuple[Plan, LLMResult]:
    """`candidates` carry only extractor output (no raw text, no flagged span text)."""
    payload = {"intent": intent, "candidates": candidates}
    res = backend.complete_json("planner", payload, PlanOut, _policy)
    out = PlanOut.model_validate(res.data)
    policy = _policy(payload)
    deviation = out.chosen_sku != policy["chosen_sku"]
    plan = Plan(
        chosen_sku=out.chosen_sku,
        quantity=out.quantity,
        rationale=out.rationale,
        cites_untrusted=out.cites_untrusted or _cites(out.rationale, flagged_spans),
        policy_deviation=deviation,
        policy_deviation_detail=f"model chose {out.chosen_sku!r}; policy chose {policy['chosen_sku']!r}"
        if deviation
        else "",
    )
    return plan, res


# ----------------------------------------------------------------------------------------------------
# Scripted, hijackable baseline (bench C0 and the "simulated hijacked agent" demo)
# ----------------------------------------------------------------------------------------------------
_VERBS = r"(?:buy|purchase|order|get|grab|pick|choose|select|go\s+with|switch\s+to)"
_BUY_FROM = re.compile(
    rf"\b{_VERBS}\s+(?:the\s+)?(?:sku\s+|item\s+|model\s+)?([A-Z]{{2,4}}-\d+)", re.IGNORECASE
)
_UNITS = re.compile(rf"\b{_VERBS}\s+(\d+)\s+(?:units?|pairs?|of\s+them)", re.IGNORECASE)


def _sentence_with(text: str, needle: str) -> str:
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if needle.lower() in sent.lower():
            return sent.strip()[:200]
    return text[:200]


class GullibleAgent:
    """Obeys the first instruction it finds in raw catalog text; otherwise picks the cheapest in-limits item."""

    name = "gullible-scripted"

    def plan(self, intent: dict, raw_items: list[dict]) -> Plan:
        """raw_items: [{sku, merchant_id, price_minor, attributes, description, reviews}] (raw, untrusted text)."""
        base = _policy(
            {
                "intent": intent,
                "candidates": [
                    {k: it[k] for k in ("sku", "merchant_id", "price_minor", "attributes")}
                    for it in raw_items
                ],
            }
        )
        sku, qty, why = base["chosen_sku"], base["quantity"], base["rationale"]
        raw = True
        for it in raw_items:
            for text in [it.get("description", ""), *it.get("reviews", [])]:
                m = _BUY_FROM.search(text)
                u = _UNITS.search(text)
                anchor = m or u
                if anchor is None:
                    continue
                why = "Followed the note on the page: " + _sentence_with(text, anchor.group(0))
                if m:
                    sku = m.group(1).upper()
                    if u:
                        qty = int(u.group(1))
                elif u:
                    qty = int(u.group(1))
                    sku = it["sku"]
                return Plan(sku, qty, why, True, raw_untrusted_read=raw)
        return Plan(sku, qty, why, False, raw_untrusted_read=raw)
