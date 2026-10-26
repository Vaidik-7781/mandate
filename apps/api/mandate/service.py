"""Application service: the flows the API, the demo and the bench all call.

Rule: never call the PayPal gateway inside an open database session (webhooks arrive re-entrantly and write).
Money only moves through `PayPalGateway` with a guard token; this module never builds a payment by itself.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from sqlalchemy import select

from . import clock
from .agents.compiler import CompiledDraft, compile_intent, to_spec
from .agents.dispute import DisputeAgent
from .agents.extractor import extract
from .agents.llm import LLMBackend
from .agents.planner import GullibleAgent, plan_purchase
from .audit.log import Audit
from .audit.receipt import build_receipt, render_receipt_pdf, verify_receipt
from .credentials.chain import (
    AgentSigner,
    Delegation,
    IntentSpec,
    issue_delegation,
    presentations_for,
    verify_purchase,
)
from .credentials.keys import KeyPair, KeyRing, derive_key
from .db import (
    AgentDecision,
    Approval,
    Database,
    Dispute,
    EvidencePacket,
    Mandate,
    Order,
    Registry,
    WebhookEvent,
)
from .gateway.paypal.client import PayPalError
from .gateway.paypal.gateway import PayPalGateway
from .guard.guard import Guard, PurchaseRequest
from .guard.taint import Label, Provenance, ProvInput, evaluate_taint
from .guard.token import TokenError
from .ledger.ledger import Ledger
from .liability.facts import derive_facts
from .liability.matrix import attribute, explain
from .merchant.store import MERCHANTS, MerchantService
from .money import from_paypal
from .settings import Settings


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class MandateService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        keys: KeyRing,
        merchants: MerchantService,
        ledger: Ledger,
        audit: Audit,
        guard: Guard,
        backend: LLMBackend,
        gateway: PayPalGateway,
        disputes: DisputeAgent,
        sim: Any | None = None,
        sim_http: Any | None = None,
    ) -> None:
        self.cfg, self.db, self.keys, self.merchants = settings, db, keys, merchants
        self.ledger, self.audit, self.guard, self.backend = ledger, audit, guard, backend
        self.gw, self.disputes, self.sim, self.sim_http = gateway, disputes, sim, sim_http
        self.signer = AgentSigner(keys.agent)
        # Per-mandate lock: a capture and a revoke for the same mandate never interleave, so money is never
        # captured after the revocation sweep has already looked at the order (see tests/test_flows.py).
        self._locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)
        self._locks_guard = threading.Lock()
        self.receipt_key: KeyPair = derive_key(settings.secret_seed, "receipt", "mandate-receipt-key-1")

    def _mandate_lock(self, mandate_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks[mandate_id]

    # =========================================================================================
    # setup
    # =========================================================================================
    def seed_registry(self) -> None:
        with self.db.session() as s:
            for m in MERCHANTS:
                if s.get(Registry, m.id) is None:
                    s.add(Registry(merchant_id=m.id, name=m.name, website=m.website,
                                   status="blocked" if m.id == "m-runfast-outlet" else "allowed"))  # fmt: skip

    def _merchant_dicts(self) -> list[dict]:
        with self.db.session() as s:
            blocked = {
                r.merchant_id
                for r in s.execute(select(Registry).where(Registry.status == "blocked")).scalars()
            }
        return [m.to_dict() for m in self.merchants.merchants.values() if m.id not in blocked]

    def _catalog_dicts(self) -> list[dict]:
        return [
            {"sku": p.sku, "name": p.name, "merchant_id": p.merchant_id, "category": p.category,
             "price_minor": p.price_minor, "attributes": p.attributes}
            for p in self.merchants.catalog.values()
        ]  # fmt: skip

    # =========================================================================================
    # 1. compile intent -> 2. sign mandate
    # =========================================================================================
    def compile(self, user_id: str, prompt: str, answers: dict | None = None) -> dict:
        ci, res = compile_intent(
            self.backend,
            prompt,
            now=clock.now(),
            merchants=self._merchant_dicts(),
            categories=sorted({p.category for p in self.merchants.catalog.values()}),
            catalog=self._catalog_dicts(),
            answers=answers,
        )
        out = ci.to_dict()
        out["model"] = res.model
        out["tokens"] = res.tokens_in + res.tokens_out
        self.audit.append(
            "compiler", "intent.compiled", {"user": user_id, "ready": ci.ready, "source": ci.source}
        )
        return out

    def create_mandate(
        self,
        user_id: str,
        prompt: str,
        draft: dict,
        selected_skus: list[str],
        confirmed: bool,
        ambiguity_flagged: bool = False,
        max_occurrences: int | None = None,
    ) -> dict:
        if not confirmed:
            raise ServiceError("the user must confirm the compiled fields before anything is signed")
        d = CompiledDraft.model_validate(draft)
        if d.questions:
            raise ServiceError("unanswered questions remain: " + ", ".join(q.field for q in d.questions))
        selected: list[dict] = []
        for sku in selected_skus:
            p = self.merchants.get(sku)
            if p is None:
                raise ServiceError(f"unknown item {sku}")
            if d.category and p.category != d.category:
                raise ServiceError(f"{sku} is not in category {d.category}")
            if d.merchant_ids and p.merchant_id not in d.merchant_ids:
                raise ServiceError(f"{sku} is sold by a merchant you did not allow")
            selected.append({"id": p.sku, "title": p.name})
        if not selected:
            raise ServiceError("choose at least one specific item: an empty item list would allow anything")
        try:
            spec = to_spec(user_id, prompt, d, selected, self._merchant_dicts())
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        spec.max_occurrences = max_occurrences
        now = clock.now()
        deleg = issue_delegation(spec, self.keys, now)
        m = Mandate(
            id="mnd_" + uuid.uuid4().hex[:12],
            user_id=user_id,
            chain_digest=deleg.mandate_digest,
            status="active",
            created_at=deleg.issued_at,
            expires_at=deleg.expires_at,
            budget_minor=spec.budget_minor,
            currency=spec.currency,
            max_occurrences=max_occurrences,
            intent={
                "prompt": prompt,
                "descriptive": spec.descriptive,
                "allowed_skus": [i["id"] for i in selected],
                "merchant_ids": [x["id"] for x in spec.merchants],
                "max_per_txn_minor": spec.max_per_txn_minor,
                "max_quantity": spec.max_quantity,
                "category": d.category,
                "spec": asdict(spec),
            },
            l1=deleg.l1,
            l2=deleg.l2,
            ambiguity_flagged=ambiguity_flagged,
            user_confirmed=True,
        )
        self.ledger.register(m)
        self.audit.append("user", "mandate.created", {"mandate_id": m.id, "chain_digest": m.chain_digest,
                                                      "budget_minor": m.budget_minor, "items": [i["id"] for i in selected]})  # fmt: skip
        return self.mandate_view(m.id)

    def _delegation(self, m: Mandate) -> Delegation:
        return Delegation(
            m.l1, m.l2, m.chain_digest, m.created_at, m.expires_at, IntentSpec(**m.intent["spec"])
        )

    def mandate_view(self, mandate_id: str) -> dict:
        m = self.ledger.get(mandate_id)
        if m is None:
            raise ServiceError("no such mandate", 404)
        bal = self.ledger.balance(mandate_id)
        i = m.intent
        return {
            "id": m.id,
            "user_id": m.user_id,
            "status": self.ledger.status(m.id),
            "prompt": i.get("prompt"),
            "created_at": m.created_at,
            "expires_at": m.expires_at,
            "revoked_at": m.revoked_at,
            "currency": m.currency,
            "budget_minor": m.budget_minor,
            "max_per_txn_minor": i.get("max_per_txn_minor"),
            "max_quantity": i.get("max_quantity"),
            "allowed_skus": i.get("allowed_skus", []),
            "merchants": [x for x in i.get("spec", {}).get("merchants", [])],
            "descriptive": i.get("descriptive", {}),
            "chain_digest": m.chain_digest,
            "ambiguity_flagged": m.ambiguity_flagged,
            "user_confirmed": m.user_confirmed,
            "balance": asdict(bal) if bal else None,
        }

    def credentials(self, mandate_id: str) -> dict:
        m = self.ledger.get(mandate_id)
        if m is None:
            raise ServiceError("no such mandate", 404)
        return {"l1": m.l1, "l2": m.l2, "chain_digest": m.chain_digest, "vi_commit": self.cfg.vi_commit}

    def list_mandates(self, user_id: str | None = None) -> list[dict]:
        with self.db.session() as s:
            q = select(Mandate.id).order_by(Mandate.created_at.desc())
            if user_id:
                q = q.where(Mandate.user_id == user_id)
            ids = list(s.execute(q).scalars())
        return [self.mandate_view(i) for i in ids]

    # =========================================================================================
    # 3. agent purchase
    # =========================================================================================
    def agent_purchase(self, mandate_id: str, query: str | None = None, quantity: int = 1,
                       hijacked: bool = False) -> dict:  # fmt: skip
        m = self.ledger.get(mandate_id)
        if m is None:
            raise ServiceError("no such mandate", 404)
        intent = m.intent
        q = query or intent.get("category") or intent.get("prompt", "")
        products = self.merchants.search(q)
        prov = Provenance()
        prov.add(Label.USER, "user_prompt", intent.get("prompt", ""), intent.get("prompt", "")[:80])
        pl_intent = {
            "allowed_skus": intent["allowed_skus"],
            "merchant_ids": intent["merchant_ids"],
            "max_per_txn_minor": intent["max_per_txn_minor"],
            "quantity": quantity,
            "descriptive": intent.get("descriptive", {}),
        }
        considered: list[dict] = []
        cands: list[dict] = []
        spans: list[str] = []
        raw_items: list[dict] = []
        tokens = 0
        model = "offline"
        flagged_by_sku: dict[str, bool] = {}
        for p in products:
            res = extract(self.backend, {"sku": p.sku, "name": p.name, "price_minor": p.price_minor,
                                         "description": p.description, "reviews": p.reviews})  # fmt: skip
            tokens += res.tokens_in + res.tokens_out
            model = res.model
            ex = res.data
            flagged_by_sku[p.sku] = bool(ex["suspected_instruction"])
            spans.extend(ex["flagged_spans"])
            cands.append({"sku": p.sku, "title": p.name, "merchant_id": p.merchant_id, "price_minor": p.price_minor,
                          "attributes": p.attributes, "rating_summary": ex["rating_summary"],
                          "suspected_instruction": ex["suspected_instruction"]})  # fmt: skip
            considered.append({"sku": p.sku, "merchant_id": p.merchant_id, "price_minor": p.price_minor,
                               "suspected_instruction": ex["suspected_instruction"]})  # fmt: skip
            raw_items.append({"sku": p.sku, "merchant_id": p.merchant_id, "price_minor": p.price_minor,
                              "attributes": p.attributes, "description": p.description, "reviews": p.reviews})  # fmt: skip
        if hijacked:
            plan = GullibleAgent().plan(pl_intent, raw_items)
            planner_source = "gullible-scripted"
        else:
            plan, pres = plan_purchase(self.backend, pl_intent, cands, spans)
            tokens += pres.tokens_in + pres.tokens_out
            planner_source = pres.source
        plan_dict = {**plan.as_dict(), "considered": considered, "planner": planner_source, "query": q}
        if not plan.chosen_sku:
            self.audit.append("agent", "agent.no_candidate", {"mandate_id": mandate_id, "query": q})
            return {
                "outcome": "no_candidate",
                "reason": "no item satisfies the signed limits",
                "plan": plan_dict,
            }

        product = self.merchants.get(plan.chosen_sku)
        order_id = "ord_" + uuid.uuid4().hex[:12]
        if product is not None:  # the text the decision rests on
            text = " ".join([product.description, *product.reviews])
            prov.add(Label.UNTRUSTED, f"catalog:{product.sku}:text", text, f"{product.sku} description and reviews",
                     flagged=flagged_by_sku.get(product.sku, False), merchant_controlled=True)  # fmt: skip
        if plan.raw_untrusted_read:
            prov.add(Label.UNTRUSTED, "planner:raw:catalog", "raw catalog text read by the planner",
                     flagged=True, merchant_controlled=True)  # fmt: skip
        return self._purchase(
            m, plan.chosen_sku, plan.quantity, order_id, prov, plan_dict, model, False, tokens
        )

    # ---------------------------------------------------------------------------------------
    def _purchase(self, m: Mandate, sku: str, qty: int, order_id: str, prov: Provenance, plan: dict, model: str,
                  human_approved: bool, tokens: int = 0) -> dict:  # fmt: skip
        product = self.merchants.get(sku)
        now = clock.now()
        if product is None:
            return self._blocked(
                m,
                order_id,
                prov,
                plan,
                model,
                "UNKNOWN_ITEM",
                f"item {sku} is not sold by any known merchant",
            )
        merchant_obj, jwt = self.merchants.create_checkout(sku, qty, now, order_ref=order_id)
        amount = product.price_minor * qty
        try:
            signed = self.signer.sign_purchase(
                self._delegation(m), merchant_obj, sku, jwt, amount, product.currency, now
            )
        except ValueError as exc:  # the agent key refuses to sign anything outside the delegation
            return self._blocked(m, order_id, prov, plan, model, "NOT_IN_MANDATE", str(exc))
        req = PurchaseRequest(mandate_id=m.id, order_id=order_id, merchant=merchant_obj, sku=sku, amount_minor=amount,
                              currency=product.currency, signed=signed, provenance=prov, plan=plan, model=model,
                              human_approved=human_approved)  # fmt: skip
        d = self.guard.evaluate(req)
        base = {"decision": d.summary(), "plan": plan, "tokens": tokens, "order_id": order_id}
        if d.verdict == "deny":
            return {"outcome": "denied", **base}
        if d.verdict == "step_up":
            aid = "appr_" + uuid.uuid4().hex[:12]
            with self.db.session() as s:
                s.add(Approval(id=aid, mandate_id=m.id, kind="purchase",
                               payload={"order_id": order_id, "mandate_id": m.id, "sku": sku, "quantity": qty,
                                        "amount_minor": amount, "plan": plan, "model": model,
                                        "provenance": prov.to_list(), "merchant_id": merchant_obj.id},
                               reasons=d.reasons, status="pending", created_at=clock.now()))  # fmt: skip
            self.audit.append(
                "guard", "approval.queued", {"approval_id": aid, "order_id": order_id, "reasons": d.reasons}
            )
            return {"outcome": "step_up", "approval_id": aid, **base}
        # allow
        assert d.token is not None and d.custom_id is not None and d.report is not None
        items = d.report.checkout_payload["cart"]["items"]
        with self.db.session() as s:
            s.add(Order(id=order_id, mandate_id=m.id, chain_digest=d.custom_id, merchant_id=merchant_obj.id,
                        amount_minor=amount, currency=product.currency, status="CREATED", items=items,
                        checkout_jwt=jwt, l3a=signed.l3a, l3b=signed.l3b, created_at=now))  # fmt: skip
        try:
            res = self.gw.create_order(
                d.token, order_id=order_id, custom_id=d.custom_id, currency=product.currency,
                items=[{"sku": i["sku"], "name": i["name"], "quantity": i["quantity"],
                        "unit_price_minor": i["unit_price_minor"]} for i in items],
                merchant_name=merchant_obj.name,
                return_base=self.cfg.public_base_url.rstrip("/"),
            )  # fmt: skip
        except (PayPalError, TokenError) as exc:
            self.ledger.release(m.id, amount, order_id)
            self._set_status(order_id, "FAILED")
            self.audit.append(
                "gateway", "order.create_failed", {"order_id": order_id, "error": str(exc)[:300]}
            )
            return {"outcome": "error", "error": str(exc)[:300], **base}
        with self.db.session() as s:
            o = s.get(Order, order_id)
            assert o is not None
            o.paypal_order_id = res["id"]
        approve = next(
            (link["href"] for link in res.get("links", []) if link.get("rel") in ("approve", "payer-action")),
            None,
        )
        self.audit.append("gateway", "order.created", {"order_id": order_id, "paypal_order_id": res["id"],
                                                      "custom_id": d.custom_id, "amount_minor": amount})  # fmt: skip
        return {"outcome": "created", "order": self.order_view(order_id), "approve_url": approve, **base}

    def _blocked(
        self, m: Mandate, order_id: str, prov: Provenance, plan: dict, model: str, code: str, why: str
    ) -> dict:
        flags = evaluate_taint(prov, plan).flags
        with self.db.session() as s:
            s.add(AgentDecision(mandate_id=m.id, order_id=order_id, inputs=prov.to_list(),
                                plan={**plan, "sku": plan.get("chosen_sku"), "blocked_at": "signing", "reason": why},
                                prompt_version="", model=model, verdict="deny", taint_flags=flags,
                                created_at=clock.now()))  # fmt: skip
        self.audit.append("guard", "agent.signing_refused", {"mandate_id": m.id, "order_id": order_id, "code": code,
                                                            "reason": why[:200], "taint_flags": flags})  # fmt: skip
        return {"outcome": "blocked", "decision": {"verdict": "deny", "codes": [code], "reasons": [why],
                                                   "taint_flags": flags}, "plan": plan, "order_id": order_id}  # fmt: skip

    def _set_status(self, order_id: str, status: str) -> None:
        with self.db.session() as s:
            o = s.get(Order, order_id)
            if o:
                o.status = status

    # ---------------------------------------------------------------------------------------
    def decide_approval(self, approval_id: str, approve: bool) -> dict:
        with self.db.session() as s:
            a = s.get(Approval, approval_id)
            if a is None:
                raise ServiceError("no such approval", 404)
            kind, status, payload = a.kind, a.status, dict(a.payload)
        if kind == "dispute":
            try:
                return self.disputes.resolve_approval(approval_id, approve)
            except KeyError as exc:
                raise ServiceError("no such approval", 404) from exc
        if status != "pending":
            return {"status": status, "detail": "already decided"}
        with self.db.session() as s:
            a = s.get(Approval, approval_id)
            assert a is not None
            a.status = "approved" if approve else "declined"
            a.decided_at = clock.now()
        self.audit.append("human", "approval.decided", {"approval_id": approval_id, "approved": approve,
                                                        "order_id": payload["order_id"]})  # fmt: skip
        if not approve:
            return {"status": "declined", "detail": "no purchase made"}
        m = self.ledger.get(payload["mandate_id"])
        assert m is not None
        prov = Provenance([ProvInput(Label(i["label"]), i["source"], i["sha256"], i.get("summary", ""),
                                     i.get("flagged", False), i.get("merchant_controlled", False))
                           for i in payload["provenance"]])  # fmt: skip
        out = self._purchase(m, payload["sku"], payload["quantity"], payload["order_id"], prov, payload["plan"],
                             payload.get("model", "offline"), True)  # fmt: skip
        return {"status": "approved", **out}

    def list_approvals(self, status: str | None = None) -> list[dict]:
        with self.db.session() as s:
            q = select(Approval).order_by(Approval.created_at.desc())
            if status:
                q = q.where(Approval.status == status)
            return [{"id": a.id, "kind": a.kind, "status": a.status, "mandate_id": a.mandate_id, "reasons": a.reasons,
                     "payload": {k: v for k, v in a.payload.items() if k not in ("provenance",)},
                     "created_at": a.created_at, "decided_at": a.decided_at} for a in s.execute(q).scalars()]  # fmt: skip

    # =========================================================================================
    # 4. buyer approval (simulated in replay), capture, delivery
    # =========================================================================================
    def simulate_buyer_approval(self, order_id: str) -> dict:
        """Replay mode only: stands in for the buyer approving in the PayPal popup (JS SDK v6 onApprove)."""
        if self.sim_http is None:
            raise ServiceError("buyer approval happens in the PayPal popup outside replay mode", 409)
        o = self._order(order_id)
        if not o.paypal_order_id:
            raise ServiceError("order has no PayPal order yet", 409)
        r = self.sim_http.post(f"/sim/v1/orders/{o.paypal_order_id}/approve")
        if r.status_code >= 400:
            raise ServiceError(f"simulated approval failed: {r.text[:200]}", 502)
        with self.db.session() as s:
            row = s.get(Order, order_id)
            assert row is not None
            if row.status == "CREATED":
                row.status = "APPROVED"
        return self.order_view(order_id)

    def _order(self, order_id: str) -> Order:
        with self.db.session() as s:
            o = s.get(Order, order_id)
        if o is None:
            raise ServiceError("no such order", 404)
        return o

    def capture(self, order_id: str) -> dict:
        o = self._order(order_id)
        with self._mandate_lock(o.mandate_id):
            return self._capture(order_id)

    def _capture(self, order_id: str) -> dict:
        o = self._order(order_id)  # re-read under the lock: a revoke may have just cancelled it
        if o.status == "CAPTURED":
            return {"outcome": "captured", "idempotent": True, "order": self.order_view(order_id)}
        if o.status == "CREATED" and o.paypal_order_id:
            po = self.gw.get_order(o.paypal_order_id)
            if po.get("status") == "APPROVED":
                self._set_status(order_id, "APPROVED")
                o = self._order(order_id)
        if o.status not in ("APPROVED",):
            raise ServiceError(f"order is {o.status}; the buyer has not approved it", 409)
        d = self.guard.authorize_capture(o)
        if not d.allowed:
            self.ledger.release(o.mandate_id, o.amount_minor, o.id)
            self._set_status(order_id, "BLOCKED")
            self.audit.append("guard", "capture.blocked", {"order_id": order_id, "codes": d.codes})
            return {
                "outcome": "blocked",
                "codes": d.codes,
                "reasons": d.reasons,
                "order": self.order_view(order_id),
            }
        assert o.paypal_order_id is not None
        try:
            cap = self.gw.capture(d.token, paypal_order_id=o.paypal_order_id, custom_id=o.chain_digest,
                                  amount_minor=o.amount_minor, currency=o.currency)  # fmt: skip
        except (PayPalError, TokenError) as exc:
            self.audit.append("gateway", "capture.failed", {"order_id": order_id, "error": str(exc)[:300]})
            return {"outcome": "error", "error": str(exc)[:300], "order": self.order_view(order_id)}
        self._mark_captured(order_id, cap.capture_id, cap.amount_minor)
        o = self._order(order_id)
        # the merchant's gatekeeper re-verifies the whole chain before shipping
        gate_ok, gate_why = self._merchant_gatekeeper(o)
        if not gate_ok:
            self.audit.append("merchant", "gatekeeper.rejected", {"order_id": order_id, "why": gate_why})
            self._refund_order(o, "gatekeeper rejected the credentials")
            return {"outcome": "refunded", "reason": gate_why, "order": self.order_view(order_id)}
        sh = self.merchants.ship(o.id)
        try:
            self.gw.add_tracking(o.paypal_order_id or "", cap.capture_id, sh.tracking_number, sh.carrier)
            tracking = {"carrier": sh.carrier, "tracking_number": sh.tracking_number,
                        "tracker_id": f"{cap.capture_id}-{sh.tracking_number}"}  # fmt: skip
        except PayPalError as exc:
            tracking = None
            self.audit.append("gateway", "tracking.failed", {"order_id": order_id, "error": str(exc)[:300]})
        with self.db.session() as s:
            row = s.get(Order, order_id)
            assert row is not None
            row.tracking = tracking
        self.audit.append("merchant", "order.shipped", {"order_id": order_id, "tracking": tracking})
        return {"outcome": "captured", "order": self.order_view(order_id)}

    def _mark_captured(self, order_id: str, capture_id: str, amount_minor: int) -> bool:
        with self.db.session() as s:
            o = s.get(Order, order_id, with_for_update=True)
            if o is None or (o.status == "CAPTURED" and o.capture_id):
                return False
            o.status, o.capture_id, o.captured_amount_minor, o.captured_at = (
                "CAPTURED",
                capture_id,
                amount_minor,
                clock.now(),
            )
            mandate_id = o.mandate_id
        self.ledger.commit(mandate_id, amount_minor, order_id)
        self.audit.append("gateway", "order.captured", {"order_id": order_id, "capture_id": capture_id,
                                                       "amount_minor": amount_minor})  # fmt: skip
        return True

    def _merchant_gatekeeper(self, o: Order) -> tuple[bool, str]:
        with self.db.session() as s:
            m = s.get(Mandate, o.mandate_id)
        if m is None:
            return False, "mandate unknown"
        try:
            pay, chk = presentations_for(m.l2, o.merchant_id, o.items[0]["sku"])
            rep = verify_purchase(l1=m.l1, l2=m.l2, l3a=o.l3a, l3b=o.l3b, l2_payment_presentation=pay,
                                  l2_checkout_presentation=chk, issuer_public_key=self.keys.issuer.public_key,
                                  merchant_public_jwks=self.merchants.public_jwks(), now=o.created_at)  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            return False, f"could not verify: {exc}"
        if not rep.ok:
            return False, "; ".join(rep.errors + rep.violations)[:300]
        if rep.checkout_total_minor != o.captured_amount_minor:
            return False, "captured amount differs from the signed cart"
        return True, "ok"

    def deliver(self, order_id: str) -> dict:
        o = self._order(order_id)
        if o.status != "CAPTURED" or not o.tracking:
            raise ServiceError("only a shipped, captured order can be delivered", 409)
        self.merchants.mark_delivered(order_id)
        assert o.paypal_order_id is not None
        self.gw.client.patch_tracker(o.paypal_order_id, o.tracking["tracker_id"],
                                     [{"op": "replace", "path": "/status", "value": "DELIVERED"}])  # fmt: skip
        with self.db.session() as s:
            row = s.get(Order, order_id)
            assert row is not None
            row.delivered_at = clock.now()
        self.audit.append("merchant", "order.delivered", {"order_id": order_id})
        return self.order_view(order_id)

    # =========================================================================================
    # 5. refunds and the kill switch
    # =========================================================================================
    def _refund_order(self, o: Order, note: str, tag: str = "full") -> dict | None:
        if not o.capture_id:
            return None
        amount = (o.captured_amount_minor or o.amount_minor) - (o.refunded_minor or 0)
        if amount <= 0:
            return None
        tok = self.guard.issue_token("refund", o, amount_minor=amount)
        try:
            res = self.gw.refund(tok, capture_id=o.capture_id, custom_id=o.chain_digest, amount_minor=amount,
                                 currency=o.currency, note=note, invoice_id=o.id, tag=tag)  # fmt: skip
        except (PayPalError, TokenError) as exc:
            self.audit.append("gateway", "refund.failed", {"order_id": o.id, "error": str(exc)[:300]})
            return None
        self._book_refund(o.id, amount, tag=res.get("id", tag))
        return res

    def _book_refund(self, order_id: str, amount_minor: int, tag: str) -> bool:
        """Idempotent per (order, tag): the same refund seen via the API response and via the webhook counts once."""
        with self.db.session() as s:
            o = s.get(Order, order_id)
            if o is None:
                return False
            mandate_id = o.mandate_id
        if not self.ledger.refund(mandate_id, amount_minor, order_id, tag=tag):
            return False
        with self.db.session() as s:
            o = s.get(Order, order_id)
            assert o is not None
            o.refunded_minor = (o.refunded_minor or 0) + amount_minor
            total = o.captured_amount_minor or o.amount_minor
            if o.refunded_minor >= total:
                o.status = "REFUNDED"
        self.audit.append(
            "ledger", "refund.booked", {"order_id": order_id, "amount_minor": amount_minor, "tag": tag}
        )
        return True

    def revoke(self, mandate_id: str, reason: str = "user") -> dict:
        with self._mandate_lock(mandate_id):
            return self._revoke(mandate_id, reason)

    def _revoke(self, mandate_id: str, reason: str = "user") -> dict:
        ts = self.ledger.revoke(mandate_id, reason)
        if ts is None:
            raise ServiceError("no such mandate", 404)
        self.audit.append(
            "user", "mandate.revoked", {"mandate_id": mandate_id, "reason": reason, "revoked_at": ts}
        )
        cancelled: list[str] = []
        refunded: list[str] = []
        failed: list[str] = []
        with self.db.session() as s:
            orders = list(s.execute(select(Order).where(Order.mandate_id == mandate_id)).scalars())
        for o in orders:
            if o.status in ("CREATED", "APPROVED"):
                self.ledger.release(mandate_id, o.amount_minor, o.id)
                self._set_status(o.id, "CANCELLED")
                cancelled.append(o.id)
            elif o.status == "CAPTURED" and o.delivered_at is None:
                res = self._refund_order(o, "authorisation revoked before delivery", tag="revoke")
                (refunded if res else failed).append(o.id)
        with self.db.session() as s:  # pending step-ups for this mandate die with it
            for a in s.execute(
                select(Approval).where(Approval.mandate_id == mandate_id, Approval.status == "pending")
            ).scalars():
                a.status, a.decided_at = "declined", clock.now()
        self.audit.append("ledger", "mandate.revocation_swept",
                          {"mandate_id": mandate_id, "cancelled": cancelled, "refunded": refunded, "failed": failed})  # fmt: skip
        return {"mandate_id": mandate_id, "revoked_at": ts, "cancelled_orders": cancelled, "refunded_orders": refunded,
                "refund_failures": failed}  # fmt: skip

    # =========================================================================================
    # 6. webhooks
    # =========================================================================================
    def process_event(self, event_id: str) -> str:
        with self.db.session() as s:
            ev = s.get(WebhookEvent, event_id)
            if ev is None:
                return "missing"
            if ev.status == "processed":
                return "duplicate"
            event = json.loads(ev.raw_body)
        status = "processed"
        try:
            self._apply_event(event)
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            self.audit.append(
                "receiver",
                "webhook.failed",
                {"event_id": event_id, "error": f"{type(exc).__name__}: {exc}"[:300]},
            )
        with self.db.session() as s:
            ev = s.get(WebhookEvent, event_id)
            assert ev is not None
            ev.status, ev.processed_at = status, clock.now()
        return status

    def _order_for(self, resource: dict) -> Order | None:
        with self.db.session() as s:
            if resource.get("custom_id"):
                o = s.execute(
                    select(Order).where(Order.chain_digest == resource["custom_id"])
                ).scalar_one_or_none()
                if o:
                    return o
            if resource.get("invoice_id"):
                o = s.get(Order, resource["invoice_id"])
                if o:
                    return o
            cap_id = _capture_from_links(resource)
            if cap_id:
                return s.execute(select(Order).where(Order.capture_id == cap_id)).scalar_one_or_none()
            if resource.get("id"):
                return s.execute(
                    select(Order).where(Order.paypal_order_id == resource["id"])
                ).scalar_one_or_none()
        return None

    def _apply_event(self, event: dict) -> None:
        et, res = event["event_type"], event.get("resource", {})
        if et == "CHECKOUT.ORDER.APPROVED":
            o = self._order_for(res)
            if o and o.status == "CREATED":
                self._set_status(o.id, "APPROVED")
        elif et == "PAYMENT.CAPTURE.COMPLETED":
            o = self._order_for(res)
            if o:
                cur = res["amount"]["currency_code"]
                self._mark_captured(o.id, res["id"], from_paypal(res["amount"]["value"], cur))
        elif et in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
            o = self._order_for(res)
            if o:
                cur = res["amount"]["currency_code"]
                amt = from_paypal(res["amount"]["value"], cur)
                if self._book_refund(o.id, amt, tag=res["id"]):
                    self._mark_dispute_refund_booked(o.id)
        elif et == "CUSTOMER.DISPUTE.CREATED":
            self.disputes.handle(res["dispute_id"])
        elif et in ("CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED"):
            self._sync_dispute(res, resolved=et.endswith("RESOLVED"))

    def _mark_dispute_refund_booked(self, order_id: str) -> None:
        with self.db.session() as s:
            for d in s.execute(
                select(Dispute).where(Dispute.order_id == order_id, Dispute.status != "RESOLVED")
            ).scalars():
                d.analysis = {**(d.analysis or {}), "refund_booked": True}

    def _sync_dispute(self, res: dict, resolved: bool) -> None:
        did = res["dispute_id"]
        outcome = res.get("dispute_outcome") or {}
        refunded = outcome.get("amount_refunded")
        order_id = None
        book = None
        with self.db.session() as s:
            d = s.get(Dispute, did)
            if d is None:
                return
            d.status = res.get("status", d.status)
            d.updated_at = clock.now()
            if resolved:
                d.outcome = outcome.get("outcome_code")
                order_id = d.order_id
                if refunded and not (d.analysis or {}).get("refund_booked"):
                    book = from_paypal(refunded["value"], refunded["currency_code"])
                    d.analysis = {**(d.analysis or {}), "refund_booked": True}
        if book and order_id:
            self._book_refund(order_id, book, tag=f"dispute:{did}")
        if resolved:
            self.audit.append(
                "receiver", "dispute.resolved", {"dispute_id": did, "outcome": outcome.get("outcome_code")}
            )

    # =========================================================================================
    # 7. disputes (sandbox helpers + agent hooks)
    # =========================================================================================
    def open_sim_dispute(
        self,
        order_id: str,
        reason: str,
        message: str = "",
        amount_minor: int | None = None,
        seller_protection: bool = False,
    ) -> dict:
        if self.sim is None:
            raise ServiceError("buyer disputes are opened in the PayPal sandbox UI outside replay mode", 409)
        o = self._order(order_id)
        if not o.capture_id:
            raise ServiceError("order has no capture to dispute", 409)
        d = self.sim.create_dispute(o.capture_id, reason, message, amount_minor, protection=seller_protection)
        self.sim.deliver()
        return {"dispute_id": d["dispute_id"], "dispute": self.dispute_view(d["dispute_id"])}

    def sim_buyer_message(self, dispute_id: str, message: str) -> dict:
        if self.sim_http is None:
            raise ServiceError("replay mode only", 409)
        r = self.sim_http.post(f"/sim/v1/disputes/{dispute_id}/buyer-message", json={"message": message})
        return r.json()

    def sim_buyer_respond(self, dispute_id: str, accept: bool) -> dict:
        if self.sim_http is None:
            raise ServiceError("replay mode only", 409)
        r = self.sim_http.post(f"/sim/v1/disputes/{dispute_id}/buyer-{'accept' if accept else 'deny'}-offer")
        return {"status_code": r.status_code}

    def adjudicate(self, dispute_id: str) -> dict:
        if self.cfg.paypal_env == "live":
            raise ServiceError("adjudication is sandbox-only", 409)
        out = self.disputes.adjudicate_scripted(dispute_id)
        if self.sim is not None:
            self.sim.deliver()
        return out

    def dispute_view(self, dispute_id: str) -> dict:
        with self.db.session() as s:
            d = s.get(Dispute, dispute_id)
            packets = list(
                s.execute(select(EvidencePacket).where(EvidencePacket.dispute_id == dispute_id)).scalars()
            )
        if d is None:
            raise ServiceError("no such dispute", 404)
        return {
            "dispute_id": d.dispute_id, "order_id": d.order_id, "mandate_id": d.mandate_id, "reason": d.reason,
            "stage": d.stage, "status": d.status, "amount_minor": d.amount_minor, "currency": d.currency,
            "due_time": d.due_time, "fault_split": d.fault_split, "rule_id": d.rule_id, "decision": d.decision,
            "outcome": d.outcome, "analysis": d.analysis,
            "packets": [{"completeness": p.completeness, "sha256": p.sha256, "note": p.note,
                         "submitted_at": p.submitted_at} for p in packets],
            "created_at": d.created_at, "updated_at": d.updated_at,
        }  # fmt: skip

    def list_disputes(self) -> list[dict]:
        with self.db.session() as s:
            ids = list(s.execute(select(Dispute.dispute_id).order_by(Dispute.created_at.desc())).scalars())
        return [self.dispute_view(i) for i in ids]

    # =========================================================================================
    # 8. views: orders, liability, receipts, analytics
    # =========================================================================================
    def order_view(self, order_id: str) -> dict:
        o = self._order(order_id)
        return {
            "id": o.id, "paypal_order_id": o.paypal_order_id, "mandate_id": o.mandate_id, "merchant_id": o.merchant_id,
            "amount_minor": o.amount_minor, "currency": o.currency, "status": o.status, "capture_id": o.capture_id,
            "captured_amount_minor": o.captured_amount_minor, "refunded_minor": o.refunded_minor, "items": o.items,
            "tracking": o.tracking, "chain_digest": o.chain_digest, "created_at": o.created_at,
            "captured_at": o.captured_at, "delivered_at": o.delivered_at,
        }  # fmt: skip

    def list_orders(self, mandate_id: str | None = None) -> list[dict]:
        with self.db.session() as s:
            q = select(Order.id).order_by(Order.created_at.desc())
            if mandate_id:
                q = q.where(Order.mandate_id == mandate_id)
            ids = list(s.execute(q).scalars())
        return [self.order_view(i) for i in ids]

    def decisions(self, mandate_id: str | None = None, limit: int = 100) -> list[dict]:
        with self.db.session() as s:
            q = select(AgentDecision).order_by(AgentDecision.id.desc()).limit(limit)
            if mandate_id:
                q = q.where(AgentDecision.mandate_id == mandate_id)
            return [{"id": d.id, "mandate_id": d.mandate_id, "order_id": d.order_id, "verdict": d.verdict,
                     "taint_flags": d.taint_flags, "inputs": d.inputs, "plan": d.plan, "model": d.model,
                     "prompt_version": d.prompt_version, "created_at": d.created_at} for d in s.execute(q).scalars()]  # fmt: skip

    def liability(self, order_id: str) -> dict:
        o = self._order(order_id)
        b = derive_facts(self.db, self.ledger, o, self.keys.issuer.public_key, self.merchants.public_jwks())
        a = attribute(b.facts)
        return {"order_id": order_id, "attribution": a.to_dict(), "explanation": explain(a), "notes": b.notes,
                "intent_gaps": b.intent_gaps}  # fmt: skip

    def receipt(self, order_id: str) -> dict:
        o = self._order(order_id)
        if o.status not in ("CAPTURED", "REFUNDED") or not o.capture_id:
            raise ServiceError("no receipt until the payment is captured", 409)
        with self.db.session() as s:
            m = s.get(Mandate, o.mandate_id)
            dec = (
                s.execute(
                    select(AgentDecision)
                    .where(AgentDecision.order_id == o.id, AgentDecision.verdict == "allow")
                    .order_by(AgentDecision.id.desc())
                )
                .scalars()
                .first()
            )
        assert m is not None
        checks = [{"check": c, "ok": True} for c in (
            "mandate_active", "chain_valid", "constraints_satisfied", "merchant_not_blocked", "ledger_reserved")] if dec else []  # fmt: skip
        return build_receipt(o, m, checks, self.audit.head(), self.receipt_key)

    def receipt_pdf(self, order_id: str) -> bytes:
        return render_receipt_pdf(self.receipt(order_id))

    def verify_receipt(self, doc: dict) -> bool:
        return verify_receipt(doc, self.receipt_key.public_key, self.receipt_key.kid)

    def analytics(self) -> dict:
        with self.db.session() as s:
            decisions = list(s.execute(select(AgentDecision)).scalars())
            orders = list(s.execute(select(Order)).scalars())
            disputes = list(s.execute(select(Dispute)).scalars())
            mandates = list(s.execute(select(Mandate)).scalars())
        by_verdict: dict[str, int] = {}
        for d in decisions:
            by_verdict[d.verdict] = by_verdict.get(d.verdict, 0) + 1
        taint: dict[str, int] = {}
        for d in decisions:
            for f in d.taint_flags:
                taint[f] = taint.get(f, 0) + 1
        by_decision: dict[str, int] = {}
        rules: dict[str, int] = {}
        for dp in disputes:
            by_decision[dp.decision or "pending"] = by_decision.get(dp.decision or "pending", 0) + 1
            if dp.rule_id:
                rules[dp.rule_id] = rules.get(dp.rule_id, 0) + 1
        captured = [o for o in orders if o.capture_id]
        return {
            "mandates": len(mandates),
            "mandates_active": sum(1 for m in mandates if self.ledger.status(m.id) == "active"),
            "orders": len(orders),
            "captured_volume_minor": sum(o.captured_amount_minor or 0 for o in captured),
            "refunded_volume_minor": sum(o.refunded_minor or 0 for o in orders),
            "guard_verdicts": by_verdict,
            "taint_flags": taint,
            "disputes": len(disputes),
            "dispute_decisions": by_decision,
            "dispute_rules": rules,
            "dispute_rate": round(len(disputes) / len(captured), 3) if captured else 0.0,
            "audit": asdict(self.audit.verify()),
        }


def _capture_from_links(resource: dict) -> str | None:
    for link in resource.get("links", []):
        href = link.get("href", "")
        if link.get("rel") == "up" and "/captures/" in href:
            return href.rsplit("/captures/", 1)[1].split("/")[0].split("?")[0]
    return None
