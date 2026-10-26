"""Derive the nine Liability Matrix facts (P1..P9) for an order from stored records. Pure data in, facts out."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from ..credentials.chain import ChainReport, presentations_for, verify_purchase
from ..db import AgentDecision, Approval, Database, Mandate, Order, Registry
from ..ledger.ledger import Ledger
from .matrix import Facts


@dataclass
class FactsBundle:
    facts: Facts
    report: ChainReport | None
    decisions: list[dict] = field(default_factory=list)
    intent_gaps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # one line per fact that did not hold, with the reason


def derive_facts(
    db: Database, ledger: Ledger, order: Order, issuer_public_key, merchant_jwks: dict[str, dict]
) -> FactsBundle:
    with db.session() as s:
        m = s.get(Mandate, order.mandate_id)
        decisions = list(
            s.execute(
                select(AgentDecision).where(AgentDecision.order_id == order.id).order_by(AgentDecision.id)
            ).scalars()
        )
        approved = s.execute(
            select(Approval).where(
                Approval.status == "approved",
                Approval.kind == "purchase",
                Approval.payload["order_id"].as_string() == order.id,
            )
        ).first()
        blocked = s.execute(
            select(Registry).where(Registry.merchant_id == order.merchant_id, Registry.status == "blocked")
        ).first()
    notes: list[str] = []
    report: ChainReport | None = None
    if m is None:
        return FactsBundle(Facts(p1=False, p2=False), None, [], [], ["mandate record missing"])

    # P1 / P2: re-verify the stored chain *as of the purchase time* (L3 lives five minutes)
    try:
        sku = order.items[0]["sku"]
        pay_pres, chk_pres = presentations_for(m.l2, order.merchant_id, sku)
        report = verify_purchase(
            l1=m.l1, l2=m.l2, l3a=order.l3a, l3b=order.l3b,
            l2_payment_presentation=pay_pres, l2_checkout_presentation=chk_pres,
            issuer_public_key=issuer_public_key, merchant_public_jwks=merchant_jwks, now=order.created_at,
        )  # fmt: skip
        p1 = report.chain_valid and not report.errors
        p2 = report.constraints_satisfied
    except Exception as exc:  # noqa: BLE001
        p1 = p2 = False
        notes.append(f"P1/P2: stored chain could not be verified ({exc})")
    if report is not None:
        if not p1:
            notes.append("P1: credential chain did not verify: " + "; ".join(report.errors))
        if p1 and not p2:
            notes.append("P2: constraints violated: " + "; ".join(report.violations))

    # P3: cart vs descriptive intent (recorded by the guard at decision time)
    gaps: list[str] = []
    allow_rows = [d for d in decisions if d.verdict == "allow"]
    for d in allow_rows:
        gaps.extend(d.plan.get("intent_gaps", []))
    p3 = not gaps
    if gaps:
        notes.append("P3: cart does not match the stated preferences: " + "; ".join(gaps))

    # P4: no untrusted instruction influenced the choice (unless the user approved after the warning)
    flagged = [d for d in decisions if d.taint_flags]
    p4 = not flagged or bool(approved)
    if flagged and not approved:
        notes.append(
            "P4: untrusted instruction influenced the choice: "
            + ", ".join(sorted({f for d in flagged for f in d.taint_flags}))
        )
    merchant_controlled = any(
        i.get("flagged") and i.get("merchant_controlled") for d in flagged for i in d.inputs
    )

    # P5: amount charged equals the signed cart
    signed_total = report.checkout_total_minor if report else None
    captured = order.captured_amount_minor
    p5 = (
        signed_total is not None
        and (captured is None or captured == signed_total)
        and order.amount_minor == signed_total
    )
    if not p5:
        notes.append(
            f"P5: charged {captured if captured is not None else order.amount_minor} but signed cart total is {signed_total}"
        )

    # P6: fulfilment proven (tracking recorded and delivery confirmed)
    p6 = bool(order.tracking) and order.delivered_at is not None
    if not p6:
        notes.append("P6: " + ("no tracking recorded" if not order.tracking else "delivery not confirmed"))

    # P7: not revoked at capture
    at = order.captured_at if order.captured_at is not None else order.created_at
    p7 = not ledger.was_revoked_at(order.mandate_id, at)
    if not p7:
        notes.append("P7: the mandate was revoked before capture")

    # P8: merchant authentic and allowed
    p8 = (
        bool(
            report
            and report.chain_valid
            and not any("allowlist" in v or "allowed" in v for v in report.violations)
        )
        and not blocked
    )
    if not p8:
        notes.append("P8: merchant not authentic, not allowed or blocked")

    # P9: user confirmed after an ambiguity was flagged
    p9 = bool(m.ambiguity_flagged and m.user_confirmed)

    facts = Facts(p1=p1, p2=p2, p3=p3, p4=p4, p5=p5, p6=p6, p7=p7, p8=p8, p9=p9,
                  injected_text_merchant_controlled=merchant_controlled)  # fmt: skip
    return FactsBundle(
        facts,
        report,
        [
            {
                "id": d.id,
                "verdict": d.verdict,
                "taint_flags": d.taint_flags,
                "inputs": d.inputs,
                "plan": d.plan,
            }
            for d in decisions
        ],
        gaps,
        notes,
    )
