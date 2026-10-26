"""Dispute agent: read a dispute, attribute fault by rule, decide, assemble evidence, act through the gateway.

The model never decides money. Order of the decision (all plain code):
    0. buyer messages are UNTRUSTED: the quarantined extractor reads them; any suspected instruction => human
    1. amount above the human threshold => human (every money-moving action above $100 needs a person)
    2. merchant + agent-platform fault >= 50% => accept the claim (or offer a refund while it is still an inquiry)
    3. seller protection already applied => nothing to do (do not duplicate evidence)
    4. evidence completeness below the minimum => human
    5. otherwise expected value: fight iff p > (c + f) / (A + f), else accept
`p` comes from a fixed table keyed by evidence completeness (a calibration guess, labelled as such).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from .. import clock
from ..audit.log import Audit
from ..db import Approval, Database, Dispute, EvidencePacket, Mandate, Order
from ..gateway.paypal.client import PayPalError
from ..gateway.paypal.gateway import PayPalGateway
from ..guard.guard import Guard
from ..ledger.ledger import Ledger
from ..liability.facts import FactsBundle, derive_facts
from ..liability.matrix import Attribution, attribute, explain
from ..money import from_paypal
from ..settings import Settings
from .evidence import ALLOWED_EVIDENCE_TYPES, Packet, build_packet
from .extractor import extract
from .llm import LLMBackend


def win_probability(completeness: float) -> float:
    """Calibration table (a documented guess, not learned): chance of winning a defended dispute."""
    if completeness >= 0.95:
        return 0.85
    if completeness >= 0.9:
        return 0.78
    if completeness >= 0.8:
        return 0.68
    return 0.4


def should_fight(p: float, amount_minor: int, cost_minor: int, fee_minor: int) -> bool:
    """Fight iff the expected recovery beats the cost: p * (A + f) > c + f."""
    return p * (amount_minor + fee_minor) > cost_minor + fee_minor


@dataclass
class Decision:
    action: str  # fight | accept | offer | human | none
    reasons: list[str]
    offer_minor: int | None = None
    recommended: str | None = None  # what a human is asked to approve
    p: float | None = None
    ev_fight: float | None = None

    def to_dict(self) -> dict:
        return {"action": self.action, "reasons": self.reasons, "offer_minor": self.offer_minor,
                "recommended": self.recommended, "p": self.p, "ev_fight": self.ev_fight}  # fmt: skip


@dataclass
class DisputeResult:
    dispute_id: str
    order_id: str | None
    decision: Decision
    attribution: Attribution | None
    packet_summary: dict | None
    taint_flags: list[str]
    executed: bool
    detail: str = ""
    approval_id: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id,
            "order_id": self.order_id,
            "decision": self.decision.to_dict(),
            "attribution": self.attribution.to_dict() if self.attribution else None,
            "explanation": explain(self.attribution) if self.attribution else None,
            "packet": self.packet_summary,
            "taint_flags": self.taint_flags,
            "executed": self.executed,
            "detail": self.detail,
            "approval_id": self.approval_id,
        }


def decide(
    *,
    amount_minor: int,
    stage: str,
    attribution: Attribution,
    completeness: float,
    taint_flags: list[str],
    protection_applied: bool,
    settings: Settings,
) -> Decision:
    reasons: list[str] = []
    non_user = attribution.merchant_or_platform_share
    user_share = attribution.split["user"]

    def refund_decision() -> Decision:
        if stage == "INQUIRY" and user_share > 0:
            offer = amount_minor * (100 - user_share) // 100
            return Decision(
                "offer", [*reasons, f"inquiry stage: offer {100 - user_share}% refund"], offer_minor=offer
            )
        if stage == "INQUIRY":
            return Decision(
                "offer",
                [*reasons, "inquiry stage: refund offer resolves it cheaply"],
                offer_minor=amount_minor,
            )
        return Decision("accept", [*reasons, "claim stage: accept and refund"], offer_minor=amount_minor)

    # 0. taint
    if taint_flags:
        rec = refund_decision() if non_user >= 50 else None
        return Decision(
            "human",
            ["buyer message contained a suspected instruction: " + ", ".join(taint_flags)],
            recommended=rec.action if rec else "fight",
        )

    # what we would do without a human gate
    if non_user >= 50:
        reasons.append(f"rule {attribution.rule_id}: {non_user}% of fault is on the merchant/agent platform")
        base = refund_decision()
    elif protection_applied:
        return Decision("none", ["PayPal seller protection already applied; evidence not duplicated"])
    else:
        p = win_probability(completeness)
        fight = should_fight(
            p, amount_minor, settings.dispute_cost_to_fight_minor, settings.dispute_fee_minor
        )
        ev = p * (amount_minor + settings.dispute_fee_minor) - (
            settings.dispute_cost_to_fight_minor + settings.dispute_fee_minor
        )
        reasons.append(f"rule {attribution.rule_id}: valid authorisation, merchant should defend")
        if completeness < settings.evidence_min_completeness:
            return Decision(
                "human",
                [
                    *reasons,
                    f"evidence completeness {completeness:.0%} is below {settings.evidence_min_completeness:.0%}",
                ],
                recommended="fight" if fight else "accept",
                p=p,
                ev_fight=ev,
            )
        if fight:
            base = Decision(
                "fight", [*reasons, f"p={p:.2f} beats the cost-to-fight threshold"], p=p, ev_fight=ev
            )
        else:
            base = Decision("accept", [*reasons, f"p={p:.2f} does not beat the cost to fight"], p=p, ev_fight=ev,
                            offer_minor=amount_minor)  # fmt: skip

    # 1. money gate
    if amount_minor > settings.dispute_human_threshold_minor:
        return Decision(
            "human",
            [
                *base.reasons,
                f"amount {amount_minor} is above the human threshold {settings.dispute_human_threshold_minor}",
            ],
            offer_minor=base.offer_minor,
            recommended=base.action,
            p=base.p,
            ev_fight=base.ev_fight,
        )
    return base


class DisputeAgent:
    def __init__(
        self,
        db: Database,
        ledger: Ledger,
        audit: Audit,
        guard: Guard,
        gateway: PayPalGateway,
        backend: LLMBackend,
        settings: Settings,
        issuer_public_key: Any,
        merchant_jwks: Any,
    ) -> None:
        self.db, self.ledger, self.audit, self.guard, self.gw = db, ledger, audit, guard, gateway
        self.backend, self.cfg = backend, settings
        self.issuer_public_key, self.merchant_jwks = issuer_public_key, merchant_jwks

    # ------------------------------------------------------------------------------------------------
    def find_order(self, d: dict) -> Order | None:
        tx = (d.get("disputed_transactions") or [{}])[0]
        cap_id, custom = tx.get("seller_transaction_id"), tx.get("custom")
        with self.db.session() as s:
            if cap_id:
                o = s.execute(select(Order).where(Order.capture_id == cap_id)).scalar_one_or_none()
                if o:
                    return o
            if custom:
                return s.execute(select(Order).where(Order.chain_digest == custom)).scalar_one_or_none()
        return None

    def _taint(self, d: dict) -> list[str]:
        flags: list[str] = []
        for msg in d.get("messages") or []:
            if msg.get("posted_by") == "BUYER":
                res = extract(self.backend, {"sku": "dispute-message", "name": "", "description": "",
                                             "reviews": [msg.get("content", "")]})  # fmt: skip
                if res.data.get("suspected_instruction"):
                    flags.append("suspected_instruction")
        return sorted(set(flags))

    def analyse(self, dispute_id: str) -> tuple[dict, Order | None, FactsBundle | None, Attribution | None]:
        d = self.gw.get_dispute(dispute_id)
        order = self.find_order(d)
        if order is None:
            return d, None, None, None
        bundle = derive_facts(self.db, self.ledger, order, self.issuer_public_key, self.merchant_jwks())
        return d, order, bundle, attribute(bundle.facts)

    # ------------------------------------------------------------------------------------------------
    def handle(self, dispute_id: str, *, execute: bool = True) -> DisputeResult:
        d, order, bundle, attr = self.analyse(dispute_id)
        cur = d["dispute_amount"]["currency_code"]
        amount = from_paypal(d["dispute_amount"]["value"], cur)
        stage = d.get("dispute_life_cycle_stage", "")
        reason = d.get("reason", "")
        now = clock.now()
        self.audit.append("dispute_agent", "dispute.read",
                          {"dispute_id": dispute_id, "reason": reason, "status": d.get("status"), "amount_minor": amount})  # fmt: skip

        if order is None or bundle is None or attr is None:
            dec = Decision("human", ["no matching order found for this dispute"], recommended="none")
            appr = self._queue(dispute_id, None, dec, amount, cur)
            self._store(d, None, None, dec, None, amount, cur)
            return DisputeResult(dispute_id, None, dec, None, None, [], False, "unmatched dispute", appr)

        flags = self._taint(d)
        with self.db.session() as s:
            m = s.get(Mandate, order.mandate_id)
        audit_state = self.audit.verify()
        packet = build_packet(self.backend, dispute=d, order=order, mandate=m, bundle=bundle, attribution=attr,
                              audit_head=self.audit.head(), audit_ok=audit_state.ok)  # fmt: skip
        tx = (d.get("disputed_transactions") or [{}])[0]
        protection = (
            tx.get("seller_protection_eligible") is True and d.get("seller_protection_applied") is True
        )
        dec = decide(amount_minor=amount, stage=stage, attribution=attr, completeness=packet.completeness,
                     taint_flags=flags, protection_applied=protection, settings=self.cfg)  # fmt: skip
        self.audit.append(
            "dispute_agent",
            "dispute.decision",
            {"dispute_id": dispute_id, "order_id": order.id, "rule": attr.rule_id, "split": attr.split,
             "completeness": packet.completeness, "taint": flags, "decision": dec.to_dict()},
        )  # fmt: skip
        approval_id = None
        executed = False
        detail = ""
        if dec.action == "human":
            approval_id = self._queue(dispute_id, order.id, dec, amount, cur)
            detail = "waiting for human approval"
        elif execute and dec.action != "none":
            try:
                detail = self._execute(dec, d, order, packet, amount, cur)
                executed = True
            except PayPalError as exc:  # already acted on (webhook redelivery, second click) or wrong state
                detail = f"not executed: {exc}"
                self.audit.append("dispute_agent", "dispute.execute_refused",
                                  {"dispute_id": dispute_id, "action": dec.action, "error": str(exc)[:300]})  # fmt: skip
            d = self.gw.get_dispute(dispute_id)  # status/outcome moved while we acted (webhooks arrive first)
        self._store(d, order, attr, dec, packet, amount, cur, now=now)
        return DisputeResult(
            dispute_id, order.id, dec, attr, packet.summary(), flags, executed, detail, approval_id
        )

    # ------------------------------------------------------------------------------------------------
    def _execute(
        self, dec: Decision, d: dict, order: Order, packet: Packet | None, amount: int, cur: str
    ) -> str:
        did = d["dispute_id"]
        if dec.action == "fight":
            assert packet is not None
            note = packet.note[:2000]
            reason = d.get("reason", "")
            allowed = ALLOWED_EVIDENCE_TYPES.get(reason, ("OTHER",))
            etype = packet.evidence_type if packet.evidence_type in allowed else allowed[-1]
            tracking = (
                {"tracking_info": [{"carrier_name": (order.tracking or {}).get("carrier", "OTHER"),
                                    "tracking_number": (order.tracking or {}).get("tracking_number", "")}]}
                if order.tracking
                else None
            )  # fmt: skip
            main: dict = {"evidence_type": etype, "notes": note}
            evidences = [main]
            if etype == "PROOF_OF_FULFILLMENT" and tracking:
                main["evidence_info"] = tracking
            elif (
                tracking and "PROOF_OF_FULFILLMENT" in allowed
            ):  # e.g. UNAUTHORISED: chain as OTHER + tracking
                evidences.append({"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "Carrier tracking for the order.",
                                  "evidence_info": tracking})  # fmt: skip
            self.gw.provide_evidence(
                did, evidences, [(f"mandate-evidence-{did}.pdf", packet.pdf, "application/pdf")]
            )
            self.audit.append("dispute_agent", "dispute.evidence_submitted",
                              {"dispute_id": did, "sha256": packet.sha256, "completeness": packet.completeness})  # fmt: skip
            return "evidence submitted"
        if dec.action in ("accept", "offer"):
            amt = dec.offer_minor if dec.offer_minor is not None else amount
            tok = self.guard.issue_dispute_token(
                "accept_claim" if dec.action == "accept" else "make_offer",
                mandate_id=order.mandate_id, dispute_id=did, amount_minor=amt, currency=cur,
                merchant_id=order.merchant_id,
            )  # fmt: skip
            opts = d.get("allowed_response_options") or {}
            if dec.action == "accept":
                kinds = (opts.get("accept_claim") or {}).get("accept_claim_types") or []
                kind = ("REFUND" if amt >= amount else "PARTIAL_REFUND") if kinds else None
                if kind and kind not in kinds:
                    kind = kinds[
                        0
                    ]  # PayPal tells us which types this dispute allows; never send one it did not list
                self.gw.accept_claim(tok, dispute_id=did, amount_minor=amt, currency=cur,
                                     reason="NOT_ABLE_TO_WIN", note="; ".join(dec.reasons)[:1900],
                                     claim_type=kind)  # fmt: skip
                self.audit.append(
                    "dispute_agent", "dispute.accepted", {"dispute_id": did, "amount_minor": amt}
                )
                return "claim accepted"
            offer_types = (opts.get("make_offer") or {}).get("offer_types") or ["REFUND"]
            self.gw.make_offer(
                tok,
                dispute_id=did,
                amount_minor=amt,
                currency=cur,
                note="; ".join(dec.reasons)[:1900],
                offer_type="REFUND" if "REFUND" in offer_types else offer_types[0],
            )
            self.audit.append("dispute_agent", "dispute.offered", {"dispute_id": did, "amount_minor": amt})
            return "refund offered"
        return ""

    def _queue(self, dispute_id: str, order_id: str | None, dec: Decision, amount: int, cur: str) -> str:
        with self.db.session() as s:
            existing = s.execute(
                select(Approval).where(Approval.kind == "dispute", Approval.status == "pending")
            ).scalars()
            for a in existing:
                if a.payload.get("dispute_id") == dispute_id:
                    return a.id
            aid = f"appr_{dispute_id}_{int(time.time() * 1000) % 10**8}"
            s.add(Approval(id=aid, mandate_id=None, kind="dispute",
                           payload={"dispute_id": dispute_id, "order_id": order_id, "amount_minor": amount,
                                    "currency": cur, "recommended": dec.recommended, "decision": dec.to_dict()},
                           reasons=dec.reasons, status="pending", created_at=clock.now()))  # fmt: skip
        self.audit.append("dispute_agent", "approval.queued", {"approval_id": aid, "dispute_id": dispute_id,
                                                               "reasons": dec.reasons})  # fmt: skip
        return aid

    def _store(self, d: dict, order: Order | None, attr: Attribution | None, dec: Decision,
               packet: Packet | None, amount: int, cur: str, now: int | None = None) -> None:  # fmt: skip
        now = now or clock.now()
        with self.db.session() as s:
            row = s.get(Dispute, d["dispute_id"])
            if row is None:
                row = Dispute(dispute_id=d["dispute_id"], created_at=now, updated_at=now)
                s.add(row)
            row.order_id = order.id if order else None
            row.mandate_id = order.mandate_id if order else None
            row.reason = d.get("reason", "")
            row.stage = d.get("dispute_life_cycle_stage", "")
            row.status = d.get("status", "")
            row.amount_minor = amount
            row.currency = cur
            row.due_time = d.get("seller_response_due_date")
            row.outcome = (d.get("dispute_outcome") or {}).get("outcome_code") or row.outcome
            row.fault_split = attr.split if attr else None
            row.rule_id = attr.rule_id if attr else None
            row.decision = dec.action
            row.analysis = {"decision": dec.to_dict(), "attribution": attr.to_dict() if attr else None,
                            "explanation": explain(attr) if attr else None,
                            "packet": packet.summary() if packet else None}  # fmt: skip
            row.updated_at = now
            if packet is not None:
                s.add(EvidencePacket(dispute_id=d["dispute_id"], completeness=packet.completeness,
                                     sha256=packet.sha256, pdf_path="", note=packet.note,
                                     submitted_at=now if dec.action == "fight" else None))  # fmt: skip

    # ------------------------------------------------------------------------------------------------
    def resolve_approval(self, approval_id: str, approve: bool) -> dict:
        """A human decides a queued dispute action. Approve executes the recommended action."""
        with self.db.session() as s:
            a = s.get(Approval, approval_id)
            if a is None or a.kind != "dispute":
                raise KeyError(approval_id)
            if a.status != "pending":
                return {"status": a.status, "detail": "already decided"}
            a.status = "approved" if approve else "declined"
            a.decided_at = clock.now()
            payload = dict(a.payload)
        self.audit.append("human", "approval.decided", {"approval_id": approval_id, "approved": approve,
                                                        "dispute_id": payload["dispute_id"]})  # fmt: skip
        if not approve:
            return {"status": "declined", "detail": "no action taken"}
        rec = payload.get("recommended") or "none"
        d, order, bundle, attr = self.analyse(payload["dispute_id"])
        if order is None or attr is None or bundle is None or rec == "none":
            return {"status": "approved", "detail": "nothing to execute"}
        cur = d["dispute_amount"]["currency_code"]
        amount = from_paypal(d["dispute_amount"]["value"], cur)
        with self.db.session() as s:
            m = s.get(Mandate, order.mandate_id)
        packet = build_packet(self.backend, dispute=d, order=order, mandate=m, bundle=bundle, attribution=attr,
                              audit_head=self.audit.head(), audit_ok=self.audit.verify().ok)  # fmt: skip
        offer = payload.get("decision", {}).get("offer_minor")
        dec = Decision(rec, ["approved by a human"], offer_minor=offer if offer is not None else amount)
        try:
            detail = self._execute(dec, d, order, packet, amount, cur)
        except PayPalError as exc:
            return {"status": "approved", "detail": f"PayPal refused: {exc}"}
        d = self.gw.get_dispute(payload["dispute_id"])
        self._store(d, order, attr, dec, packet, amount, cur)
        return {"status": "approved", "detail": detail}

    def adjudicate_scripted(self, dispute_id: str) -> dict:
        """SANDBOX ONLY, scripted: PayPal's sandbox lets the merchant force an outcome. We pick one by a fixed rule
        (seller wins when the facts support the merchant) so the demo shows both branches. This is NOT a real
        adjudication and is reported as such."""
        d = self.gw.get_dispute(dispute_id)
        if d.get("status") != "UNDER_REVIEW":
            return {"adjudicated": False, "detail": f"dispute is {d.get('status')}"}
        _, order, bundle, attr = self.analyse(dispute_id)
        outcome = "SELLER_FAVOR" if (attr is not None and attr.defend_merchant) else "BUYER_FAVOR"
        self.gw.adjudicate(dispute_id, outcome)
        self.audit.append(
            "dispute_agent", "dispute.adjudicated_scripted", {"dispute_id": dispute_id, "outcome": outcome}
        )
        return {"adjudicated": True, "outcome": outcome, "scripted": True}
