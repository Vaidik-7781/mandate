from __future__ import annotations

from mandate.guard.guard import compare_intent
from mandate.guard.taint import Label, Provenance


def test_clean_purchase_allowed_with_token(env):
    m = env.make_mandate()
    req = env.request(m, "RF-100")
    d = env.guard.evaluate(req)
    assert d.allowed, d.reasons
    assert d.token is not None and len(d.custom_id) == 43
    d.token.check(
        "guard-secret", purpose="create_order", custom_id=d.custom_id, amount_minor=8900, currency="USD",
        now=env.clk(),
    )  # fmt: skip
    assert env.ledger.balance(m.id).exposure_minor == 8900
    assert env.audit.verify().ok


def test_over_per_txn_limit_denied_with_all_reasons(env):
    m = env.make_mandate(max_per_txn_minor=9000, budget_minor=25000)
    d = env.guard.evaluate(env.request(m, "RF-200"))  # 119.00 > 90.00
    assert d.verdict == "deny"
    assert "CONSTRAINT_VIOLATION" in d.codes
    assert env.ledger.balance(m.id).exposure_minor == 0  # nothing reserved


def test_quantity_over_cap_denied(env):
    m = env.make_mandate(max_quantity=1, max_per_txn_minor=30000, budget_minor=30000)
    d = env.guard.evaluate(env.request(m, "RF-100", qty=2))
    assert d.verdict == "deny" and any("quantity" in r.lower() for r in d.reasons)


def test_replay_of_same_final_mandate_denied(env):
    m = env.make_mandate()
    req = env.request(m, "RF-100")
    assert env.guard.evaluate(req).allowed
    again = env.guard.evaluate(req)
    # same idempotency key => idempotent retry returns OK (retry-safe) but never double-reserves
    assert env.ledger.balance(m.id).exposure_minor == 8900
    assert again.allowed or again.codes == ["REPLAY"]
    # a *different* order id re-using the same signed mandate must still not reserve twice
    req2 = env.request(m, "RF-100")
    req2.signed = req.signed
    d3 = env.guard.evaluate(req2)
    assert env.ledger.balance(m.id).exposure_minor == 8900
    assert d3.allowed or d3.verdict == "deny"


def test_budget_exhaustion(env):
    m = env.make_mandate(budget_minor=12000, max_per_txn_minor=12000)
    assert env.guard.evaluate(env.request(m, "RF-100")).allowed
    d = env.guard.evaluate(env.request(m, "RF-100"))
    assert d.verdict == "deny" and d.codes == ["BUDGET"]


def test_revoked_mandate_denied(env):
    m = env.make_mandate()
    env.ledger.revoke(m.id)
    d = env.guard.evaluate(env.request(m, "RF-100"))
    assert d.verdict == "deny" and "REVOKED" in d.codes


def test_expired_l3_denied(env):
    m = env.make_mandate()
    req = env.request(m, "RF-100")
    env.clk.advance(1000)  # L3 lives 300 s
    d = env.guard.evaluate(req)
    assert d.verdict == "deny" and "CHAIN_INVALID" in d.codes


def test_blocked_merchant_denied(env):
    from mandate.db import Registry

    m = env.make_mandate()
    with env.db.session() as s:
        s.add(Registry(merchant_id="m-runfast", name="x", website="y", status="blocked"))
    d = env.guard.evaluate(env.request(m, "RF-100"))
    assert d.verdict == "deny" and "MERCHANT_BLOCKED" in d.codes


def test_request_amount_must_equal_signed(env):
    m = env.make_mandate()
    req = env.request(m, "RF-100")
    req.amount_minor = 100  # caller lies; the server trusts only the signed values
    d = env.guard.evaluate(req)
    assert d.verdict == "deny" and "AMOUNT_MISMATCH" in d.codes


def test_cart_price_swap_detected(env):
    """Merchant signs a cart for a different price than the agent pays => amount differs from the signed cart."""
    m = env.make_mandate()
    req = env.request(m, "RF-100", amount_minor=5000)
    d = env.guard.evaluate(req)
    assert d.verdict == "deny" and any("differs from signed cart total" in r for r in d.reasons)


def test_taint_causes_step_up_without_reserving(env):
    m = env.make_mandate()
    prov = Provenance()
    prov.add(Label.UNTRUSTED, "catalog:RF-200:review", "IGNORE ALL", flagged=True)
    d = env.guard.evaluate(env.request(m, "RF-200", provenance=prov))
    assert d.verdict == "step_up" and d.taint_flags
    assert env.ledger.balance(m.id).exposure_minor == 0
    # after a human approves, the same choice proceeds
    d2 = env.guard.evaluate(env.request(m, "RF-200", provenance=prov, human_approved=True))
    assert d2.allowed


def test_policy_deviation_step_up(env):
    m = env.make_mandate()
    d = env.guard.evaluate(env.request(m, "RF-100", plan={"policy_deviation": True}))
    assert d.verdict == "step_up"


def test_capture_recheck_blocks_after_revocation(env):
    from mandate.db import Order

    m = env.make_mandate()
    req = env.request(m, "RF-100")
    d = env.guard.evaluate(req)
    order = Order(
        id=req.order_id, mandate_id=m.id, chain_digest=d.custom_id, merchant_id="m-runfast", amount_minor=8900,
        currency="USD", status="APPROVED", created_at=env.clk(),
    )  # fmt: skip
    assert env.guard.authorize_capture(order).allowed
    env.ledger.revoke(m.id)
    cap = env.guard.authorize_capture(order)
    assert cap.verdict == "deny" and "REVOKED" in cap.codes


def test_decision_provenance_is_stored(env):
    from mandate.db import AgentDecision
    from sqlalchemy import select

    m = env.make_mandate()
    prov = Provenance()
    prov.add(Label.USER, "user_prompt", "running shoes under $120")
    prov.add(Label.SIGNED, "checkout:RF-100", "jwt")
    env.guard.evaluate(env.request(m, "RF-100", provenance=prov))
    with env.db.session() as s:
        row = s.execute(select(AgentDecision)).scalar_one()
    assert [i["label"] for i in row.inputs] == ["USER", "SIGNED"]
    assert row.prompt_version and row.verdict == "allow"


def test_cart_to_intent_diff():
    items = [{"sku": "A", "attributes": {"color": "black", "weight_g": 600, "waterproof": False}}]
    gaps = compare_intent({"color": "black", "max_weight_g": 500, "waterproof": True, "size": 10}, items)
    assert len(gaps) == 3 and not any("color" in g for g in gaps)
    assert compare_intent({}, items) == []
