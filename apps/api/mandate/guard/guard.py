"""The guard: the only place a money decision is made. Plain code, no model.

Order of work for a purchase:
    1. mandate status (revoked / paused / expired)
    2. credential chain + every signed constraint (all violations collected, never first-fail)
    3. merchant registry (blocked merchants)
    4. the request's own claims must equal what is signed (amount, merchant, SKU)
    5. cart-to-intent diff for descriptive constraints (recorded, feeds the liability engine)
    6. taint / provenance -> step-up
    7. atomic ledger reservation (budget, occurrences, replay, velocity)
    8. a gateway token bound to this exact cart
A denial at any step still runs the later read-only checks so the user sees every reason at once.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from .. import clock
from ..agents.prompts import PROMPT_VERSION
from ..audit.log import Audit
from ..credentials.chain import (
    ChainReport,
    Merchant,
    SignedPurchase,
    order_chain_digest,
    verify_purchase,
)
from ..db import AgentDecision, Database, Mandate, Order, Registry
from ..ledger.ledger import Ledger
from ..settings import get_settings
from .taint import Provenance, evaluate_taint
from .token import GatewayToken


@dataclass(frozen=True)
class GuardFeatures:
    """Ablation switches used only by Mandate-Bench to measure what each defence contributes. All on in production.

    `chain` (credential + constraint verification, request-claims check, merchant registry) is always on when a
    guard is used at all; the bench's C0 configuration skips the guard entirely.
    """

    status: bool = True  # revocation / pause check at decision time
    taint: bool = True  # provenance-based step-up
    ledger: bool = True  # budget, occurrences, replay set, velocity


@dataclass
class PurchaseRequest:
    mandate_id: str
    order_id: str
    merchant: Merchant
    sku: str
    amount_minor: int
    currency: str
    signed: SignedPurchase
    provenance: Provenance = field(default_factory=Provenance)
    plan: dict[str, Any] = field(default_factory=dict)
    model: str = "offline"
    human_approved: bool = False  # set after a step-up approval; skips taint/amount step-up only


@dataclass
class GuardDecision:
    verdict: str  # allow | deny | step_up
    codes: list[str]
    reasons: list[str]
    report: ChainReport | None = None
    token: GatewayToken | None = None
    custom_id: str | None = None
    taint_flags: list[str] = field(default_factory=list)
    intent_gaps: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    decision_id: int | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"

    def summary(self) -> dict:
        return {
            "verdict": self.verdict,
            "codes": self.codes,
            "reasons": self.reasons,
            "taint_flags": self.taint_flags,
            "intent_gaps": self.intent_gaps,
            "custom_id": self.custom_id,
        }


def compare_intent(descriptive: dict, items: list[dict]) -> list[str]:
    """Cart-to-intent diff for the *descriptive* constraints (color, size, max_weight_g, waterproof...).

    Descriptive constraints are informational in Verifiable Intent: they never block a purchase, but any gap is
    recorded and later feeds liability rule R6 (P3). Cart items carry merchant-signed `attributes`.
    """
    gaps: list[str] = []
    for key, want in descriptive.items():
        for it in items:
            attrs = it.get("attributes") or {}
            if key.startswith("max_"):
                attr = key[4:]
                have = attrs.get(attr)
                if have is None:
                    gaps.append(f"{it['sku']}: cannot verify {attr} <= {want}")
                elif have > want:
                    gaps.append(f"{it['sku']}: {attr} is {have}, request was at most {want}")
            elif key.startswith("min_"):
                attr = key[4:]
                have = attrs.get(attr)
                if have is None:
                    gaps.append(f"{it['sku']}: cannot verify {attr} >= {want}")
                elif have < want:
                    gaps.append(f"{it['sku']}: {attr} is {have}, request was at least {want}")
            else:
                have = attrs.get(key)
                if have is None:
                    gaps.append(f"{it['sku']}: cannot verify {key} == {want}")
                elif str(have).lower() != str(want).lower():
                    gaps.append(f"{it['sku']}: {key} is {have}, request was {want}")
    return gaps


class Guard:
    def __init__(
        self,
        db: Database,
        ledger: Ledger,
        audit: Audit,
        issuer_public_key: ec.EllipticCurvePublicKey,
        merchant_jwks: Callable[[], dict[str, dict]],
        secret: str,
        features: GuardFeatures | None = None,
    ) -> None:
        self.features = features or GuardFeatures()
        self.db, self.ledger, self.audit = db, ledger, audit
        self.issuer_public_key = issuer_public_key
        self.merchant_jwks = merchant_jwks
        self.secret = secret

    # ------------------------------------------------------------------------------------------------
    def evaluate(self, req: PurchaseRequest) -> GuardDecision:
        cfg = get_settings()
        now = clock.now()
        codes: list[str] = []
        reasons: list[str] = []
        checks: list[dict] = []

        def fail(code: str, why: str) -> None:
            codes.append(code)
            reasons.append(why)

        def passed(name: str) -> None:
            checks.append({"check": name, "ok": True})

        with self.db.session() as s:
            m = s.get(Mandate, req.mandate_id)
            blocked = s.execute(
                select(Registry).where(Registry.merchant_id == req.merchant.id, Registry.status == "blocked")
            ).scalar_one_or_none()
        if m is None:
            d = GuardDecision("deny", ["UNKNOWN_MANDATE"], ["no such mandate"], checks=checks)
            self._record(req, d)
            return d

        # 1. status
        status = self.ledger.status(req.mandate_id) if self.features.status else "active"
        if status != "active":
            fail(status.upper(), f"mandate is {status}")
        else:
            passed("mandate_active")

        # 2. chain + constraints
        report = verify_purchase(
            l1=m.l1,
            l2=m.l2,
            l3a=req.signed.l3a,
            l3b=req.signed.l3b,
            l2_payment_presentation=req.signed.l2_payment_presentation,
            l2_checkout_presentation=req.signed.l2_checkout_presentation,
            issuer_public_key=self.issuer_public_key,
            merchant_public_jwks=self.merchant_jwks(),
            now=now,
        )
        if not report.chain_valid or report.errors:
            fail("CHAIN_INVALID", "credential chain failed: " + "; ".join(report.errors or ["unknown error"]))
        else:
            passed("chain_valid")
        for v in report.violations:
            fail("CONSTRAINT_VIOLATION", v)
        if report.chain_valid and not report.violations:
            passed("constraints_satisfied")

        # 3. registry
        if blocked is not None:
            fail("MERCHANT_BLOCKED", f"merchant {req.merchant.id} is blocked in the registry")
        else:
            passed("merchant_not_blocked")

        # 4. request claims equal signed values (the server never trusts the caller's numbers)
        if report.chain_valid:
            if report.amount_minor != req.amount_minor or report.currency != req.currency:
                fail("AMOUNT_MISMATCH", "request amount differs from the signed payment mandate")
            if (report.merchant or {}).get("id") != req.merchant.id:
                fail("MERCHANT_MISMATCH", "request merchant differs from the signed payee")
            if [li["id"] for li in report.line_items] != [req.sku]:
                fail("ITEM_MISMATCH", "request SKU differs from the signed cart")

        # 5. cart-to-intent diff
        gaps: list[str] = []
        if report.checkout_payload:
            gaps = compare_intent(m.intent.get("descriptive", {}), report.checkout_payload["cart"]["items"])

        # 6. taint -> step-up (only if nothing is already a hard deny)
        taint = evaluate_taint(req.provenance, req.plan)
        if not self.features.taint:
            taint = type(taint)(flags=[], step_up=False, detail=[])
        step_reasons: list[str] = []
        if not req.human_approved:
            if taint.step_up:
                step_reasons.extend(taint.detail)
            if cfg.step_up_threshold_minor and req.amount_minor > cfg.step_up_threshold_minor:
                step_reasons.append(f"amount above the step-up threshold of {cfg.step_up_threshold_minor}")

        decision = GuardDecision(
            verdict="deny",
            codes=codes,
            reasons=reasons,
            report=report,
            taint_flags=taint.flags,
            intent_gaps=gaps,
            checks=checks,
        )
        if codes:
            self._record(req, decision)
            return decision
        if step_reasons:
            decision.verdict = "step_up"
            decision.codes = ["STEP_UP"]
            decision.reasons = step_reasons
            self._record(req, decision)
            return decision

        # 7. atomic reservation
        custom_id = order_chain_digest(m.chain_digest, req.signed.l3a, req.signed.l3b)
        if self.features.ledger:
            res = self.ledger.reserve(
                req.mandate_id,
                req.amount_minor,
                idempotency_key=f"reserve:{report.transaction_id}",
                currency=req.currency,
                transaction_id=report.transaction_id or "",
                order_id=req.order_id,
            )
            if not res.ok:
                decision.codes = [res.code]
                decision.reasons = [res.detail]
                self._record(req, decision)
                return decision
            passed("ledger_reserved")

        # 8. token for this exact cart
        decision.verdict = "allow"
        decision.codes = ["OK"]
        decision.reasons = ["all checks passed"]
        decision.custom_id = custom_id
        decision.token = GatewayToken.issue(
            self.secret,
            purpose="create_order",
            mandate_id=req.mandate_id,
            custom_id=custom_id,
            amount_minor=req.amount_minor,
            currency=req.currency,
            merchant_id=req.merchant.id,
            now=now,
        )
        self._record(req, decision)
        return decision

    # ------------------------------------------------------------------------------------------------
    def authorize_capture(self, order: Order) -> GuardDecision:
        """Re-check just before capture: revocation race, amount integrity. Issues the capture token."""
        now = clock.now()
        codes: list[str] = []
        reasons: list[str] = []
        status = self.ledger.status(order.mandate_id)
        if status in ("revoked", "paused"):
            codes.append(status.upper())
            reasons.append(f"mandate is {status}; refusing to capture")
        if order.status not in ("APPROVED", "CREATED"):
            codes.append("BAD_ORDER_STATE")
            reasons.append(f"order is {order.status}")
        d = GuardDecision("deny" if codes else "allow", codes or ["OK"], reasons or ["capture authorised"])
        d.custom_id = order.chain_digest
        if not codes:
            d.token = GatewayToken.issue(
                self.secret,
                purpose="capture",
                mandate_id=order.mandate_id,
                custom_id=order.chain_digest,
                amount_minor=order.amount_minor,
                currency=order.currency,
                merchant_id=order.merchant_id,
                now=now,
            )
        self.audit.append(
            "guard",
            "guard.capture_check",
            {"order_id": order.id, "mandate_id": order.mandate_id, "verdict": d.verdict, "codes": d.codes},
        )
        return d

    def issue_token(self, purpose: str, order: Order, amount_minor: int | None = None) -> GatewayToken:
        return GatewayToken.issue(
            self.secret,
            purpose=purpose,
            mandate_id=order.mandate_id,
            custom_id=order.chain_digest,
            amount_minor=order.amount_minor if amount_minor is None else amount_minor,
            currency=order.currency,
            merchant_id=order.merchant_id,
            now=clock.now(),
        )

    def issue_dispute_token(
        self,
        purpose: str,
        *,
        mandate_id: str,
        dispute_id: str,
        amount_minor: int,
        currency: str,
        merchant_id: str,
    ) -> GatewayToken:
        """Authorises one dispute money action (accept-claim / make-offer) for exactly this dispute and amount."""
        if purpose not in ("accept_claim", "make_offer"):
            raise ValueError(f"not a dispute money purpose: {purpose}")
        return GatewayToken.issue(
            self.secret,
            purpose=purpose,
            mandate_id=mandate_id,
            custom_id=dispute_id,
            amount_minor=amount_minor,
            currency=currency,
            merchant_id=merchant_id,
            now=clock.now(),
        )

    # ------------------------------------------------------------------------------------------------
    def _record(self, req: PurchaseRequest, d: GuardDecision) -> None:
        with self.db.session() as s:
            row = AgentDecision(
                mandate_id=req.mandate_id,
                order_id=req.order_id,
                inputs=req.provenance.to_list(),
                plan={
                    **req.plan,
                    "sku": req.sku,
                    "amount_minor": req.amount_minor,
                    "intent_gaps": d.intent_gaps,
                },
                prompt_version=PROMPT_VERSION,
                model=req.model,
                verdict=d.verdict,
                taint_flags=d.taint_flags,
                created_at=int(time.time()) if clock.now() == 0 else clock.now(),
            )
            s.add(row)
            s.flush()
            d.decision_id = row.id
        self.audit.append(
            "guard",
            "guard.decision",
            {
                "mandate_id": req.mandate_id,
                "order_id": req.order_id,
                "merchant_id": req.merchant.id,
                "sku": req.sku,
                "amount_minor": req.amount_minor,
                "verdict": d.verdict,
                "codes": d.codes,
                "reasons": d.reasons[:6],
                "taint_flags": d.taint_flags,
                "custom_id": d.custom_id,
                "decision_id": d.decision_id,
            },
        )
