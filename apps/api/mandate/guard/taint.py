"""Taint labels and decision provenance.

Labels:
    USER       the user's own words and the signed mandate           trusted
    SIGNED     merchant-signed cart fields, PayPal responses         authenticated; used for prices and ids
    UNTRUSTED  catalog free text, reviews, web pages, dispute msgs   never enters a planner prompt directly

Only the quarantined extractor reads UNTRUSTED text; the planner sees the extractor's fixed-field output.
The provenance record lists every input to a decision with its label and a content hash, and is stored with
the decision and in the audit log.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum


class Label(StrEnum):
    USER = "USER"
    SIGNED = "SIGNED"
    UNTRUSTED = "UNTRUSTED"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ProvInput:
    label: Label
    source: str  # e.g. "user_prompt", "catalog:SKU-1:description", "paypal:order"
    sha256: str
    summary: str = ""
    flagged: bool = False  # extractor reported a suspected instruction in this input
    merchant_controlled: bool = False  # the text came from the merchant's own page (matters for liability R5)

    def to_dict(self) -> dict:
        return {
            "label": self.label.value,
            "source": self.source,
            "sha256": self.sha256,
            "summary": self.summary[:160],
            "flagged": self.flagged,
            "merchant_controlled": self.merchant_controlled,
        }


@dataclass
class Provenance:
    inputs: list[ProvInput] = field(default_factory=list)

    def add(
        self,
        label: Label,
        source: str,
        content: str,
        summary: str = "",
        flagged: bool = False,
        merchant_controlled: bool = False,
    ) -> ProvInput:
        item = ProvInput(
            label, source, sha256_hex(content), summary or content[:80], flagged, merchant_controlled
        )
        self.inputs.append(item)
        return item

    @property
    def flagged_inputs(self) -> list[ProvInput]:
        return [i for i in self.inputs if i.flagged]

    @property
    def merchant_controlled_injection(self) -> bool:
        return any(i.flagged and i.merchant_controlled for i in self.inputs)

    def to_list(self) -> list[dict]:
        return [i.to_dict() for i in self.inputs]


@dataclass
class TaintVerdict:
    flags: list[str]
    step_up: bool
    detail: list[str]


def evaluate_taint(prov: Provenance, plan: dict) -> TaintVerdict:
    """Decide whether provenance requires step-up.

    A choice escalates when
      * it cites an instruction that came from an UNTRUSTED source (`plan['cites_untrusted']`), or
      * any input to the decision was flagged by the quarantined extractor, or
      * it deviates from the deterministic policy (`plan['policy_deviation']`), or
      * raw UNTRUSTED text reached the planner (a pipeline bug; fail closed).
    """
    flags: list[str] = []
    detail: list[str] = []
    if plan.get("cites_untrusted"):
        flags.append("cites_untrusted_instruction")
        detail.append("the decision cites an instruction from an untrusted source")
    for i in prov.flagged_inputs:
        flags.append("suspected_instruction")
        detail.append(f"extractor flagged {i.source}")
    if plan.get("policy_deviation"):
        flags.append("policy_deviation")
        detail.append(plan.get("policy_deviation_detail", "choice differs from the deterministic policy"))
    for i in prov.inputs:
        if i.label == Label.UNTRUSTED and i.source.startswith("planner:raw"):
            flags.append("raw_untrusted_in_planner")
            detail.append(f"raw untrusted text reached the planner via {i.source}")
    flags = sorted(set(flags))
    return TaintVerdict(flags=flags, step_up=bool(flags), detail=detail)
