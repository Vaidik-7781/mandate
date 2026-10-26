"""In-process PayPal sandbox simulator (REPLAY mode).

It speaks the same HTTP interface the gateway uses against `api-m.sandbox.paypal.com`: OAuth, Orders v2,
tracking, Payments v2 refunds, Customer Disputes v1 (including the sandbox-only adjudicate / require-evidence
calls) and signed webhooks. The gateway code is identical in both modes; only the base URL differs.

HONESTY: this is a simulator written from PayPal's public API reference, not PayPal. Behaviours I could not
verify against the live sandbox are marked `UNVERIFIED` below and in README "Unverified items". Dispute
adjudication is scripted by the caller (as in PayPal's own sandbox).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import secrets
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import clock
from ..money import MoneyError, from_paypal, to_paypal

SIM_CERT_ID = "CERT-SIM-MANDATE-1"
SIM_CERT_URL = f"https://api.sandbox.paypal.com/v1/notifications/certs/{SIM_CERT_ID}"
MAX_NOTE = 2000
MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 50 * 1024 * 1024
ALLOWED_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif")
# UNVERIFIED: PayPal documents custom_id as a string up to 255 characters; confirm in the live sandbox.
MAX_CUSTOM_ID = 255

Subscriber = Callable[[dict[str, str], bytes], None]


def _iso(ts: float | int | None = None) -> str:
    t = clock.now_f() if ts is None else ts
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id(prefix: str = "", n: int = 17) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(n))


def _err(status: int, name: str, message: str, details: list[dict] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"name": name, "message": message, "debug_id": _id("", 12).lower()}
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status)


@dataclass
class SimState:
    orders: dict[str, dict] = field(default_factory=dict)
    captures: dict[str, dict] = field(default_factory=dict)  # capture id -> capture (+ order id)
    disputes: dict[str, dict] = field(default_factory=dict)
    tokens: dict[str, float] = field(default_factory=dict)
    request_cache: dict[tuple[str, str, str], tuple[int, Any]] = field(default_factory=dict)
    outbox: list[dict] = field(default_factory=list)


class PayPalSim:
    def __init__(
        self,
        client_id: str = "sim-client-id",
        client_secret: str = "sim-client-secret",
        webhook_id: str = "WEBHOOK_ID",
    ) -> None:
        self.client_id, self.client_secret, self.webhook_id = client_id, client_secret, webhook_id
        self.state = SimState()
        self.subscribers: list[Subscriber] = []
        self.delivered: list[dict] = []
        # webhook signing key + self-signed certificate (served at SIM_CERT_URL)
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mandate-paypal-sim")])
        # Wide fixed validity so frozen test clocks and replay runs never see an expired certificate.
        start, end = dt.datetime(2020, 1, 1, tzinfo=dt.UTC), dt.datetime(2100, 1, 1, tzinfo=dt.UTC)
        self.cert_pem: bytes = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(end)
            .sign(self._key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
        )

    # ------------------------------------------------------------------ webhooks
    def subscribe(self, fn: Subscriber) -> None:
        self.subscribers.append(fn)

    def emit(self, event_type: str, resource: dict, resource_type: str) -> dict:
        event = {
            "id": _id("WH-", 20),
            "event_version": "1.0",
            "create_time": _iso(),
            "resource_type": resource_type,
            "event_type": event_type,
            "summary": event_type.replace(".", " ").title(),
            "resource": resource,
        }
        self.state.outbox.append(event)
        return event

    def sign(
        self, body: bytes, transmission_id: str, transmission_time: str, webhook_id: str | None = None
    ) -> str:
        crc = zlib.crc32(body) & 0xFFFFFFFF
        msg = f"{transmission_id}|{transmission_time}|{webhook_id or self.webhook_id}|{crc}".encode()
        sig = self._key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def build_delivery(self, event: dict) -> tuple[dict[str, str], bytes]:
        body = json.dumps(event, separators=(",", ":")).encode()
        tid, ttime = _id("", 16).lower(), _iso()
        headers = {
            "paypal-transmission-id": tid,
            "paypal-transmission-time": ttime,
            "paypal-transmission-sig": self.sign(body, tid, ttime),
            "paypal-cert-url": SIM_CERT_URL,
            "paypal-auth-algo": "SHA256withRSA",
            "content-type": "application/json",
        }
        return headers, body

    def deliver(self) -> int:
        """Deliver every queued event to every subscriber. Returns the number of deliveries."""
        n = 0
        while self.state.outbox:
            event = self.state.outbox.pop(0)
            headers, body = self.build_delivery(event)
            self.delivered.append(event)
            for fn in list(self.subscribers):
                fn(headers, body)
                n += 1
        return n

    def cert_for_url(self, url: str) -> bytes | None:
        return self.cert_pem if url == SIM_CERT_URL else None

    # ------------------------------------------------------------------ domain helpers
    def find_capture(self, capture_id: str) -> dict | None:
        return self.state.captures.get(capture_id)

    def create_dispute(self, capture_id: str, reason: str, message: str = "", amount_minor: int | None = None,
                       stage: str | None = None, protection: bool = False) -> dict:  # fmt: skip
        """Sandbox helper. In the real sandbox the buyer test account opens the case in the Resolution Center, or a
        buyer-side `POST /v1/customer/disputes` (sandbox-only; needs the DISPUTE_CREATE scope and a
        PayPal-Auth-Assertion header)."""
        cap = self.state.captures.get(capture_id)
        if cap is None:
            raise KeyError(f"unknown capture {capture_id}")
        cur = cap["amount"]["currency_code"]
        amt = cap_amt = from_paypal(cap["amount"]["value"], cur)
        if amount_minor is not None:
            amt = min(amount_minor, cap_amt)
        did = _id("PP-D-", 11)
        due = _iso(clock.now_f() + 10 * 86400)
        order = self.state.orders[cap["order_id"]]
        pu = order["purchase_units"][0]
        d: dict[str, Any] = {
            "dispute_id": did,
            "create_time": _iso(),
            "update_time": _iso(),
            "disputed_transactions": [
                {
                    "seller_transaction_id": capture_id,
                    "create_time": cap["create_time"],
                    "transaction_status": "COMPLETED",
                    "gross_amount": cap["amount"],
                    "invoice_number": pu.get("invoice_id", ""),
                    "custom": pu.get("custom_id", ""),
                    "buyer": {"name": "Sandbox Buyer"},
                    "seller": {"merchant_id": "SIM-MERCHANT", "name": "Mandate Demo Store"},
                    "items": [{"item_description": i.get("name", ""), "item_quantity": i.get("quantity", "1")}
                              for i in pu.get("items", [])],
                }
            ],
            "reason": reason,
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_amount": {"currency_code": cur, "value": to_paypal(amt, cur)},
            "dispute_life_cycle_stage": stage or ("INQUIRY" if reason != "UNAUTHORISED" else "CHARGEBACK"),
            "dispute_channel": "INTERNAL",
            "seller_response_due_date": due,
            "messages": [{"posted_by": "BUYER", "time_posted": _iso(), "content": message}] if message else [],
            "evidences": [],
            "offer": None,
            "dispute_outcome": None,
            # SIM-ONLY convenience: PayPal lists requested evidence in the Resolution Center, not on this resource.
            "evidence_requested": _requested_evidence(reason),
            "allowed_response_options": {
                "make_offer": {"offer_types": ["REFUND", "REFUND_WITH_RETURN", "REFUND_WITH_REPLACEMENT",
                                               "REPLACEMENT_WITHOUT_REFUND"]},
                "accept_claim": {"accept_claim_types": ["REFUND", "REFUND_WITH_RETURN", "PARTIAL_REFUND"],
                                 "accept_claim_reasons": ["DID_NOT_SHIP_ITEM", "TOO_TIME_CONSUMING", "LOST_IN_MAIL",
                                                          "NOT_ABLE_TO_WIN", "COMPANY_POLICY", "REASON_NOT_SET"]},
            },
        }  # fmt: skip
        if (
            protection
        ):  # SIM-ONLY: PayPal's Chargeback Protection / seller protection already covers this case
            d["seller_protection_applied"] = True
            d["disputed_transactions"][0]["seller_protection_eligible"] = True
        self.state.disputes[did] = d
        self.emit("CUSTOMER.DISPUTE.CREATED", self.view_dispute(d, summary=True), "dispute")
        return d

    def view_dispute(self, d: dict, summary: bool = False) -> dict:
        v = json.loads(json.dumps(d))
        v["links"] = self._dispute_links(d)
        if summary:
            for k in ("messages", "evidences", "allowed_response_options", "evidence_requested"):
                v.pop(k, None)
        return v

    def _dispute_links(self, d: dict) -> list[dict]:
        base = f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{d['dispute_id']}"
        rels: list[tuple[str, str]] = [("self", "GET")]
        st = d["status"]
        if st == "WAITING_FOR_SELLER_RESPONSE":
            rels += [("provide-evidence", "POST"), ("make-offer", "POST"), ("accept-claim", "POST"),
                     ("send-message", "POST")]  # fmt: skip
            if d["dispute_life_cycle_stage"] == "INQUIRY":
                rels.append(("provide-supporting-info", "POST"))
        elif st == "UNDER_REVIEW":
            rels += [("adjudicate", "POST"), ("require-evidence", "POST"), ("send-message", "POST")]
        elif st == "WAITING_FOR_BUYER_RESPONSE":
            rels += [("send-message", "POST"), ("escalate", "POST")]
        elif st == "OPEN":
            rels += [("send-message", "POST")]
        return [{"href": f"{base}/{r}" if r != "self" else base, "rel": r, "method": m} for r, m in rels]

    def _touch(self, d: dict) -> None:
        d["update_time"] = _iso()

    def _emit_updated(self, d: dict) -> None:
        self.emit("CUSTOMER.DISPUTE.UPDATED", self.view_dispute(d, summary=True), "dispute")

    def _resolve(self, d: dict, outcome_code: str, refund: bool) -> None:
        d["status"] = "RESOLVED"
        cap_id = d["disputed_transactions"][0]["seller_transaction_id"]
        refunded = None
        if refund:
            refunded = d["dispute_amount"]
            cap = self.state.captures.get(cap_id)
            if cap:
                cur = cap["amount"]["currency_code"]
                cap["refunded_minor"] = cap.get("refunded_minor", 0) + from_paypal(refunded["value"], cur)
                self.emit(
                    "PAYMENT.CAPTURE.REVERSED",
                    {"id": _id("REV-", 12), "status": "COMPLETED", "amount": refunded,
                     "links": [{"rel": "up", "href": f".../captures/{cap_id}", "method": "GET"}]},
                    "refund",
                )  # fmt: skip
        d["dispute_outcome"] = {
            "outcome_code": outcome_code,
            **({"amount_refunded": refunded} if refunded else {}),
        }
        self._touch(d)
        self.emit("CUSTOMER.DISPUTE.RESOLVED", self.view_dispute(d, summary=True), "dispute")


# Mirrors the table in PayPal's "Dispute reasons and evidence" page (developer.paypal.com/disputes/reasons-evidence).
EVIDENCE_TYPES_BY_REASON: dict[str, list[str]] = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": ["PROOF_OF_FULFILLMENT", "PROOF_OF_REFUND"],
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": ["OTHER", "PROOF_OF_REFUND"],
    "UNAUTHORISED": ["PROOF_OF_FULFILLMENT", "PROOF_OF_REFUND", "OTHER"],
    "CREDIT_NOT_PROCESSED": ["PROOF_OF_REFUND", "OTHER"],
    "DUPLICATE_TRANSACTION": ["PROOF_OF_REFUND", "OTHER"],
    "INCORRECT_AMOUNT": ["PROOF_OF_REFUND", "OTHER"],
    "PAYMENT_BY_OTHER_MEANS": ["PROOF_OF_REFUND", "OTHER"],
    "CANCELED_RECURRING_BILLING": ["PROOF_OF_REFUND", "OTHER"],
    "OTHER": ["PROOF_OF_REFUND", "OTHER"],
}


def _requested_evidence(reason: str) -> list[str]:
    return EVIDENCE_TYPES_BY_REASON.get(reason, ["OTHER"])


# =====================================================================================================
# FastAPI app
# =====================================================================================================
_MPART_NAME = re.compile(r'name="([^"]*)"(?:; filename="([^"]*)")?')


def create_sim_app(sim: PayPalSim) -> FastAPI:
    app = FastAPI(title="PayPal sandbox simulator (Mandate)", docs_url=None, redoc_url=None)
    S = sim.state

    def bg(tasks: BackgroundTasks) -> None:
        tasks.add_task(sim.deliver)

    def authed(request: Request) -> bool:
        h = request.headers.get("authorization", "")
        if not h.startswith("Bearer "):
            return False
        exp = S.tokens.get(h[7:])
        return bool(exp and exp > clock.now_f())

    def idem(request: Request) -> tuple[str, str, str] | None:
        rid = request.headers.get("paypal-request-id")
        return (request.method, request.url.path, rid) if rid else None

    def replay_or_none(request: Request) -> JSONResponse | None:
        k = idem(request)
        if k and k in S.request_cache:
            status, body = S.request_cache[k]
            return JSONResponse(body, status_code=status, headers={"x-sim-idempotent-replay": "1"})
        return None

    def remember(request: Request, status: int, body: Any) -> None:
        k = idem(request)
        if k:
            S.request_cache[k] = (status, body)

    # -------------------------------------------------------------- OAuth
    @app.post("/v1/oauth2/token")
    async def token(request: Request):
        auth = request.headers.get("authorization", "")
        try:
            cid, _, sec = base64.b64decode(auth.removeprefix("Basic ")).decode().partition(":")
        except Exception:
            return _err(
                401,
                "AUTHENTICATION_FAILURE",
                "Authentication failed due to invalid authentication credentials.",
            )
        form = (await request.body()).decode()
        if cid != sim.client_id or sec != sim.client_secret or "grant_type=client_credentials" not in form:
            return _err(
                401,
                "AUTHENTICATION_FAILURE",
                "Authentication failed due to invalid authentication credentials.",
            )
        tok = "A21AA" + secrets.token_urlsafe(40)
        S.tokens[tok] = clock.now_f() + 32400
        return {"scope": "https://uri.paypal.com/services/payments/payment", "access_token": tok,
                "token_type": "Bearer", "app_id": "APP-SIM", "expires_in": 32400, "nonce": _id("", 20)}  # fmt: skip

    # -------------------------------------------------------------- Orders v2
    @app.post("/v2/checkout/orders")
    async def create_order(request: Request, tasks: BackgroundTasks):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        if (r := replay_or_none(request)) is not None:
            return r
        body = await request.json()
        if body.get("intent") not in ("CAPTURE", "AUTHORIZE"):
            return _err(
                422,
                "INVALID_REQUEST",
                "Request is not well-formed.",
                [{"field": "/intent", "issue": "INVALID_PARAMETER_VALUE"}],
            )
        pus = body.get("purchase_units") or []
        if len(pus) != 1:
            return _err(422, "INVALID_REQUEST", "exactly one purchase unit is supported by the simulator",
                        [{"field": "/purchase_units", "issue": "UNSUPPORTED"}])  # fmt: skip
        pu = pus[0]
        amt = pu.get("amount") or {}
        cur, val = amt.get("currency_code"), amt.get("value")
        try:
            minor = from_paypal(val, cur)  # type: ignore[arg-type]  # raises on bad precision / non-string
        except MoneyError as exc:
            return _err(422, "UNPROCESSABLE_ENTITY", "amount invalid", [{"field": "/purchase_units/0/amount/value",
                                                                         "issue": "DECIMAL_PRECISION", "description": str(exc)}])  # fmt: skip
        if minor <= 0:
            return _err(422, "UNPROCESSABLE_ENTITY", "amount must be positive")
        for fld in ("custom_id", "invoice_id", "reference_id"):
            v = pu.get(fld)
            if v is not None and (not isinstance(v, str) or len(v) > MAX_CUSTOM_ID):
                return _err(422, "INVALID_REQUEST", f"{fld} too long", [{"field": f"/purchase_units/0/{fld}",
                                                                        "issue": "INVALID_STRING_MAX_LENGTH"}])  # fmt: skip
        oid = _id()
        order = {
            "id": oid,
            "intent": body["intent"],
            "status": "CREATED",
            "create_time": _iso(),
            "purchase_units": [pu],
            "payment_source": body.get("payment_source"),
        }
        S.orders[oid] = order
        resp = _order_view(order, created=True)
        remember(request, 201, resp)
        return JSONResponse(resp, status_code=201)

    @app.get("/v2/checkout/orders/{oid}")
    async def get_order(oid: str, request: Request):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        o = S.orders.get(oid)
        return (
            _order_view(o) if o else _err(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
        )

    @app.post("/sim/v1/orders/{oid}/approve")
    async def sim_approve(oid: str, tasks: BackgroundTasks):
        """SIM-ONLY: stands in for the buyer approving in the PayPal popup (JS SDK v6 onApprove)."""
        o = S.orders.get(oid)
        if not o:
            return _err(404, "RESOURCE_NOT_FOUND", "no such order")
        if o["status"] == "CREATED":
            o["status"] = "APPROVED"
            o["payer"] = {
                "name": {"given_name": "Sandbox", "surname": "Buyer"},
                "email_address": "buyer@sandbox.example",
            }
            sim.emit("CHECKOUT.ORDER.APPROVED", _order_view(o), "checkout-order")
        bg(tasks)
        return _order_view(o)

    @app.post("/v2/checkout/orders/{oid}/capture")
    async def capture(oid: str, request: Request, tasks: BackgroundTasks):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        if (r := replay_or_none(request)) is not None:
            return r
        o = S.orders.get(oid)
        if not o:
            return _err(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
        if o["status"] == "COMPLETED":
            return _err(422, "UNPROCESSABLE_ENTITY", "Order already captured.",
                        [{"issue": "ORDER_ALREADY_CAPTURED"}])  # fmt: skip
        if o["status"] != "APPROVED":
            return _err(422, "UNPROCESSABLE_ENTITY", "Order not approved.", [{"issue": "ORDER_NOT_APPROVED"}])
        pu = o["purchase_units"][0]
        cap = {
            "id": _id("CAP", 14), "status": "COMPLETED", "amount": pu["amount"],
            "custom_id": pu.get("custom_id"), "invoice_id": pu.get("invoice_id"), "final_capture": True,
            "create_time": _iso(), "update_time": _iso(), "order_id": oid, "seller_protection": {"status": "ELIGIBLE"},
        }  # fmt: skip
        S.captures[cap["id"]] = cap
        o["status"] = "COMPLETED"
        pu["payments"] = {"captures": [{k: v for k, v in cap.items() if k != "order_id"}]}
        sim.emit("PAYMENT.CAPTURE.COMPLETED", {k: v for k, v in cap.items() if k != "order_id"}, "capture")
        resp = _order_view(o)
        remember(request, 201, resp)
        bg(tasks)
        return JSONResponse(resp, status_code=201)

    @app.post("/v2/checkout/orders/{oid}/track")
    async def track(oid: str, request: Request):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        o = S.orders.get(oid)
        if not o:
            return _err(404, "RESOURCE_NOT_FOUND", "no such order")
        b = await request.json()
        cap = S.captures.get(b.get("capture_id", ""))
        if not cap or cap["order_id"] != oid:
            return _err(422, "UNPROCESSABLE_ENTITY", "capture_id does not belong to this order",
                        [{"issue": "INVALID_CAPTURE_ID"}])  # fmt: skip
        if not b.get("tracking_number"):
            return _err(
                422,
                "INVALID_REQUEST",
                "tracking_number required",
                [{"field": "/tracking_number", "issue": "MISSING_REQUIRED_PARAMETER"}],
            )
        tid = f"{b['capture_id']}-{b['tracking_number']}"
        tr = {"id": tid, "status": "SHIPPED", "items": b.get("items", []), "carrier": b.get("carrier", "OTHER"),
              "tracking_number": b["tracking_number"], "capture_id": b["capture_id"]}  # fmt: skip
        o.setdefault("trackers", []).append(tr)
        cap["tracking"] = tr
        for d in S.disputes.values():  # tracking shows in dispute details
            if d["disputed_transactions"][0]["seller_transaction_id"] == b["capture_id"]:
                d["disputed_transactions"][0]["tracking_info"] = {"carrier_name": tr["carrier"],
                                                                  "tracking_number": tr["tracking_number"]}  # fmt: skip
        return JSONResponse(_order_view(o), status_code=200)

    @app.patch("/v2/checkout/orders/{oid}/trackers/{tid}")
    async def patch_tracker(oid: str, tid: str, request: Request):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        o = S.orders.get(oid)
        tr = next((t for t in (o or {}).get("trackers", []) if t["id"] == tid), None)
        if not tr:
            return _err(404, "RESOURCE_NOT_FOUND", "no such tracker")
        for op in await request.json():
            path = op.get("path", "").lstrip("/")
            if op.get("op") in ("replace", "add") and path in ("status", "carrier", "tracking_number"):
                tr[path] = op["value"]
        if tr["status"] == "DELIVERED":
            sim_cap = S.captures.get(tr["capture_id"])
            if sim_cap:
                sim_cap["delivered"] = True
        return Response(status_code=204)

    # -------------------------------------------------------------- Payments v2: refund
    @app.post("/v2/payments/captures/{cid}/refund")
    async def refund(cid: str, request: Request, tasks: BackgroundTasks):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        if (r := replay_or_none(request)) is not None:
            return r
        cap = S.captures.get(cid)
        if not cap:
            return _err(404, "RESOURCE_NOT_FOUND", "no such capture")
        raw = await request.body()
        b = json.loads(raw) if raw else {}
        cur = cap["amount"]["currency_code"]
        total = from_paypal(cap["amount"]["value"], cur)
        already = cap.get("refunded_minor", 0)
        try:
            amt = from_paypal(b["amount"]["value"], cur) if b.get("amount") else total - already
        except MoneyError as exc:
            return _err(422, "UNPROCESSABLE_ENTITY", str(exc))
        if amt <= 0 or amt + already > total:
            return _err(422, "UNPROCESSABLE_ENTITY", "Refund amount exceeds captured amount.",
                        [{"issue": "REFUND_AMOUNT_EXCEEDED"}])  # fmt: skip
        cap["refunded_minor"] = already + amt
        ref = {"id": _id("REF", 14), "status": "COMPLETED",
               "amount": {"currency_code": cur, "value": to_paypal(amt, cur)},
               "invoice_id": b.get("invoice_id"), "note_to_payer": b.get("note_to_payer")}  # fmt: skip
        sim.emit("PAYMENT.CAPTURE.REFUNDED", ref, "refund")
        remember(request, 201, ref)
        bg(tasks)
        return JSONResponse(ref, status_code=201)

    # -------------------------------------------------------------- Disputes v1
    @app.get("/v1/customer/disputes")
    async def list_disputes(request: Request):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        q = request.query_params
        items = []
        for d in S.disputes.values():
            if q.get("dispute_state") and d["status"] != q["dispute_state"]:
                continue
            if (
                q.get("disputed_transaction_id")
                and d["disputed_transactions"][0]["seller_transaction_id"] != q["disputed_transaction_id"]
            ):
                continue
            items.append(sim.view_dispute(d, summary=True))
        return {"items": items, "links": []}

    @app.get("/v1/customer/disputes/{did}")
    async def get_dispute(did: str, request: Request):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        d = S.disputes.get(did)
        return sim.view_dispute(d) if d else _err(404, "RESOURCE_NOT_FOUND", "no such dispute")

    def _need_link(d: dict, rel: str) -> JSONResponse | None:
        if not any(link["rel"] == rel for link in sim._dispute_links(d)):
            return _err(422, "UNPROCESSABLE_ENTITY", f"Action {rel} is not allowed in status {d['status']}.",
                        [{"issue": "ACTION_NOT_ALLOWED"}])  # fmt: skip
        return None

    @app.post("/v1/customer/disputes/{did}/provide-evidence")
    async def provide_evidence(did: str, request: Request, tasks: BackgroundTasks):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        d = S.disputes.get(did)
        if not d:
            return _err(404, "RESOURCE_NOT_FOUND", "no such dispute")
        if (e := _need_link(d, "provide-evidence")) is not None:
            return e
        ctype = request.headers.get("content-type", "")
        form = await request.form() if "multipart/form-data" in ctype else None
        if form is None:
            return _err(415, "UNSUPPORTED_MEDIA_TYPE", "provide-evidence requires multipart/form-data")
        raw_input = form.get("input")
        if raw_input is None:
            return _err(
                422, "INVALID_REQUEST", "missing 'input' part", [{"issue": "MISSING_REQUIRED_PARAMETER"}]
            )
        text = raw_input if isinstance(raw_input, str) else (await raw_input.read()).decode()
        try:
            inp = json.loads(text)
        except json.JSONDecodeError:
            return _err(400, "MALFORMED_REQUEST_JSON", "input is not valid JSON")
        files = []
        total = 0
        for key, val in form.multi_items():
            if key == "input" or isinstance(val, str):
                continue
            content = await val.read()
            name = val.filename or key
            if not name.lower().endswith(ALLOWED_EXT):
                return _err(
                    422,
                    "UNPROCESSABLE_ENTITY",
                    f"{name}: unsupported file type",
                    [{"issue": "INVALID_FILE_TYPE"}],
                )
            if len(content) > MAX_FILE:
                return _err(
                    422, "UNPROCESSABLE_ENTITY", f"{name}: file exceeds 10 MB", [{"issue": "FILE_TOO_LARGE"}]
                )
            total += len(content)
            files.append(
                {
                    "name": name,
                    "size": len(content),
                    "sha256": __import__("hashlib").sha256(content).hexdigest(),
                }
            )
        if total > MAX_TOTAL:
            return _err(
                422, "UNPROCESSABLE_ENTITY", "case evidence exceeds 50 MB", [{"issue": "FILE_TOO_LARGE"}]
            )
        evs = inp.get("evidences")
        if not evs:
            return _err(
                422,
                "INVALID_REQUEST",
                "evidences required",
                [{"field": "/evidences", "issue": "MISSING_REQUIRED_PARAMETER"}],
            )
        allowed_types = EVIDENCE_TYPES_BY_REASON.get(d["reason"], ["OTHER"])
        for ev in evs:
            if ev.get("evidence_type") not in allowed_types:
                return _err(422, "INVALID_REQUEST", f"evidence_type must be one of {allowed_types}",
                            [{"field": "/evidences/evidence_type", "issue": "INVALID_PARAMETER_VALUE"}])  # fmt: skip
            if len(ev.get("notes", "")) > MAX_NOTE:
                return _err(422, "INVALID_REQUEST", "notes exceed 2000 characters",
                            [{"field": "/evidences/notes", "issue": "INVALID_STRING_MAX_LENGTH"}])  # fmt: skip
            d["evidences"].append({"evidence_type": ev.get("evidence_type", "OTHER"), "notes": ev.get("notes", ""),
                                   "evidence_info": ev.get("evidence_info"), "documents": files,
                                   "source": "SUBMITTED_BY_SELLER", "date": _iso()})  # fmt: skip
        d["status"] = "UNDER_REVIEW"
        sim._touch(d)
        sim._emit_updated(d)
        bg(tasks)
        return {"links": sim._dispute_links(d)}

    @app.post("/v1/customer/disputes/{did}/make-offer")
    async def make_offer(did: str, request: Request, tasks: BackgroundTasks):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        d = S.disputes.get(did)
        if not d:
            return _err(404, "RESOURCE_NOT_FOUND", "no such dispute")
        if (e := _need_link(d, "make-offer")) is not None:
            return e
        b = await request.json()
        cur = d["dispute_amount"]["currency_code"]
        try:
            offered = from_paypal((b.get("offer_amount") or {}).get("value"), cur)  # type: ignore[arg-type]
        except MoneyError as exc:
            return _err(
                422,
                "INVALID_REQUEST",
                str(exc),
                [{"field": "/offer_amount/value", "issue": "DECIMAL_PRECISION"}],
            )
        if offered <= 0 or offered > from_paypal(d["dispute_amount"]["value"], cur):
            return _err(422, "UNPROCESSABLE_ENTITY", "offer amount must be within the disputed amount",
                        [{"issue": "INVALID_OFFER_AMOUNT"}])  # fmt: skip
        if len(b.get("note", "")) > MAX_NOTE:
            return _err(
                422,
                "INVALID_REQUEST",
                "note exceeds 2000 characters",
                [{"issue": "INVALID_STRING_MAX_LENGTH"}],
            )
        d["offer"] = {"buyer_requested_amount": d["dispute_amount"], "seller_offered_amount": b["offer_amount"],
                      "offer_type": b.get("offer_type", "REFUND"), "history": [{"offer_time": _iso(), "actor": "SELLER",
                                                                                "event_type": "PROPOSED", "notes": b.get("note", "")}]}  # fmt: skip
        d["status"] = "WAITING_FOR_BUYER_RESPONSE"
        sim._touch(d)
        sim._emit_updated(d)
        bg(tasks)
        return {"links": sim._dispute_links(d)}

    @app.post("/v1/customer/disputes/{did}/accept-claim")
    async def accept_claim(did: str, request: Request, tasks: BackgroundTasks):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        d = S.disputes.get(did)
        if not d:
            return _err(404, "RESOURCE_NOT_FOUND", "no such dispute")
        if (e := _need_link(d, "accept-claim")) is not None:
            return e
        b = await request.json() if await request.body() else {}
        d["accept_claim_reason"] = b.get("accept_claim_reason", "REASON_NOT_SET")
        sim._resolve(d, "ACCEPTED", refund=True)
        bg(tasks)
        return {"links": sim._dispute_links(d)}

    @app.post("/v1/customer/disputes/{did}/send-message")
    async def send_message(did: str, request: Request):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        d = S.disputes.get(did)
        if not d:
            return _err(404, "RESOURCE_NOT_FOUND", "no such dispute")
        if (e := _need_link(d, "send-message")) is not None:
            return e
        msg = (await request.json()).get("message", "")
        if not msg or len(msg) > MAX_NOTE:
            return _err(
                422,
                "INVALID_REQUEST",
                "message must be 1-2000 characters",
                [{"issue": "INVALID_STRING_MAX_LENGTH"}],
            )
        d["messages"].append({"posted_by": "SELLER", "time_posted": _iso(), "content": msg})
        return {"links": sim._dispute_links(d)}

    @app.post("/v1/customer/disputes/{did}/adjudicate")
    async def adjudicate(did: str, request: Request, tasks: BackgroundTasks):
        """Sandbox-only in PayPal. Requires UNDER_REVIEW and the `adjudicate` link."""
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        d = S.disputes.get(did)
        if not d:
            return _err(404, "RESOURCE_NOT_FOUND", "no such dispute")
        if (e := _need_link(d, "adjudicate")) is not None:
            return e
        outcome = (await request.json()).get("adjudication_outcome")
        if outcome not in ("BUYER_FAVOR", "SELLER_FAVOR"):
            return _err(422, "INVALID_REQUEST", "adjudication_outcome must be BUYER_FAVOR or SELLER_FAVOR")
        sim._resolve(d, "RESOLVED_BUYER_FAVOUR" if outcome == "BUYER_FAVOR" else "RESOLVED_SELLER_FAVOUR",
                     refund=outcome == "BUYER_FAVOR")  # fmt: skip
        bg(tasks)
        return {"links": sim._dispute_links(d)}

    @app.post("/v1/customer/disputes/{did}/require-evidence")
    async def require_evidence(did: str, request: Request, tasks: BackgroundTasks):
        if not authed(request):
            return _err(401, "AUTHENTICATION_FAILURE", "token invalid or expired")
        d = S.disputes.get(did)
        if not d:
            return _err(404, "RESOURCE_NOT_FOUND", "no such dispute")
        if (e := _need_link(d, "require-evidence")) is not None:
            return e
        action = (await request.json()).get("action")
        if action not in ("BUYER_EVIDENCE", "SELLER_EVIDENCE"):
            return _err(422, "INVALID_REQUEST", "action must be BUYER_EVIDENCE or SELLER_EVIDENCE")
        d["status"] = (
            "WAITING_FOR_SELLER_RESPONSE" if action == "SELLER_EVIDENCE" else "WAITING_FOR_BUYER_RESPONSE"
        )
        sim._touch(d)
        sim._emit_updated(d)
        bg(tasks)
        return {"links": sim._dispute_links(d)}

    @app.post("/v1/customer/disputes/{did}/escalate")
    async def escalate(did: str, request: Request, tasks: BackgroundTasks):
        d = S.disputes.get(did)
        if not d:
            return _err(404, "RESOURCE_NOT_FOUND", "no such dispute")
        if (e := _need_link(d, "escalate")) is not None:
            return e
        d["status"] = "UNDER_REVIEW"
        sim._touch(d)
        sim._emit_updated(d)
        bg(tasks)
        return {"links": sim._dispute_links(d)}

    # ---- SIM-ONLY buyer-side controls (real sandbox: use the buyer's Resolution Center)
    @app.post("/sim/v1/disputes")
    async def sim_create_dispute(request: Request, tasks: BackgroundTasks):
        b = await request.json()
        try:
            d = sim.create_dispute(b["capture_id"], b.get("reason", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"),
                                   b.get("message", ""), b.get("amount_minor"), b.get("stage"))  # fmt: skip
        except KeyError as exc:
            return _err(404, "RESOURCE_NOT_FOUND", str(exc))
        bg(tasks)
        return sim.view_dispute(d)

    @app.post("/sim/v1/disputes/{did}/buyer-message")
    async def sim_buyer_message(did: str, request: Request):
        d = S.disputes.get(did)
        if not d:
            return _err(404, "RESOURCE_NOT_FOUND", "no such dispute")
        d["messages"].append(
            {"posted_by": "BUYER", "time_posted": _iso(), "content": (await request.json())["message"]}
        )
        return {"ok": True}

    @app.post("/sim/v1/disputes/{did}/buyer-accept-offer")
    async def sim_accept_offer(did: str, tasks: BackgroundTasks):
        d = S.disputes.get(did)
        if not d or not d.get("offer"):
            return _err(404, "RESOURCE_NOT_FOUND", "no open offer")
        offered = d["offer"]["seller_offered_amount"]
        d["status"] = "RESOLVED"
        d["dispute_outcome"] = {"outcome_code": "RESOLVED_WITH_PAYOUT", "amount_refunded": offered}
        cap_id = d["disputed_transactions"][0]["seller_transaction_id"]
        cap = S.captures.get(cap_id)
        if cap:
            cur = cap["amount"]["currency_code"]
            cap["refunded_minor"] = cap.get("refunded_minor", 0) + from_paypal(offered["value"], cur)
        sim._touch(d)
        sim.emit("CUSTOMER.DISPUTE.RESOLVED", sim.view_dispute(d, summary=True), "dispute")
        bg(tasks)
        return sim.view_dispute(d)

    @app.post("/sim/v1/disputes/{did}/buyer-deny-offer")
    async def sim_deny_offer(did: str, tasks: BackgroundTasks):
        d = S.disputes.get(did)
        if not d or not d.get("offer"):
            return _err(404, "RESOURCE_NOT_FOUND", "no open offer")
        d["status"] = "UNDER_REVIEW"
        sim._touch(d)
        sim._emit_updated(d)
        bg(tasks)
        return sim.view_dispute(d)

    @app.post("/sim/v1/webhooks/deliver")
    async def sim_deliver():
        return {"delivered": sim.deliver()}

    @app.get("/v1/notifications/certs/{cert_id}")
    async def certs(cert_id: str):
        if cert_id != SIM_CERT_ID:
            return _err(404, "RESOURCE_NOT_FOUND", "unknown cert")
        return Response(sim.cert_pem, media_type="application/x-pem-file")

    @app.get("/sim/v1/state")
    async def sim_state():
        return {"orders": len(S.orders), "captures": len(S.captures), "disputes": len(S.disputes),
                "queued_events": len(S.outbox), "delivered_events": len(sim.delivered)}  # fmt: skip

    return app


def _order_view(o: dict, created: bool = False) -> dict:
    base = "https://api-m.sandbox.paypal.com/v2/checkout/orders"
    v = json.loads(json.dumps(o))
    v.pop("trackers", None) if created else None
    links = [{"href": f"{base}/{o['id']}", "rel": "self", "method": "GET"}]
    if o["status"] == "CREATED":
        links += [
            {
                "href": f"https://www.sandbox.paypal.com/checkoutnow?token={o['id']}",
                "rel": "approve",
                "method": "GET",
            },
            {"href": f"{base}/{o['id']}", "rel": "update", "method": "PATCH"},
            {"href": f"{base}/{o['id']}/capture", "rel": "capture", "method": "POST"},
        ]
    elif o["status"] == "APPROVED":
        links.append({"href": f"{base}/{o['id']}/capture", "rel": "capture", "method": "POST"})
    v["links"] = links
    return v
