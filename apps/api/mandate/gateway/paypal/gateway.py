"""PayPalGateway: the only code path that moves money.

Invariants enforced here:
* every money call (create order, capture, refund, accept-claim, make-offer) demands a `GatewayToken` the guard
  issued for that exact cart or dispute; the gateway re-derives the expected values from its own arguments;
* amounts are integers in minor units until the PayPal boundary, converted by `money.to_paypal`;
* the order amount is computed from the signed cart items, never from a caller-supplied total;
* every POST carries a deterministic `PayPal-Request-Id`, so retries are safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ... import clock
from ...guard.token import GatewayToken, TokenError
from ...money import from_paypal, to_paypal
from .client import PayPalClient


@dataclass
class CaptureResult:
    capture_id: str
    status: str
    amount_minor: int
    currency: str
    custom_id: str | None
    raw: dict


class PayPalGateway:
    def __init__(self, client: PayPalClient, secret: str) -> None:
        self.client = client
        self._secret = secret

    def _check(self, token: GatewayToken | None, **kw: Any) -> None:
        if token is None:
            raise TokenError("a guard token is required for this call")
        token.check(self._secret, now=clock.now(), **kw)

    # ------------------------------------------------------------------ orders
    def create_order(
        self,
        token: GatewayToken | None,
        *,
        order_id: str,
        custom_id: str,
        currency: str,
        items: list[dict],
        merchant_name: str,
        return_base: str = "",
    ) -> dict:
        """`items` = [{sku, name, quantity, unit_price_minor}] from the merchant-signed cart.
        `return_base` (the app's public URL) gives the PayPal approval page somewhere to return the buyer to."""
        total = sum(int(i["unit_price_minor"]) * int(i["quantity"]) for i in items)
        self._check(token, purpose="create_order", custom_id=custom_id, amount_minor=total, currency=currency)
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": order_id,
                    "custom_id": custom_id,  # base64url SHA-256 of the L3 chain (43 chars)
                    "invoice_id": order_id,
                    "description": f"Agent purchase from {merchant_name}"[:127],
                    "amount": {
                        "currency_code": currency,
                        "value": to_paypal(total, currency),
                        "breakdown": {
                            "item_total": {"currency_code": currency, "value": to_paypal(total, currency)}
                        },
                    },
                    "items": [
                        {
                            "name": i["name"][:127],
                            "sku": i["sku"][:127],
                            "quantity": str(int(i["quantity"])),
                            "category": "PHYSICAL_GOODS",
                            "unit_amount": {
                                "currency_code": currency,
                                "value": to_paypal(int(i["unit_price_minor"]), currency),
                            },
                        }
                        for i in items
                    ],
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                        "brand_name": "Mandate (sandbox)",
                        **(
                            {
                                "return_url": f"{return_base}/?paypal=approved&order={order_id}",
                                "cancel_url": f"{return_base}/?paypal=cancelled&order={order_id}",
                            }
                            if return_base
                            else {}
                        ),
                    }
                }
            },
        }
        return self.client.create_order(body, request_id=f"mandate-create-{custom_id}")

    def get_order(self, paypal_order_id: str) -> dict:
        return self.client.get_order(paypal_order_id)

    def capture(
        self,
        token: GatewayToken | None,
        *,
        paypal_order_id: str,
        custom_id: str,
        amount_minor: int,
        currency: str,
    ) -> CaptureResult:
        self._check(
            token, purpose="capture", custom_id=custom_id, amount_minor=amount_minor, currency=currency
        )
        res = self.client.capture_order(paypal_order_id, request_id=f"mandate-capture-{custom_id}")
        cap = res["purchase_units"][0]["payments"]["captures"][0]
        cur = cap["amount"]["currency_code"]
        got = from_paypal(cap["amount"]["value"], cur)
        if got != amount_minor or cur != currency:  # defence in depth: PayPal charged something else
            raise TokenError(f"captured {got} {cur}, expected {amount_minor} {currency}")
        return CaptureResult(cap["id"], cap["status"], got, cur, cap.get("custom_id"), res)

    def add_tracking(self, paypal_order_id: str, capture_id: str, tracking_number: str, carrier: str) -> dict:
        return self.client.add_tracking(
            paypal_order_id,
            capture_id,
            tracking_number,
            carrier,
            request_id=f"mandate-track-{capture_id}-{tracking_number}",
        )

    def refund(
        self,
        token: GatewayToken | None,
        *,
        capture_id: str,
        custom_id: str,
        amount_minor: int,
        currency: str,
        note: str,
        invoice_id: str | None = None,
        tag: str = "full",
    ) -> dict:
        self._check(
            token, purpose="refund", custom_id=custom_id, amount_minor=amount_minor, currency=currency
        )
        body: dict[str, Any] = {
            "amount": {"currency_code": currency, "value": to_paypal(amount_minor, currency)},
            "note_to_payer": note[:255],
        }
        if invoice_id:
            body["invoice_id"] = invoice_id
        return self.client.refund_capture(capture_id, body, request_id=f"mandate-refund-{capture_id}-{tag}")

    # ------------------------------------------------------------------ disputes
    def get_dispute(self, dispute_id: str) -> dict:
        return self.client.get_dispute(dispute_id)

    def provide_evidence(
        self, dispute_id: str, evidences: list[dict], files: list[tuple[str, bytes, str]]
    ) -> dict:
        return self.client.provide_evidence(dispute_id, evidences, files)

    def send_message(self, dispute_id: str, message: str) -> dict:
        return self.client.send_message(dispute_id, message)

    def accept_claim(
        self,
        token: GatewayToken | None,
        *,
        dispute_id: str,
        amount_minor: int,
        currency: str,
        reason: str,
        note: str,
        claim_type: str | None = None,
    ) -> dict:
        self._check(
            token, purpose="accept_claim", custom_id=dispute_id, amount_minor=amount_minor, currency=currency
        )
        body: dict[str, Any] = {"note": note[:2000], "accept_claim_reason": reason}
        if claim_type:  # only a type the dispute's own allowed_response_options listed
            body["accept_claim_type"] = claim_type
            if claim_type == "PARTIAL_REFUND":
                body["refund_amount"] = {
                    "currency_code": currency,
                    "value": to_paypal(amount_minor, currency),
                }
        return self.client.accept_claim(dispute_id, body)

    def make_offer(
        self,
        token: GatewayToken | None,
        *,
        dispute_id: str,
        amount_minor: int,
        currency: str,
        note: str,
        offer_type: str = "REFUND",
    ) -> dict:
        self._check(
            token, purpose="make_offer", custom_id=dispute_id, amount_minor=amount_minor, currency=currency
        )
        return self.client.make_offer(
            dispute_id,
            {
                "note": note[:2000],
                "offer_amount": {"currency_code": currency, "value": to_paypal(amount_minor, currency)},
                "offer_type": offer_type,
            },
        )

    # sandbox-only (never called outside sandbox / replay)
    def adjudicate(self, dispute_id: str, outcome: str) -> dict:
        return self.client.adjudicate(dispute_id, outcome)

    def require_evidence(self, dispute_id: str, action: str) -> dict:
        return self.client.require_evidence(dispute_id, action)
