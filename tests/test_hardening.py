"""Regression tests for the hardening pass: revoke-vs-capture race, ledger re-reserve, receipt pinning,
input bounds, rate limit and body cap, PayPal-documented evidence types."""

from __future__ import annotations

import copy
import threading

import pytest
from helpers import PROMPT, build
from mandate import clock
from mandate.agents.evidence import ALLOWED_EVIDENCE_TYPES, packet_evidence_type
from mandate.main import create_app
from mandate.service import ServiceError
from starlette.testclient import TestClient


@pytest.fixture()
def rt():
    r = build()
    yield r
    clock.set_clock(None)


def _mandate(rt, prompt=PROMPT):
    c = rt.service.compile("alice", prompt)
    return rt.service.create_mandate("alice", prompt, c["draft"], [x["sku"] for x in c["candidates"]], True)


# ------------------------------------------------------------------ ledger
def test_commit_after_release_re_reserves_so_exposure_never_undercounts(env):
    m = env.make_mandate()
    r = env.ledger.reserve(m.id, 8900, "k1", currency="USD", transaction_id="t1", order_id="o1")
    assert r.ok
    assert env.ledger.release(m.id, 8900, "o1")  # swept as cancelled ...
    assert env.ledger.balance(m.id).exposure_minor == 0
    assert env.ledger.commit(m.id, 8900, "o1")  # ... but PayPal captured it anyway (late webhook)
    b = env.ledger.balance(m.id)
    assert b.exposure_minor == 8900 and b.spent_minor == 8900 and b.remaining_minor == m.budget_minor - 8900
    assert not env.ledger.commit(m.id, 8900, "o1")  # idempotent


def test_normal_commit_does_not_double_count(env):
    m = env.make_mandate()
    env.ledger.reserve(m.id, 8900, "k1", currency="USD", transaction_id="t1", order_id="o1")
    env.ledger.commit(m.id, 8900, "o1")
    assert env.ledger.balance(m.id).exposure_minor == 8900


# ------------------------------------------------------------------ race
@pytest.fixture(params=["memory", "file"])
def race_rt(request, tmp_path):
    """Both database modes: the shared in-memory connection, and a file with real per-thread connections."""
    if request.param == "memory":
        r = build()
    else:
        from helpers import T0
        from mandate.db import Database
        from mandate.runtime import Runtime
        from mandate.settings import Settings

        clock.set_clock(clock.FrozenClock(T0))
        url = f"sqlite:///{tmp_path / 'race.db'}"
        cfg = Settings(database_url=url, replay_mode=True, secret_seed="flow-seed", _env_file=None)
        r = Runtime.build(cfg, db=Database(url))
    yield r
    clock.set_clock(None)


@pytest.mark.parametrize("round_", range(8))
def test_revoke_racing_capture_never_leaves_money_captured_and_unrefunded(race_rt, round_):
    rt = race_rt
    m = _mandate(rt)
    oid = rt.service.agent_purchase(m["id"])["order_id"]
    rt.service.simulate_buyer_approval(oid)
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def cap():
        barrier.wait()
        try:
            rt.service.capture(oid)
        except ServiceError:
            pass  # revoke won the race: "order is CANCELLED" is the correct answer
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def rev():
        barrier.wait()
        try:
            rt.service.revoke(m["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=cap), threading.Thread(target=rev)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    o = rt.service.order_view(oid)
    captured = (o["captured_amount_minor"] or 0) if o["capture_id"] else 0
    net = captured - (o["refunded_minor"] or 0)
    assert net == 0, o  # either never captured, or captured and fully refunded
    b = rt.ledger.balance(m["id"])
    assert b.exposure_minor == 0 and b.spent_minor == 0


# ------------------------------------------------------------------ receipts
def test_receipt_envelope_alg_and_kid_are_pinned(rt):
    m = _mandate(rt)
    oid = rt.service.agent_purchase(m["id"])["order_id"]
    rt.service.simulate_buyer_approval(oid)
    rt.service.capture(oid)
    doc = rt.service.receipt(oid)
    assert rt.service.verify_receipt(doc)
    for field, value in (("alg", "none"), ("kid", "attacker-key")):
        bad = copy.deepcopy(doc)
        bad[field] = value
        assert not rt.service.verify_receipt(bad), field
    tampered = copy.deepcopy(doc)
    tampered["receipt"]["amount_minor"] = 1
    assert not rt.service.verify_receipt(tampered)


# ------------------------------------------------------------------ API bounds
@pytest.fixture()
def api(rt):
    return TestClient(create_app(rt))


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/compile", {"prompt": ""}),
        ("/api/compile", {"prompt": "x" * 2001}),
        ("/api/mandates/m/purchase", {"quantity": 0}),
        ("/api/mandates/m/purchase", {"quantity": -3}),
        ("/api/mandates/m/purchase", {"quantity": 10**9}),
        ("/api/mandates/m/purchase", {"query": "q" * 501}),
        ("/api/orders/o/sim-dispute", {"message": "m" * 2001}),
        ("/api/orders/o/sim-dispute", {"amount_minor": 0}),
    ],
)
def test_out_of_range_input_is_a_422_not_a_500(api, path, body):
    assert api.post(path, json=body).status_code == 422


def test_oversized_body_is_refused_with_413(api):
    r = api.post(
        "/api/compile", content=b"{" + b" " * 1_100_000 + b"}", headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


def test_write_rate_limit_returns_429_with_retry_after(rt):
    rt.settings.rate_limit_per_minute = 5
    client = TestClient(create_app(rt))
    codes = [client.post("/api/compile", json={"prompt": "shoes"}).status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and set(codes[5:]) == {429}
    r = client.post("/api/compile", json={"prompt": "shoes"})
    assert r.headers["retry-after"].isdigit()
    assert client.get("/health").status_code == 200  # reads are never limited


# ------------------------------------------------------------------ PayPal-documented evidence types
def test_no_reason_uses_an_undocumented_evidence_type():
    assert "PROOF_OF_AUTHORIZATION" not in {t for v in ALLOWED_EVIDENCE_TYPES.values() for t in v}
    for reason, allowed in ALLOWED_EVIDENCE_TYPES.items():
        assert packet_evidence_type(reason) in allowed, reason
    assert packet_evidence_type("UNAUTHORISED") == "OTHER"
    assert packet_evidence_type("MERCHANDISE_OR_SERVICE_NOT_RECEIVED") == "PROOF_OF_FULFILLMENT"


def test_unauthorised_dispute_is_defended_with_documented_evidence_types(rt):
    m = _mandate(rt)
    oid = rt.service.agent_purchase(m["id"])["order_id"]
    rt.service.simulate_buyer_approval(oid)
    rt.service.capture(oid)
    rt.service.deliver(oid)
    out = rt.service.open_sim_dispute(oid, "UNAUTHORISED", "I did not authorise this charge.")
    d = rt.service.dispute_view(out["dispute_id"])
    assert d["decision"] in ("fight", "human"), d
    sim_d = rt.sim.state.disputes[out["dispute_id"]]
    types = [e["evidence_type"] for e in sim_d["evidences"]]
    assert all(t in ALLOWED_EVIDENCE_TYPES["UNAUTHORISED"] for t in types), types


def test_sim_rejects_an_undocumented_evidence_type(rt):
    m = _mandate(rt)
    oid = rt.service.agent_purchase(m["id"])["order_id"]
    rt.service.simulate_buyer_approval(oid)
    rt.service.capture(oid)
    out = rt.service.open_sim_dispute(oid, "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "")
    from mandate.gateway.paypal.client import PayPalError

    with pytest.raises(PayPalError):
        rt.gateway.provide_evidence(
            out["dispute_id"], [{"evidence_type": "PROOF_OF_AUTHORIZATION", "notes": "x"}], []
        )


def test_paypal_protection_already_applied_is_deferred_to_not_duplicated(rt):
    m = _mandate(rt)
    oid = rt.service.agent_purchase(m["id"])["order_id"]
    rt.service.simulate_buyer_approval(oid)
    rt.service.capture(oid)
    rt.service.deliver(oid)
    out = rt.service.open_sim_dispute(
        oid, "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "never came", seller_protection=True
    )
    d = rt.service.dispute_view(out["dispute_id"])
    assert d["decision"] == "none", d
    assert (
        rt.sim.state.disputes[out["dispute_id"]]["evidences"] == []
    )  # we did not duplicate PayPal's evidence


# ------------------------------------------------------------------ personas and the reality table
def test_every_persona_preset_compiles_cleanly_and_buys_inside_its_limits(rt):
    from mandate.presets import PRESETS

    assert {p["id"] for p in PRESETS} >= {"runner", "caregiver", "procurement"}
    for p in PRESETS:
        c = rt.service.compile("alice", p["prompt"])
        assert c["ready"], (p["id"], c["draft"]["questions"])
        assert c["candidates"], p["id"]
        m = rt.service.create_mandate(
            "alice", p["prompt"], c["draft"], [x["sku"] for x in c["candidates"]], True
        )
        out = rt.service.agent_purchase(m["id"])
        assert out["outcome"] in ("created", "step_up"), (p["id"], out)
        assert out["plan"]["chosen_sku"] in m["allowed_skus"]


def test_caregiver_injected_review_never_reaches_a_purchase_of_something_else(rt):
    p = next(x for x in __import__("mandate.presets", fromlist=["PRESETS"]).PRESETS if x["id"] == "caregiver")
    c = rt.service.compile("alice", p["prompt"])
    m = rt.service.create_mandate("alice", p["prompt"], c["draft"], [x["sku"] for x in c["candidates"]], True)
    out = rt.service.agent_purchase(m["id"], hijacked=True)
    assert out["outcome"] in ("denied", "blocked", "step_up", "no_candidate"), out


def test_config_states_plainly_what_is_simulated(api):
    cfg = api.get("/api/config").json()
    assert cfg["mode"] == "replay" and cfg["replay"] is True
    parts = {r["part"]: r["status"] for r in cfg["reality"]}
    assert parts["PayPal Orders, Payments, Disputes, Webhooks"] == "simulated"
    assert parts["Dispute outcome"] == "scripted"
    assert api.get("/api/presets").json()[0]["prompt"]
