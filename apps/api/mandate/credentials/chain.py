"""Verifiable Intent chain: issue (demo provider + user wallet), agent signing, and verification.

Built on the pinned `verifiable-intent` reference library (Mastercard, Apache-2.0, draft v0.1). The library does
the SD-JWT and chain cryptography; this module adds:

* the demo credential provider and user wallet that stand in for a real issuer (sandbox only),
* the agent-side signing of the final payment (L3a) and checkout (L3b) mandates,
* a verifier that runs the library's chain checks, resolves selective-disclosure references, runs the library's
  constraint checker in STRICT mode, and then re-checks the money-relevant fields independently so a
  "skipped" branch in the library can never turn into a silent pass.

Amounts are integer minor units. The checkout JWT is merchant-signed (ES256) and its SHA-256 is the
transaction id that ties the payment mandate to the checkout mandate.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import types
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from verifiable_intent import (
    AllowedMerchantConstraint,
    AllowedPayeeConstraint,
    CheckoutL3Mandate,
    CheckoutLineItemsConstraint,
    CheckoutMandate,
    FinalCheckoutMandate,
    FinalPaymentMandate,
    IssuerCredential,
    MandateMode,
    PaymentAmountConstraint,
    PaymentBudgetConstraint,
    PaymentL3Mandate,
    PaymentMandate,
    StrictnessMode,
    UserMandate,
    check_constraints,
    create_layer1,
    create_layer2_autonomous,
    create_layer3_checkout,
    create_layer3_payment,
    verify_chain,
)
from verifiable_intent.crypto.disclosure import (
    build_selective_presentation,
    hash_bytes,
    hash_disclosure,
)
from verifiable_intent.crypto.sd_jwt import SdJwt, decode_sd_jwt, resolve_disclosures
from verifiable_intent.crypto.signing import (
    _b64url_decode,
    _jwt_decode_parts,
    _jwt_encode,
    es256_verify,
    jwk_to_public_key,
)
from verifiable_intent.verification import chain as _vi_chain

from .. import clock
from ..money import MoneyError
from .keys import KeyPair, KeyRing

PROVIDER_ISS = "https://provider.mandate.example"
WALLET_ISS = "https://wallet.mandate.example"
AGENT_AUD = "https://agent.mandate.example"
AGENT_ISS = "https://agent.mandate.example"
PAYPAL_AUD = "https://www.paypal.com"

CHECKOUT_VCT = "mandate.checkout.open.1"
PAYMENT_VCT = "mandate.payment.open.1"
L3_PAYMENT_VCT = "mandate.payment.1"
L3_CHECKOUT_VCT = "mandate.checkout.1"

PAYMENT_INSTRUMENT = {
    "type": "paypal.wallet",
    "id": "paypal-sandbox-buyer",
    "description": "PayPal sandbox buyer account",
}

L1_LIFETIME = 365 * 86400
L3_LIFETIME = 300  # plan: final mandates live about 5 minutes
CLOCK_SKEW = 30  # the VI library defaults to 300s of leeway, which would double an L3's real lifetime


_TIME_LOCK = threading.Lock()


@contextmanager
def _vi_clock(now: int) -> Iterator[None]:
    """The VI library reads wall-clock time internally; route it through our injectable clock."""
    with _TIME_LOCK:
        orig = _vi_chain.time
        _vi_chain.time = types.SimpleNamespace(time=lambda: float(now))  # type: ignore[assignment]
        try:
            yield
        finally:
            _vi_chain.time = orig


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sha256_b64url(text: str) -> str:
    return b64url(hashlib.sha256(text.encode("utf-8")).digest())


# --------------------------------------------------------------------------------------
# Spec objects
# --------------------------------------------------------------------------------------


@dataclass
class Merchant:
    id: str
    name: str
    website: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "website": self.website}


@dataclass
class IntentSpec:
    """The compiled, user-confirmed constraints that get signed into layer 2."""

    user_id: str
    prompt_summary: str
    merchants: list[dict]  # [{id, name, website}]
    items: list[dict]  # [{id, title}] explicit SKUs; an empty list would mean "any item" and is rejected
    max_per_txn_minor: int
    budget_minor: int
    currency: str = "USD"
    min_per_txn_minor: int | None = None
    max_quantity: int = 1
    valid_seconds: int = 7 * 86400
    max_occurrences: int | None = None  # enforced by the ledger (VI leaves it to the network)
    descriptive: dict[str, Any] = field(
        default_factory=dict
    )  # color, size, "waterproof"... informational only

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.merchants:
            problems.append("allowed merchants list is empty (an empty allowlist is unsatisfiable)")
        if not self.items:
            problems.append("no explicit items: an empty acceptable-items list would allow any item")
        for m in self.merchants:
            if not (m.get("id") and m.get("name") and m.get("website")):
                problems.append(f"merchant {m!r} must have id, name and website")
        for i in self.items:
            if not (i.get("id") and i.get("title")):
                problems.append(f"item {i!r} must have id and title")
        for label, v in (("max_per_txn", self.max_per_txn_minor), ("budget", self.budget_minor)):
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                problems.append(f"{label} must be a positive integer in minor units")
        if self.max_per_txn_minor and self.budget_minor and self.max_per_txn_minor > self.budget_minor:
            problems.append("per-transaction max exceeds the total budget")
        if self.min_per_txn_minor is not None and self.min_per_txn_minor > self.max_per_txn_minor:
            problems.append("min exceeds max")
        if self.max_quantity < 1:
            problems.append("max_quantity must be at least 1")
        if not 900 <= self.valid_seconds <= 30 * 86400:
            problems.append("validity must be between 15 minutes and 30 days")
        return problems


@dataclass
class Delegation:
    """L1 + L2 as issued, plus the indexes needed to route selective disclosure."""

    l1: str
    l2: str
    mandate_digest: str
    issued_at: int
    expires_at: int
    spec: IntentSpec

    def parsed_l2(self) -> SdJwt:
        return decode_sd_jwt(self.l2)


# --------------------------------------------------------------------------------------
# Issuance: demo provider + user wallet (sandbox stand-ins)
# --------------------------------------------------------------------------------------


def mandate_digest(l1: str, l2: str) -> str:
    return sha256_b64url(f"{l1}~~{l2}")


def order_chain_digest(mdigest: str, l3a: str, l3b: str) -> str:
    """The 43-character value sent to PayPal as `custom_id` (base64url SHA-256, no padding)."""
    return sha256_b64url(f"{mdigest}.{l3a}.{l3b}")


def issue_delegation(spec: IntentSpec, keys: KeyRing, now: int) -> Delegation:
    problems = spec.validate()
    if problems:
        raise ValueError("invalid intent: " + "; ".join(problems))

    cred = IssuerCredential(
        iss=PROVIDER_ISS,
        sub=spec.user_id,
        iat=now,
        exp=now + L1_LIFETIME,
        aud=WALLET_ISS,
        cnf_jwk=keys.user.public_jwk,
        email=f"{spec.user_id}@users.mandate.example",
        pan_last_four="0000",
        scheme="PayPal",
    )
    l1 = create_layer1(cred, keys.issuer.private_key, kid=keys.issuer.kid)

    expires = now + spec.valid_seconds
    agent_jwk = keys.agent.public_jwk
    checkout_constraints = [
        AllowedMerchantConstraint(allowed=list(spec.merchants)),
        CheckoutLineItemsConstraint(
            items=[{"id": "line-item-1", "acceptable_items": list(spec.items), "quantity": spec.max_quantity}]
        ),
    ]
    payment_constraints = [
        PaymentAmountConstraint(
            currency=spec.currency, min=spec.min_per_txn_minor, max=spec.max_per_txn_minor
        ),
        AllowedPayeeConstraint(allowed=list(spec.merchants)),
        PaymentBudgetConstraint(currency=spec.currency, max=spec.budget_minor),
    ]
    mandate = UserMandate(
        nonce=str(uuid.uuid4()),
        aud=AGENT_AUD,
        iat=now,
        iss=WALLET_ISS,
        exp=expires,
        mode=MandateMode.AUTONOMOUS,
        sd_hash=hash_bytes(l1.serialize().encode("ascii")),
        prompt_summary=spec.prompt_summary,
        checkout_mandate=CheckoutMandate(
            vct=CHECKOUT_VCT, cnf_jwk=agent_jwk, cnf_kid=keys.agent.kid, constraints=checkout_constraints
        ),
        payment_mandate=PaymentMandate(
            vct=PAYMENT_VCT,
            cnf_jwk=agent_jwk,
            cnf_kid=keys.agent.kid,
            payment_instrument=PAYMENT_INSTRUMENT,
            constraints=payment_constraints,
        ),
        merchants=list(spec.merchants),
        acceptable_items=list(spec.items),
    )
    l2 = create_layer2_autonomous(mandate, keys.user.private_key, kid=keys.user.kid)
    l1s, l2s = l1.serialize(), l2.serialize()
    return Delegation(
        l1=l1s, l2=l2s, mandate_digest=mandate_digest(l1s, l2s), issued_at=now, expires_at=expires, spec=spec
    )


# --------------------------------------------------------------------------------------
# Merchant-signed checkout JWT
# --------------------------------------------------------------------------------------


def sign_checkout_jwt(
    merchant: Merchant,
    key: KeyPair,
    lines: list[dict],
    currency: str,
    now: int,
    order_ref: str | None = None,
    ttl: int = 3600,
) -> str:
    """Merchant signs the cart. `lines` = [{sku, name, quantity, unit_price_minor}]; totals are integers."""
    total = sum(int(li["unit_price_minor"]) * int(li["quantity"]) for li in lines)
    payload = {
        "iss": merchant.website,
        "sub": "cart_checkout",
        "iat": now,
        "exp": now + ttl,
        "merchant": merchant.to_dict(),
        "order_ref": order_ref or uuid.uuid4().hex,
        "cart": {
            "items": [
                {
                    "sku": li["sku"],
                    "name": li["name"],
                    "quantity": int(li["quantity"]),
                    "unit_price_minor": int(li["unit_price_minor"]),
                    **({"attributes": li["attributes"]} if li.get("attributes") else {}),
                }
                for li in lines
            ],
            "total_minor": total,
            "currency": currency,
        },
    }
    return _jwt_encode({"alg": "ES256", "typ": "JWT", "kid": key.kid}, payload, key.private_key)


def verify_checkout_jwt(token: str, merchant_public_jwk: dict, now: int) -> dict:
    """Verify signature + expiry of a merchant checkout JWT; returns the payload or raises ValueError."""
    header, payload, sig = _jwt_decode_parts(token)
    if header.get("alg") != "ES256":
        raise ValueError("checkout JWT must be ES256")
    h, p, _ = token.split(".")
    if not es256_verify(f"{h}.{p}".encode("ascii"), sig, jwk_to_public_key(merchant_public_jwk)):
        raise ValueError("checkout JWT signature invalid")
    if payload.get("exp", 0) < now:
        raise ValueError("checkout JWT expired")
    return payload


# --------------------------------------------------------------------------------------
# Agent signing (final payment + checkout mandates)
# --------------------------------------------------------------------------------------


def _find_disclosure(sd: SdJwt, pred) -> str | None:
    for disc, value in zip(sd.disclosures, sd.disclosure_values, strict=False):
        v = value[-1] if value else None
        if isinstance(v, dict) and pred(v):
            return disc
    return None


@dataclass
class SignedPurchase:
    l3a: str
    l3b: str
    l2_payment_presentation: str
    l2_checkout_presentation: str
    checkout_hash: str
    nonce: str


class AgentSigner:
    """Holds the agent key. The model never sees it; every call is made by guarded code."""

    def __init__(self, key: KeyPair) -> None:
        self._key = key

    @property
    def public_jwk(self) -> dict:
        return self._key.public_jwk

    def sign_purchase(
        self,
        delegation: Delegation,
        merchant: Merchant,
        sku: str,
        checkout_jwt: str,
        amount_minor: int,
        currency: str,
        now: int,
    ) -> SignedPurchase:
        l2 = delegation.parsed_l2()
        l2_ser = delegation.l2
        base_jwt = l2_ser.split("~")[0]
        payment_disc = _find_disclosure(l2, lambda v: v.get("vct") == PAYMENT_VCT)
        checkout_disc = _find_disclosure(l2, lambda v: v.get("vct") == CHECKOUT_VCT)
        merchant_disc = _find_disclosure(l2, lambda v: v.get("id") == merchant.id and "website" in v)
        item_disc = _find_disclosure(l2, lambda v: v.get("id") == sku and "title" in v)
        if not (payment_disc and checkout_disc and merchant_disc):
            raise ValueError("delegation is missing required L2 disclosures (payment/checkout/merchant)")
        if not item_disc:
            raise ValueError(f"item {sku} is not in the signed mandate")

        c_hash = sha256_b64url(checkout_jwt)
        nonce = str(uuid.uuid4())
        l3a_m = PaymentL3Mandate(
            nonce=nonce,
            aud=PAYPAL_AUD,
            iat=now,
            iss=AGENT_ISS,
            exp=now + L3_LIFETIME,
            final_payment=FinalPaymentMandate(
                transaction_id=c_hash,
                payee=merchant.to_dict(),
                payment_amount={"currency": currency, "amount": amount_minor},
                payment_instrument=PAYMENT_INSTRUMENT,
            ),
            final_merchant=merchant.to_dict(),
        )
        l3a = create_layer3_payment(
            l3a_m, self._key.private_key, base_jwt, payment_disc, merchant_disc, kid=self._key.kid
        )
        l3b_m = CheckoutL3Mandate(
            nonce=nonce,
            aud=merchant.website,
            iat=now,
            iss=AGENT_ISS,
            exp=now + L3_LIFETIME,
            final_checkout=FinalCheckoutMandate(checkout_jwt=checkout_jwt, checkout_hash=c_hash),
        )
        l3b = create_layer3_checkout(
            l3b_m, self._key.private_key, base_jwt, checkout_disc, item_disc, kid=self._key.kid
        )
        return SignedPurchase(
            l3a=l3a.serialize(),
            l3b=l3b.serialize(),
            l2_payment_presentation=build_selective_presentation(base_jwt, [payment_disc, merchant_disc]),
            l2_checkout_presentation=build_selective_presentation(base_jwt, [checkout_disc, item_disc]),
            checkout_hash=c_hash,
            nonce=nonce,
        )


def presentations_for(l2_serialized: str, merchant_id: str, sku: str) -> tuple[str, str]:
    """Rebuild the role-specific L2 presentations (payment side, checkout side) for a purchase.

    Used when a purchase is re-verified later (merchant gatekeeper, dispute evidence) from stored credentials.
    """
    l2 = decode_sd_jwt(l2_serialized)
    base = l2_serialized.split("~")[0]
    pay = _find_disclosure(l2, lambda v: v.get("vct") == PAYMENT_VCT)
    chk = _find_disclosure(l2, lambda v: v.get("vct") == CHECKOUT_VCT)
    mer = _find_disclosure(l2, lambda v: v.get("id") == merchant_id and "website" in v)
    item = _find_disclosure(l2, lambda v: v.get("id") == sku and "title" in v)
    if not (pay and chk and mer and item):
        raise ValueError("L2 lacks the disclosures needed to rebuild presentations")
    return build_selective_presentation(base, [pay, mer]), build_selective_presentation(base, [chk, item])


# --------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------


@dataclass
class ChainReport:
    """Everything the guard, the liability engine and the evidence packet need to know about a purchase."""

    chain_valid: bool = False
    constraints_satisfied: bool = False
    errors: list[str] = field(default_factory=list)  # chain / crypto failures
    violations: list[str] = field(default_factory=list)  # constraint violations (all collected)
    checks_performed: list[str] = field(default_factory=list)
    merchant: dict = field(default_factory=dict)
    amount_minor: int | None = None
    currency: str | None = None
    transaction_id: str | None = None
    checkout_total_minor: int | None = None
    line_items: list[dict] = field(default_factory=list)
    checkout_payload: dict = field(default_factory=dict)
    agent_kid: str | None = None
    l2_exp: int | None = None
    l3_exp: int | None = None
    budget_max_minor: int | None = None

    @property
    def ok(self) -> bool:
        return self.chain_valid and self.constraints_satisfied and not self.errors and not self.violations


def _delegate(claims: dict, vct: str) -> dict:
    for d in claims.get("delegate_payload", []):
        if isinstance(d, dict) and d.get("vct") == vct:
            return d
    return {}


def _resolve_refs(refs: list, by_hash: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    for ref in refs or []:
        if isinstance(ref, dict) and "..." in ref:
            val = by_hash.get(ref["..."])
            if isinstance(val, dict):
                out.append(val)
        elif isinstance(ref, dict):
            out.append(ref)
    return out


def verify_purchase(
    *,
    l1: str,
    l2: str,
    l3a: str,
    l3b: str,
    l2_payment_presentation: str,
    l2_checkout_presentation: str,
    issuer_public_key: ec.EllipticCurvePublicKey,
    merchant_public_jwks: dict[str, dict],
    now: int | None = None,
) -> ChainReport:
    """Verify a full purchase chain and every constraint. Collects all violations (never stops at the first)."""
    rep = ChainReport()
    now = clock.now() if now is None else now
    try:
        p_l1, p_l2 = decode_sd_jwt(l1), decode_sd_jwt(l2)
        p_l3a, p_l3b = decode_sd_jwt(l3a), decode_sd_jwt(l3b)
    except Exception as exc:  # malformed input is a failure, not a crash
        rep.errors.append(f"credential parse error: {exc}")
        return rep

    try:
        with _vi_clock(now):
            res = verify_chain(
                p_l1,
                p_l2,
                l3_payment=p_l3a,
                l3_checkout=p_l3b,
                issuer_public_key=issuer_public_key,
                l1_serialized=l1,
                l2_serialized=l2,
                l2_payment_serialized=l2_payment_presentation,
                l2_checkout_serialized=l2_checkout_presentation,
                expected_l2_aud=AGENT_AUD,
                expected_l3_payment_aud=PAYPAL_AUD,
                clock_skew_seconds=CLOCK_SKEW,
            )
    except Exception as exc:
        rep.errors.append(f"chain verification raised: {exc}")
        return rep
    rep.chain_valid = res.valid
    rep.errors.extend(res.errors)
    rep.checks_performed = list(res.checks_performed)
    if not res.valid:
        return rep

    l2_claims = resolve_disclosures(p_l2)
    by_hash: dict[str, Any] = {}
    for disc, value in zip(p_l2.disclosures, p_l2.disclosure_values, strict=False):
        by_hash[hash_disclosure(disc)] = value[-1] if value else None

    pay_open = _delegate(l2_claims, PAYMENT_VCT)
    chk_open = _delegate(l2_claims, CHECKOUT_VCT)
    l3a_claims, l3b_claims = res.l3_payment_claims, res.l3_checkout_claims
    pay_final = _delegate(l3a_claims, L3_PAYMENT_VCT)
    chk_final = _delegate(l3b_claims, L3_CHECKOUT_VCT)
    if not (pay_open and chk_open and pay_final and chk_final):
        rep.errors.append("missing open or final mandates after chain verification")
        return rep

    rep.agent_kid = (pay_open.get("cnf", {}).get("jwk", {}) or {}).get("kid")
    rep.l2_exp = p_l2.payload.get("exp")
    rep.l3_exp = p_l3a.payload.get("exp")
    for label, p3 in ("L3a", p_l3a), ("L3b", p_l3b):  # independent of the library's leeway handling
        e3 = p3.payload.get("exp")
        if not isinstance(e3, int | float) or now > e3:
            rep.errors.append(f"{label} expired or has no exp")
    if rep.errors:
        return rep

    # -- resolve selective-disclosure references so the library checker sees real values ----------------
    pay_constraints = json.loads(json.dumps(pay_open.get("constraints", [])))
    chk_constraints = json.loads(json.dumps(chk_open.get("constraints", [])))
    for c in pay_constraints:
        if c.get("type") == "mandate.payment.allowed_payees":
            c["allowed"] = _resolve_refs(c.get("allowed", []), by_hash) or c.get("allowed", [])
    for c in chk_constraints:
        if c.get("type") == "mandate.checkout.allowed_merchants":
            c["allowed"] = _resolve_refs(c.get("allowed", []), by_hash) or c.get("allowed", [])
        if c.get("type") == "mandate.checkout.line_items":
            for entry in c.get("items", []):
                entry["acceptable_items"] = _resolve_refs(
                    entry.get("acceptable_items", []), by_hash
                ) or entry.get("acceptable_items", [])

    # -- checkout JWT: merchant signature, cart, hash binding --------------------------------------------
    jwt = chk_final.get("checkout_jwt", "")
    merchant = pay_final.get("payee", {}) or {}
    rep.merchant = merchant
    try:
        key = merchant_public_jwks.get(merchant.get("id", ""))
        if key is None:
            raise ValueError(f"no signing key on file for merchant {merchant.get('id')!r}")
        payload = verify_checkout_jwt(jwt, key, now)
        rep.checkout_payload = payload
        rep.checkout_total_minor = int(payload["cart"]["total_minor"])
        rep.line_items = [{"id": i["sku"], "quantity": i["quantity"]} for i in payload["cart"]["items"]]
        if payload.get("merchant", {}).get("id") != merchant.get("id"):
            rep.violations.append("checkout JWT merchant differs from the payee in the payment mandate")
        if payload["cart"].get("currency") != pay_final.get("payment_amount", {}).get("currency"):
            rep.violations.append("checkout currency differs from payment currency")
    except (ValueError, KeyError, TypeError, MoneyError) as exc:
        rep.violations.append(f"checkout JWT invalid: {exc}")

    if sha256_b64url(jwt) != chk_final.get("checkout_hash") or pay_final.get(
        "transaction_id"
    ) != chk_final.get("checkout_hash"):
        rep.violations.append("transaction_id does not equal the checkout hash")

    amount = pay_final.get("payment_amount", {})
    rep.amount_minor = amount.get("amount") if isinstance(amount.get("amount"), int) else None
    rep.currency = amount.get("currency")
    rep.transaction_id = pay_final.get("transaction_id")
    if rep.checkout_total_minor is not None and rep.amount_minor != rep.checkout_total_minor:
        rep.violations.append(
            f"payment amount {rep.amount_minor} differs from signed cart total {rep.checkout_total_minor}"
        )

    # -- library constraint checker (STRICT) -------------------------------------------------------------
    allowed_pay: list = next(
        (c["allowed"] for c in pay_constraints if c.get("type") == "mandate.payment.allowed_payees"), []
    )
    pay_fulfil = dict(pay_final)
    pay_fulfil["allowed_merchants"] = [m for m in allowed_pay if isinstance(m, dict)]
    r1 = check_constraints(pay_constraints, pay_fulfil, mode=StrictnessMode.STRICT, is_open_mandate=True)
    rep.violations.extend(r1.violations)

    allowed_chk: list = next(
        (c["allowed"] for c in chk_constraints if c.get("type") == "mandate.checkout.allowed_merchants"), []
    )
    chk_fulfil = {
        "merchant": merchant,
        "allowed_merchants": [m for m in allowed_chk if isinstance(m, dict)],
        "line_items": rep.line_items,
    }
    r2 = check_constraints(chk_constraints, chk_fulfil, mode=StrictnessMode.STRICT, is_open_mandate=True)
    rep.violations.extend(r2.violations)

    # -- independent re-checks (so a library "skip" path can never become a silent pass) -----------------
    def _allowed_ids(lst: Any) -> set[str]:
        return {str(m["id"]) for m in lst if isinstance(m, dict) and m.get("id")}

    if merchant.get("id") not in _allowed_ids(pay_fulfil["allowed_merchants"]):
        rep.violations.append(f"payee {merchant.get('id')!r} is not in the signed payee allowlist")
    if merchant.get("id") not in _allowed_ids(chk_fulfil["allowed_merchants"]):
        rep.violations.append(f"merchant {merchant.get('id')!r} is not in the signed merchant allowlist")
    for allowed in (pay_fulfil["allowed_merchants"], chk_fulfil["allowed_merchants"]):
        match = [m for m in allowed if m.get("id") == merchant.get("id")]
        if match and match[0].get("website") != merchant.get("website"):
            rep.violations.append("merchant website differs from the signed allowlist entry")
    for c in pay_constraints:
        if c.get("type") == "mandate.payment.budget":
            rep.budget_max_minor = c.get("max")
        if c.get("type") == "mandate.payment.amount_range" and rep.currency != c.get("currency"):
            rep.violations.append("payment currency differs from the signed currency")

    rep.constraints_satisfied = not rep.violations
    return rep


def tamper_flip(serialized: str) -> str:
    """Test helper: flip one character in the signature of the first JWT of an SD-JWT string."""
    parts = serialized.split("~")
    jwt = parts[0].split(".")
    sig = list(jwt[2])
    sig[2] = "A" if sig[2] != "A" else "B"
    jwt[2] = "".join(sig)
    parts[0] = ".".join(jwt)
    return "~".join(parts)


__all__ = [
    "AgentSigner", "ChainReport", "Delegation", "IntentSpec", "Merchant", "SignedPurchase",
    "issue_delegation", "mandate_digest", "order_chain_digest", "sha256_b64url", "sign_checkout_jwt",
    "verify_checkout_jwt", "verify_purchase", "presentations_for", "tamper_flip", "b64url", "_b64url_decode",
]  # fmt: skip
