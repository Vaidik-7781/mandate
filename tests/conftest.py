from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from mandate import clock
from mandate.audit.log import Audit
from mandate.credentials.chain import AgentSigner, IntentSpec, issue_delegation, order_chain_digest
from mandate.credentials.keys import KeyRing
from mandate.db import Database, Mandate, set_db
from mandate.guard.guard import Guard, PurchaseRequest
from mandate.ledger.ledger import Ledger
from mandate.merchant.store import MERCHANTS, MerchantService

SEED = "test-seed"
T0 = 1_790_000_000


@dataclass
class Env:
    db: Database
    ledger: Ledger
    audit: Audit
    merchants: MerchantService
    keys: KeyRing
    guard: Guard
    clk: clock.FrozenClock

    def make_mandate(self, **over) -> Mandate:
        base = dict(
            user_id="alice",
            prompt_summary="running shoes under $120",
            merchants=[MERCHANTS[0].to_dict(), MERCHANTS[1].to_dict()],
            items=[
                {"id": "RF-100", "title": "RunFast Road Racer"},
                {"id": "RF-200", "title": "RunFast Trail Pro"},
                {"id": "TC-310", "title": "TrailCo Ultra Runner"},
            ],
            max_per_txn_minor=12000,
            budget_minor=25000,
            max_quantity=2,
            descriptive={},
        )
        base.update(over)
        occ = base.pop("max_occurrences", None)
        spec = IntentSpec(**base)
        d = issue_delegation(spec, self.keys, clock.now())
        m = Mandate(
            id=uuid.uuid4().hex,
            user_id=spec.user_id,
            chain_digest=d.mandate_digest,
            status="active",
            created_at=d.issued_at,
            expires_at=d.expires_at,
            budget_minor=spec.budget_minor,
            currency=spec.currency,
            max_occurrences=occ,
            intent={"prompt": spec.prompt_summary, "descriptive": spec.descriptive},
            l1=d.l1,
            l2=d.l2,
            ambiguity_flagged=False,
            user_confirmed=True,
        )
        self.ledger.register(m)
        m._delegation = d  # type: ignore[attr-defined]
        return m

    def request(self, m: Mandate, sku: str = "RF-100", qty: int = 1, **over) -> PurchaseRequest:
        deleg = m._delegation  # type: ignore[attr-defined]
        merchant_obj, jwt = self.merchants.create_checkout(sku, qty, clock.now(), order_ref=uuid.uuid4().hex)
        price = self.merchants.get(sku).price_minor * qty
        amount = over.pop("amount_minor", price)
        signed = AgentSigner(self.keys.agent).sign_purchase(
            deleg, merchant_obj, sku, jwt, amount, "USD", clock.now()
        )
        return PurchaseRequest(
            mandate_id=m.id,
            order_id=uuid.uuid4().hex,
            merchant=merchant_obj,
            sku=sku,
            amount_minor=amount,
            currency="USD",
            signed=signed,
            **over,
        )

    def digest(self, m: Mandate, req: PurchaseRequest) -> str:
        return order_chain_digest(m.chain_digest, req.signed.l3a, req.signed.l3b)


@pytest.fixture()
def env() -> Env:
    clk = clock.FrozenClock(T0)
    clock.set_clock(clk)
    db = Database("sqlite://")
    db.create_all()
    set_db(db)
    keys = KeyRing.from_seed(SEED)
    ms = MerchantService(SEED)
    ledger, audit = Ledger(db), Audit(db)
    guard = Guard(db, ledger, audit, keys.issuer.public_key, ms.public_jwks, secret="guard-secret")
    yield Env(db, ledger, audit, ms, keys, guard, clk)
    clock.set_clock(None)
    set_db(None)
