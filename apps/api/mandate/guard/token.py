"""Gateway tokens: the guard's authorisation for one exact money call.

Invariant: money calls leave only the gateway, and only with a token the guard issued for that exact cart.
A token is an HMAC over the cart facts (mandate, digest, amount, currency, merchant, purpose); the gateway
re-derives the expected values from its own arguments and refuses on any mismatch or expiry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass


class TokenError(Exception):
    """The token is missing, expired, forged or issued for a different cart."""


def _canon(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class GatewayToken:
    purpose: str  # create_order | capture | refund
    mandate_id: str
    custom_id: str  # order chain digest
    amount_minor: int
    currency: str
    merchant_id: str
    exp: int
    mac: str

    def claims(self) -> dict:
        return {
            "purpose": self.purpose,
            "mandate_id": self.mandate_id,
            "custom_id": self.custom_id,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "merchant_id": self.merchant_id,
            "exp": self.exp,
        }

    @staticmethod
    def issue(
        secret: str,
        *,
        purpose: str,
        mandate_id: str,
        custom_id: str,
        amount_minor: int,
        currency: str,
        merchant_id: str,
        now: int,
        ttl: int = 300,
    ) -> GatewayToken:
        claims = {
            "purpose": purpose,
            "mandate_id": mandate_id,
            "custom_id": custom_id,
            "amount_minor": amount_minor,
            "currency": currency,
            "merchant_id": merchant_id,
            "exp": now + ttl,
        }
        mac = hmac.new(secret.encode(), _canon(claims), hashlib.sha256).hexdigest()
        return GatewayToken(mac=mac, **claims)  # type: ignore[arg-type]

    def check(
        self,
        secret: str,
        *,
        purpose: str,
        custom_id: str,
        amount_minor: int,
        currency: str,
        now: int | None = None,
    ) -> None:
        now = int(time.time()) if now is None else now
        expect = hmac.new(secret.encode(), _canon(self.claims()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, self.mac):
            raise TokenError("token signature invalid")
        if self.exp < now:
            raise TokenError("token expired")
        if self.purpose != purpose:
            raise TokenError(f"token purpose {self.purpose!r} != {purpose!r}")
        if (self.custom_id, self.amount_minor, self.currency) != (custom_id, amount_minor, currency):
            raise TokenError("token was issued for a different cart")
