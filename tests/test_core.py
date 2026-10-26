from __future__ import annotations

import dataclasses

import pytest
from mandate.db import AuditLog
from mandate.guard.taint import Label, Provenance, evaluate_taint
from mandate.guard.token import GatewayToken, TokenError
from mandate.liability.matrix import Facts, attribute, explain
from mandate.money import MoneyError, from_paypal, to_paypal
from sqlalchemy import update


# ------------------------------------------------------------------ money
@pytest.mark.parametrize(
    "minor,cur,text",
    [
        (27999, "USD", "279.99"),
        (0, "USD", "0.00"),
        (5, "USD", "0.05"),
        (1000, "JPY", "1000"),
        (100, "USD", "1.00"),
    ],
)
def test_money_roundtrip(minor, cur, text):
    assert to_paypal(minor, cur) == text
    assert from_paypal(text, cur) == minor


@pytest.mark.parametrize("bad", ["1.234", "abc", "-1.00", "NaN", "1e2x"])
def test_money_rejects(bad):
    with pytest.raises(MoneyError):
        from_paypal(bad, "USD")


def test_money_rejects_floats_and_negatives():
    with pytest.raises(MoneyError):
        to_paypal(1.5, "USD")  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        to_paypal(-1, "USD")
    with pytest.raises(MoneyError):
        to_paypal(True, "USD")  # bool is not money
    with pytest.raises(MoneyError):
        from_paypal(12.5, "USD")  # type: ignore[arg-type]


# ------------------------------------------------------------------ liability matrix
def test_rule_order_and_splits():
    assert attribute(Facts(p7=False, p1=False)).rule_id == "R1"  # R1 outranks R2
    a = attribute(Facts(p1=False))
    assert (a.rule_id, a.split) == ("R2", {"user": 0, "agent_platform": 100, "merchant": 0})
    assert attribute(Facts(p2=False)).rule_id == "R2"
    assert attribute(Facts(p5=False)).split == {"user": 0, "agent_platform": 0, "merchant": 100}
    assert attribute(Facts(p8=False)).split == {"user": 0, "agent_platform": 70, "merchant": 30}
    assert attribute(Facts(p4=False)).split == {"user": 0, "agent_platform": 100, "merchant": 0}
    assert attribute(Facts(p4=False, injected_text_merchant_controlled=True)).split == {
        "user": 0, "agent_platform": 70, "merchant": 30,
    }  # fmt: skip
    assert attribute(Facts(p3=False)).split == {"user": 0, "agent_platform": 100, "merchant": 0}
    assert attribute(Facts(p3=False, p9=True)).split == {"user": 30, "agent_platform": 70, "merchant": 0}
    assert attribute(Facts(p6=False)).split == {"user": 0, "agent_platform": 0, "merchant": 100}
    r8 = attribute(Facts())
    assert r8.rule_id == "R8" and r8.defend_merchant and r8.split["user"] == 100


def test_every_fact_combination_sums_to_100():
    import itertools

    for bits in itertools.product([True, False], repeat=9):
        a = attribute(Facts(*bits))
        assert sum(a.split.values()) == 100
        assert sum(1 for _, m in a.trace if m) == 1  # exactly one rule fires


def test_explainer_cannot_mutate_attribution():
    a = attribute(Facts(p5=False))
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.split = {"user": 100}  # type: ignore[misc]
    assert "R3" in explain(a)


# ------------------------------------------------------------------ taint
def test_taint_flags():
    p = Provenance()
    p.add(Label.USER, "user_prompt", "buy shoes")
    assert not evaluate_taint(p, {}).step_up
    p.add(Label.UNTRUSTED, "catalog:RF-200:review", "ignore previous", flagged=True)
    v = evaluate_taint(p, {})
    assert v.step_up and "suspected_instruction" in v.flags
    assert evaluate_taint(Provenance(), {"cites_untrusted": True}).step_up
    assert evaluate_taint(Provenance(), {"policy_deviation": True}).step_up
    raw = Provenance()
    raw.add(Label.UNTRUSTED, "planner:raw:review", "x")
    assert "raw_untrusted_in_planner" in evaluate_taint(raw, {}).flags


# ------------------------------------------------------------------ token
def test_token_binds_to_exact_cart():
    t = GatewayToken.issue(
        "s", purpose="create_order", mandate_id="m", custom_id="d" * 43, amount_minor=100, currency="USD",
        merchant_id="x", now=1000,
    )  # fmt: skip
    t.check("s", purpose="create_order", custom_id="d" * 43, amount_minor=100, currency="USD", now=1001)
    for kw in (
        dict(amount_minor=101), dict(currency="EUR"), dict(custom_id="e" * 43), dict(purpose="capture"),
    ):  # fmt: skip
        args = dict(purpose="create_order", custom_id="d" * 43, amount_minor=100, currency="USD", now=1001)
        args.update(kw)
        with pytest.raises(TokenError):
            t.check("s", **args)
    with pytest.raises(TokenError):
        t.check(
            "wrong", purpose="create_order", custom_id="d" * 43, amount_minor=100, currency="USD", now=1001
        )
    with pytest.raises(TokenError):
        t.check("s", purpose="create_order", custom_id="d" * 43, amount_minor=100, currency="USD", now=5000)


# ------------------------------------------------------------------ audit
def test_audit_chain_detects_tampering(env):
    for i in range(5):
        env.audit.append("test", "evt", {"i": i})
    assert env.audit.verify().ok
    with env.db.session() as s:
        s.execute(update(AuditLog).where(AuditLog.seq == 3).values(payload={"i": 999}))
    res = env.audit.verify()
    assert not res.ok and res.bad_seq == 3


def test_audit_detects_deletion(env):
    for i in range(4):
        env.audit.append("test", "evt", {"i": i})
    from sqlalchemy import delete

    with env.db.session() as s:
        s.execute(delete(AuditLog).where(AuditLog.seq == 2))
    assert not env.audit.verify().ok


# ------------------------------------------------------------------ ledger
def test_ledger_budget_replay_idempotency(env):
    m = env.make_mandate(budget_minor=20000, max_per_txn_minor=12000)
    kw = dict(currency="USD")
    r1 = env.ledger.reserve(m.id, 11900, "k1", transaction_id="t1", order_id="o1", **kw)
    assert r1.ok and r1.remaining_minor == 8100
    assert env.ledger.reserve(
        m.id, 11900, "k1", transaction_id="t1", order_id="o1", **kw
    ).idempotent  # retry safe
    assert env.ledger.reserve(m.id, 100, "k2", transaction_id="t1", **kw).code == "REPLAY"
    assert env.ledger.reserve(m.id, 9000, "k3", transaction_id="t3", **kw).code == "BUDGET"
    assert env.ledger.reserve(m.id, 8100, "k4", transaction_id="t4", **kw).ok  # exactly the remainder
    assert env.ledger.balance(m.id).remaining_minor == 0
    env.ledger.release(m.id, 8100, "o4")
    assert env.ledger.balance(m.id).remaining_minor == 8100


def test_ledger_commit_refund_accounting(env):
    m = env.make_mandate(budget_minor=20000)
    env.ledger.reserve(m.id, 10000, "k1", transaction_id="t1", order_id="o1", currency="USD")
    env.ledger.commit(m.id, 10000, "o1")
    b = env.ledger.balance(m.id)
    assert (b.reserved_minor, b.spent_minor, b.exposure_minor) == (0, 10000, 10000)
    env.ledger.refund(m.id, 10000, "o1", "full")
    b = env.ledger.balance(m.id)
    assert (b.spent_minor, b.exposure_minor, b.remaining_minor) == (0, 0, 20000)
    assert not env.ledger.commit(m.id, 10000, "o1")  # duplicate commit ignored


def test_ledger_occurrence_cap_and_currency(env):
    m = env.make_mandate(max_occurrences=2, budget_minor=100000, max_per_txn_minor=12000)
    assert env.ledger.reserve(m.id, 100, "a", transaction_id="ta", currency="USD").ok
    assert env.ledger.reserve(m.id, 100, "b", transaction_id="tb", currency="USD").ok
    assert env.ledger.reserve(m.id, 100, "c", transaction_id="tc", currency="USD").code == "OCCURRENCES"
    assert env.ledger.reserve(m.id, 100, "d", transaction_id="td", currency="EUR").code in (
        "OCCURRENCES",
        "CURRENCY",
    )


def test_ledger_revocation_pause_expiry(env):
    m = env.make_mandate()
    t = env.ledger.revoke(m.id)
    assert t is not None and env.ledger.status(m.id) == "revoked"
    assert env.ledger.reserve(m.id, 100, "x", transaction_id="tx", currency="USD").code == "REVOKED"
    assert env.ledger.was_revoked_at(m.id, t) and not env.ledger.was_revoked_at(m.id, t - 1)
    m2 = env.make_mandate()
    env.clk.advance(8 * 86400)
    assert env.ledger.reserve(m2.id, 100, "y", transaction_id="ty", currency="USD").code == "EXPIRED"


def test_velocity_breaker_pauses_mandate(env, monkeypatch):
    from mandate import settings as st

    monkeypatch.setattr(st.get_settings(), "velocity_max_attempts", 3)
    m = env.make_mandate(budget_minor=1_000_000, max_per_txn_minor=12000)
    codes = [
        env.ledger.reserve(m.id, 100, f"k{i}", transaction_id=f"t{i}", currency="USD").code for i in range(5)
    ]
    assert "VELOCITY" in codes
    assert env.ledger.status(m.id) == "paused"
    env.ledger.resume(m.id)
    assert env.ledger.status(m.id) == "active"


def test_liability_sensitivity_findings_hold():
    """The dispute action cannot depend on the platform/merchant split; the user's share is what matters."""
    from mandate.liability.sensitivity import analyse

    r = analyse(draws=200)
    assert r.baseline_matches_production
    assert r.platform_merchant_swaps_that_flip == 0
    assert all(0 <= v <= 1 for v in r.per_rule_flip_rate.values())
    assert 0 <= r.platform_share_range[0] <= r.platform_share_range[1] <= 1
    assert r.flip_thresholds["R4"].startswith("51%") and r.flip_thresholds["R8"].startswith("0%")
