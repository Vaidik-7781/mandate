from __future__ import annotations

import pytest
from mandate.credentials.chain import (
    AgentSigner,
    IntentSpec,
    Merchant,
    issue_delegation,
    order_chain_digest,
    sign_checkout_jwt,
    tamper_flip,
    verify_purchase,
)
from mandate.credentials.keys import KeyRing, merchant_key

NOW = 1_790_000_000
SEED = "unit-test-seed"
M1 = Merchant("m-run", "RunFast Store", "https://runfast.example")
M2 = Merchant("m-bad", "RunFast Outlet", "https://runfast-outlet.example")


def spec(**kw) -> IntentSpec:
    base = dict(
        user_id="alice",
        prompt_summary="running shoes under $120",
        merchants=[M1.to_dict()],
        items=[{"id": "SKU-1", "title": "Trail Shoe"}, {"id": "SKU-2", "title": "Road Shoe"}],
        max_per_txn_minor=12000,
        budget_minor=30000,
        max_quantity=2,
    )
    base.update(kw)
    return IntentSpec(**base)


@pytest.fixture()
def world():
    keys = KeyRing.from_seed(SEED)
    deleg = issue_delegation(spec(), keys, NOW)
    mk = {M1.id: merchant_key(SEED, M1.id), M2.id: merchant_key(SEED, M2.id)}
    jwks = {k: v.public_jwk for k, v in mk.items()}
    return keys, deleg, mk, jwks


def buy(world, merchant=M1, sku="SKU-1", qty=1, price=9900, amount=None, now=NOW + 10):
    keys, deleg, mk, jwks = world
    jwt = sign_checkout_jwt(
        merchant,
        mk[merchant.id],
        [{"sku": sku, "name": "Shoe", "quantity": qty, "unit_price_minor": price}],
        "USD",
        now,
    )
    signed = AgentSigner(keys.agent).sign_purchase(
        deleg, merchant, sku, jwt, amount if amount is not None else price * qty, "USD", now
    )
    return signed


def verify(world, signed, **over):
    keys, deleg, _mk, jwks = world
    args = dict(
        l1=deleg.l1,
        l2=deleg.l2,
        l3a=signed.l3a,
        l3b=signed.l3b,
        l2_payment_presentation=signed.l2_payment_presentation,
        l2_checkout_presentation=signed.l2_checkout_presentation,
        issuer_public_key=keys.issuer.public_key,
        merchant_public_jwks=jwks,
        now=NOW + 20,
    )
    args.update(over)
    return verify_purchase(**args)


def test_happy_path_verifies(world):
    rep = verify(world, buy(world))
    assert rep.chain_valid, rep.errors
    assert rep.ok, rep.violations
    assert rep.amount_minor == 9900 and rep.currency == "USD"
    assert rep.budget_max_minor == 30000


def test_digest_is_43_chars(world):
    signed = buy(world)
    d = order_chain_digest(world[1].mandate_digest, signed.l3a, signed.l3b)
    assert len(d) == 43 and "=" not in d


def test_over_per_txn_limit_is_flagged(world):
    rep = verify(world, buy(world, price=13000))
    assert rep.chain_valid
    assert not rep.ok
    assert any("exceeds maximum" in v for v in rep.violations)


def test_off_list_item_is_flagged(world):
    keys, deleg, mk, jwks = world
    jwt = sign_checkout_jwt(
        M1, mk[M1.id], [{"sku": "SKU-9", "name": "X", "quantity": 1, "unit_price_minor": 5000}], "USD", NOW
    )
    with pytest.raises(ValueError):  # the agent cannot even obtain a disclosure for an unsigned item
        AgentSigner(keys.agent).sign_purchase(deleg, M1, "SKU-9", jwt, 5000, "USD", NOW)


def test_quantity_over_cap_is_flagged(world):
    rep = verify(world, buy(world, qty=3, price=3000))
    assert not rep.ok
    assert any("quantity" in v.lower() for v in rep.violations)


def test_unlisted_merchant_is_flagged(world):
    # agent signs for a merchant that is not in the mandate: the L2 disclosure for it does not exist
    keys, deleg, mk, jwks = world
    jwt = sign_checkout_jwt(
        M2, mk[M2.id], [{"sku": "SKU-1", "name": "X", "quantity": 1, "unit_price_minor": 5000}], "USD", NOW
    )
    with pytest.raises(ValueError):
        AgentSigner(keys.agent).sign_purchase(deleg, M2, "SKU-1", jwt, 5000, "USD", NOW)


def test_amount_differs_from_signed_cart(world):
    rep = verify(world, buy(world, price=9900, amount=9000))
    assert not rep.ok
    assert any("differs from signed cart total" in v for v in rep.violations)


def test_tampered_l3a_fails_chain(world):
    signed = buy(world)
    rep = verify(world, signed, l3a=tamper_flip(signed.l3a))
    assert not rep.chain_valid and rep.errors


def test_tampered_l2_fails_chain(world):
    signed = buy(world)
    rep = verify(world, signed, l2=tamper_flip(world[1].l2))
    assert not rep.chain_valid


def test_wrong_issuer_key_fails(world):
    other = KeyRing.from_seed("some-other-seed")
    rep = verify(world, buy(world), issuer_public_key=other.issuer.public_key)
    assert not rep.chain_valid


def test_unknown_merchant_key_fails(world):
    rep = verify(world, buy(world), merchant_public_jwks={})
    assert not rep.ok
    assert any("no signing key" in v for v in rep.violations)


def test_forged_merchant_signature_fails(world):
    keys, deleg, mk, jwks = world
    bad = verify(world, buy(world), merchant_public_jwks={M1.id: mk[M2.id].public_jwk})
    assert not bad.ok


def test_l3_expiry_is_enforced(world):
    rep = verify(world, buy(world), now=NOW + 10_000)
    assert not rep.ok


def test_l3_expiry_has_no_hidden_leeway(world):
    # the VI library's default 300s skew would otherwise make a 5-minute L3 live for ten
    assert verify(world, buy(world), now=NOW + 200).ok
    assert not verify(world, buy(world), now=NOW + 340).ok


def test_intent_validation_rejects_empty_lists_and_unbounded():
    assert any("empty" in p for p in spec(merchants=[]).validate())
    assert any("any item" in p for p in spec(items=[]).validate())
    assert spec(budget_minor=0).validate()
    assert spec(max_per_txn_minor=50000).validate()  # per-txn above budget
    assert spec().validate() == []


def test_issue_rejects_invalid_spec():
    with pytest.raises(ValueError):
        issue_delegation(spec(merchants=[]), KeyRing.from_seed(SEED), NOW)
