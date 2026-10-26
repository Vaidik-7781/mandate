"""Thin, retrying httpx client for PayPal REST (Orders v2, Payments v2, Disputes v1, Webhooks).

Behaviour: OAuth client-credentials token cached until shortly before expiry; one automatic re-auth on 401;
retries with backoff on 429/5xx and connection errors; the same `PayPal-Request-Id` is reused across retries so
a retried POST can never create a second order or refund.

This class has no money logic. Money calls go through `PayPalGateway`, which demands a guard token.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from ... import clock


class PayPalError(Exception):
    def __init__(self, status: int, body: Any, message: str = "") -> None:
        self.status = status
        self.body = body if isinstance(body, dict) else {"raw": str(body)}
        self.name = self.body.get("name", "")
        self.details = self.body.get("details", [])
        self.debug_id = self.body.get("debug_id", "")
        super().__init__(message or f"PayPal {status} {self.name}: {self.body.get('message', '')}")

    @property
    def issues(self) -> list[str]:
        return [d.get("issue", "") for d in self.details if isinstance(d, dict)]


class PayPalClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        http: httpx.Client | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id, self.client_secret = client_id, client_secret
        self.http = http or httpx.Client(timeout=30.0)
        self.max_retries = max_retries
        self._sleep = sleep
        self._token: str | None = None
        self._token_exp = 0.0

    # ------------------------------------------------------------------ auth
    def _auth_header(self) -> str:
        if self._token is None or clock.now_f() >= self._token_exp - 60:
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            r = self.http.post(
                f"{self.base_url}/v1/oauth2/token",
                content="grant_type=client_credentials",
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if r.status_code != 200:
                raise PayPalError(r.status_code, _json(r), "OAuth token request failed")
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = clock.now_f() + float(body.get("expires_in", 300))
        return f"Bearer {self._token}"

    # ------------------------------------------------------------------ request
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict | None = None,
        files: Any = None,
        data: dict | None = None,
        request_id: str | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        url = f"{self.base_url}{path}"
        reauthed = False
        attempt = 0
        while True:
            headers = {"Authorization": self._auth_header(), "Accept": "application/json"}
            if request_id and method.upper() == "POST":
                headers["PayPal-Request-Id"] = request_id
            try:
                r = self.http.request(
                    method, url, headers=headers, params=params, json=json_body, files=files, data=data
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= self.max_retries:
                    raise PayPalError(0, {"message": str(exc)}, f"transport error: {exc}") from exc
                attempt += 1
                self._sleep(min(2**attempt * 0.2, 5))
                continue
            if r.status_code == 401 and not reauthed:
                self._token = None
                reauthed = True
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                attempt += 1
                retry_after = r.headers.get("retry-after")
                self._sleep(
                    float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt * 0.2, 5)
                )
                continue
            if r.status_code not in ok:
                raise PayPalError(r.status_code, _json(r))
            if r.status_code == 204 or not r.content:
                return {}
            return r.json()

    # ------------------------------------------------------------------ Orders v2
    def create_order(self, body: dict, request_id: str) -> dict:
        return self.request("POST", "/v2/checkout/orders", json_body=body, request_id=request_id)

    def get_order(self, order_id: str) -> dict:
        return self.request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str, request_id: str) -> dict:
        return self.request(
            "POST", f"/v2/checkout/orders/{order_id}/capture", json_body={}, request_id=request_id
        )

    def add_tracking(
        self, order_id: str, capture_id: str, tracking_number: str, carrier: str, request_id: str
    ) -> dict:
        body = {
            "capture_id": capture_id,
            "tracking_number": tracking_number,
            "carrier": carrier,
            "notify_payer": False,
        }
        return self.request(
            "POST", f"/v2/checkout/orders/{order_id}/track", json_body=body, request_id=request_id
        )

    def patch_tracker(self, order_id: str, tracker_id: str, ops: list[dict]) -> dict:
        return self.request("PATCH", f"/v2/checkout/orders/{order_id}/trackers/{tracker_id}", json_body=ops)

    # ------------------------------------------------------------------ Payments v2
    def refund_capture(self, capture_id: str, body: dict, request_id: str) -> dict:
        return self.request(
            "POST", f"/v2/payments/captures/{capture_id}/refund", json_body=body, request_id=request_id
        )

    # ------------------------------------------------------------------ Disputes v1
    def list_disputes(self, **params: str) -> dict:
        return self.request("GET", "/v1/customer/disputes", params=params or None)

    def get_dispute(self, dispute_id: str) -> dict:
        return self.request("GET", f"/v1/customer/disputes/{dispute_id}")

    def provide_evidence(
        self, dispute_id: str, evidences: list[dict], files: list[tuple[str, bytes, str]]
    ) -> dict:
        """Multipart: an `input` JSON part plus one part per file. Files <10 MB each, 50 MB per case."""
        parts: list[tuple[str, Any]] = [
            ("input", ("input.json", json.dumps({"evidences": evidences}), "application/json"))
        ]
        for i, (name, content, mime) in enumerate(files):
            parts.append((f"file{i + 1}", (name, content, mime)))
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/provide-evidence", files=parts)

    def make_offer(self, dispute_id: str, body: dict) -> dict:
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/make-offer", json_body=body)

    def accept_claim(self, dispute_id: str, body: dict) -> dict:
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/accept-claim", json_body=body)

    def send_message(self, dispute_id: str, message: str) -> dict:
        return self.request(
            "POST", f"/v1/customer/disputes/{dispute_id}/send-message", json_body={"message": message}
        )

    def appeal(self, dispute_id: str, evidences: list[dict], files: list[tuple[str, bytes, str]]) -> dict:
        parts: list[tuple[str, Any]] = [
            ("input", ("input.json", json.dumps({"evidences": evidences}), "application/json"))
        ]
        for i, (name, content, mime) in enumerate(files):
            parts.append((f"file{i + 1}", (name, content, mime)))
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/appeal", files=parts)

    def escalate(self, dispute_id: str, note: str) -> dict:
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/escalate", json_body={"note": note})

    # sandbox-only
    def adjudicate(self, dispute_id: str, outcome: str) -> dict:
        return self.request(
            "POST",
            f"/v1/customer/disputes/{dispute_id}/adjudicate",
            json_body={"adjudication_outcome": outcome},
        )

    def require_evidence(self, dispute_id: str, action: str) -> dict:
        return self.request(
            "POST", f"/v1/customer/disputes/{dispute_id}/require-evidence", json_body={"action": action}
        )

    # ------------------------------------------------------------------ Webhooks (postback verification)
    def verify_webhook_postback(self, headers: dict[str, str], event: dict, webhook_id: str) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        res = self.request("POST", "/v1/notifications/verify-webhook-signature", json_body=body)
        return res.get("verification_status") == "SUCCESS"


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:500]}
