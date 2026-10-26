"""Gateway + simulator + webhook receiver, end to end over the real HTTP interface (in-process ASGI)."""

from __future__ import annotations

import json

import pytest
from mandate.db import WebhookEvent
from mandate.gateway.paypal.client import PayPalClient, PayPalError
from mandate.gateway.paypal.gateway import PayPalGateway
from mandate.gateway.paypal.webhooks import (
    HttpCertFetcher,
    WebhookError,
    validate_cert_url,
    verify_signature,
)
from mandate.guard.token import GatewayToken, TokenError
from mandate.receiver.receiver import Receiver
from mandate.sim.paypal import SIM_CERT_URL, PayPalSim, create_sim_app
from starlette.testclient import TestClient

SECRET = "guard-secret"
ITEMS = [{"sku": "RF-100", "name": "RunFast Road Racer", "quantity": 1, "unit_price_minor": 8900}]
CUSTOM = "c" * 43


@pytest.fixture()
def pp(env):
    sim = PayPalSim()
    http = TestClient(create_sim_app(sim), base_url="https://api-m.sandbox.paypal.com")
    client = PayPalClient(
        "https://api-m.sandbox.paypal.com", sim.client_id, sim.client_secret, http=http, sleep=lambda s: None
    )
    gw = PayPalGateway(client, SECRET)
    received: list[tuple[dict, bytes]] = []
    sim.subscribe(lambda h, b: received.append((h, b)))
    return sim, client, gw, received


def tok(purpose="create_order", custom=CUSTOM, amount=8900, now=None):
    from mandate import clock

    return GatewayToken.issue(SECRET, purpose=purpose, mandate_id="m", custom_id=custom, amount_minor=amount,
                              currency="USD", merchant_id="m-runfast", now=now or clock.now())  # fmt: skip


def new_order(gw):
    return gw.create_order(
        tok(), order_id="ord-1", custom_id=CUSTOM, currency="USD", items=ITEMS, merchant_name="RunFast"
    )


# ---------------------------------------------------------------- gateway invariants
def test_gateway_refuses_without_token(pp):
    _, _, gw, _ = pp
    with pytest.raises(TokenError):
        gw.create_order(None, order_id="o", custom_id=CUSTOM, currency="USD", items=ITEMS, merchant_name="x")


def test_gateway_refuses_token_for_different_cart(pp):
    _, _, gw, _ = pp
    with pytest.raises(TokenError):
        gw.create_order(
            tok(amount=100), order_id="o", custom_id=CUSTOM, currency="USD", items=ITEMS, merchant_name="x"
        )
    with pytest.raises(TokenError):
        gw.create_order(
            tok(custom="d" * 43),
            order_id="o",
            custom_id=CUSTOM,
            currency="USD",
            items=ITEMS,
            merchant_name="x",
        )


def test_order_amount_comes_from_signed_items_and_digest_in_custom_id(pp):
    sim, _, gw, _ = pp
    o = new_order(gw)
    pu = sim.state.orders[o["id"]]["purchase_units"][0]
    assert pu["amount"]["value"] == "89.00" and pu["custom_id"] == CUSTOM and pu["invoice_id"] == "ord-1"
    assert any(link["rel"] == "approve" for link in o["links"])


def test_create_order_is_idempotent_per_request_id(pp):
    sim, _, gw, _ = pp
    a, b = new_order(gw), new_order(gw)
    assert a["id"] == b["id"] and len(sim.state.orders) == 1


def test_capture_requires_approval_then_succeeds_once(pp):
    sim, client, gw, rec = pp
    o = new_order(gw)
    with pytest.raises(PayPalError) as e:
        gw.capture(
            tok("capture"), paypal_order_id=o["id"], custom_id=CUSTOM, amount_minor=8900, currency="USD"
        )
    assert "ORDER_NOT_APPROVED" in e.value.issues
    client.http.post(f"/sim/v1/orders/{o['id']}/approve")
    cap = gw.capture(
        tok("capture"), paypal_order_id=o["id"], custom_id=CUSTOM, amount_minor=8900, currency="USD"
    )
    assert cap.status == "COMPLETED" and cap.amount_minor == 8900 and cap.custom_id == CUSTOM
    again = gw.capture(
        tok("capture"), paypal_order_id=o["id"], custom_id=CUSTOM, amount_minor=8900, currency="USD"
    )
    assert (
        again.capture_id == cap.capture_id
    )  # same PayPal-Request-Id => replayed response, no double capture
    types = [json.loads(b)["event_type"] for _, b in rec]
    assert "CHECKOUT.ORDER.APPROVED" in types and types.count("PAYMENT.CAPTURE.COMPLETED") == 1


def test_sim_rejects_bad_decimal_precision(pp):
    _, client, _, _ = pp
    bad = {"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": "USD", "value": "89.001"}}]}
    with pytest.raises(PayPalError) as e:
        client.create_order(bad, "rid")
    assert e.value.status == 422 and "DECIMAL_PRECISION" in e.value.issues


def test_tracking_and_refund(pp):
    sim, client, gw, _ = pp
    o = new_order(gw)
    client.http.post(f"/sim/v1/orders/{o['id']}/approve")
    cap = gw.capture(
        tok("capture"), paypal_order_id=o["id"], custom_id=CUSTOM, amount_minor=8900, currency="USD"
    )
    gw.add_tracking(o["id"], cap.capture_id, "TRK123", "USPS")
    assert sim.state.captures[cap.capture_id]["tracking"]["tracking_number"] == "TRK123"
    r = gw.refund(
        tok("refund"),
        capture_id=cap.capture_id,
        custom_id=CUSTOM,
        amount_minor=8900,
        currency="USD",
        note="x",
    )
    assert r["status"] == "COMPLETED"
    with pytest.raises(PayPalError):  # over-refund
        gw.refund(
            tok("refund", amount=100),
            capture_id=cap.capture_id,
            custom_id=CUSTOM,
            amount_minor=100,
            currency="USD",
            note="x",
            tag="2",
        )
    # refund requires its own purpose-bound token
    with pytest.raises(TokenError):
        gw.refund(
            tok("capture"),
            capture_id=cap.capture_id,
            custom_id=CUSTOM,
            amount_minor=100,
            currency="USD",
            note="x",
            tag="3",
        )


def test_auth_failure_and_token_refresh(pp):
    sim, client, _, _ = pp
    bad = PayPalClient(client.base_url, "x", "y", http=client.http, sleep=lambda s: None)
    with pytest.raises(PayPalError) as e:
        bad.get_order("NOPE")
    assert e.value.status == 401
    client.get_order  # noqa: B018
    sim.state.tokens.clear()  # server forgets the token -> client re-auths once and succeeds
    with pytest.raises(PayPalError) as e2:
        client.get_order("MISSING")
    assert e2.value.status == 404


# ---------------------------------------------------------------- disputes
def make_captured(sim, client, gw):
    o = new_order(gw)
    client.http.post(f"/sim/v1/orders/{o['id']}/approve")
    cap = gw.capture(
        tok("capture"), paypal_order_id=o["id"], custom_id=CUSTOM, amount_minor=8900, currency="USD"
    )
    return o, cap


def test_dispute_lifecycle_defended(pp):
    sim, client, gw, rec = pp
    o, cap = make_captured(sim, client, gw)
    d = sim.create_dispute(cap.capture_id, "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "where is it?")
    sim.deliver()
    got = gw.get_dispute(d["dispute_id"])
    rels = {link["rel"] for link in got["links"]}
    assert {"provide-evidence", "make-offer", "accept-claim"} <= rels and "adjudicate" not in rels
    assert got["disputed_transactions"][0]["custom"] == CUSTOM  # chain digest travels with the dispute
    gw.provide_evidence(d["dispute_id"], [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "shipped"}],
                        [("packet.pdf", b"%PDF-1.4 test", "application/pdf")])  # fmt: skip
    got = gw.get_dispute(d["dispute_id"])
    assert got["status"] == "UNDER_REVIEW" and got["evidences"][0]["documents"][0]["name"] == "packet.pdf"
    assert "adjudicate" in {link["rel"] for link in got["links"]}
    gw.adjudicate(d["dispute_id"], "SELLER_FAVOR")
    sim.deliver()
    assert gw.get_dispute(d["dispute_id"])["dispute_outcome"]["outcome_code"] == "RESOLVED_SELLER_FAVOUR"
    types = [json.loads(b)["event_type"] for _, b in rec]
    assert types.count("CUSTOMER.DISPUTE.CREATED") == 1 and "CUSTOMER.DISPUTE.RESOLVED" in types


def test_adjudicate_requires_under_review(pp):
    sim, client, gw, _ = pp
    _, cap = make_captured(sim, client, gw)
    d = sim.create_dispute(cap.capture_id, "UNAUTHORISED")
    with pytest.raises(PayPalError) as e:
        gw.adjudicate(d["dispute_id"], "BUYER_FAVOR")
    assert "ACTION_NOT_ALLOWED" in e.value.issues


def test_evidence_limits(pp):
    sim, client, gw, _ = pp
    _, cap = make_captured(sim, client, gw)
    d = sim.create_dispute(cap.capture_id, "UNAUTHORISED")
    with pytest.raises(PayPalError) as e:
        gw.provide_evidence(d["dispute_id"], [{"evidence_type": "OTHER", "notes": "x" * 2001}], [])
    assert "INVALID_STRING_MAX_LENGTH" in e.value.issues
    with pytest.raises(PayPalError) as e2:
        gw.provide_evidence(
            d["dispute_id"],
            [{"evidence_type": "OTHER", "notes": "ok"}],
            [("x.exe", b"x", "application/octet-stream")],
        )
    assert "INVALID_FILE_TYPE" in e2.value.issues
    with pytest.raises(PayPalError) as e3:
        gw.provide_evidence(d["dispute_id"], [{"evidence_type": "OTHER", "notes": "ok"}],
                            [("big.pdf", b"0" * (10 * 1024 * 1024 + 1), "application/pdf")])  # fmt: skip
    assert "FILE_TOO_LARGE" in e3.value.issues


def test_accept_claim_refunds_and_offer_flow(pp):
    sim, client, gw, _ = pp
    _, cap = make_captured(sim, client, gw)
    d = sim.create_dispute(cap.capture_id, "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED")
    did = d["dispute_id"]
    with pytest.raises(TokenError):  # money actions need a dispute-scoped token
        gw.make_offer(None, dispute_id=did, amount_minor=4000, currency="USD", note="n")
    t = tok("make_offer", custom=did, amount=4000)
    gw.make_offer(t, dispute_id=did, amount_minor=4000, currency="USD", note="partial refund")
    assert gw.get_dispute(did)["status"] == "WAITING_FOR_BUYER_RESPONSE"
    client.http.post(f"/sim/v1/disputes/{did}/buyer-accept-offer")
    assert gw.get_dispute(did)["dispute_outcome"]["outcome_code"] == "RESOLVED_WITH_PAYOUT"
    d2 = sim.create_dispute(cap.capture_id, "UNAUTHORISED")
    gw.accept_claim(tok("accept_claim", custom=d2["dispute_id"], amount=8900), dispute_id=d2["dispute_id"],
                    amount_minor=8900, currency="USD", reason="NOT_ABLE_TO_WIN", note="n")  # fmt: skip
    assert gw.get_dispute(d2["dispute_id"])["dispute_outcome"]["outcome_code"] == "ACCEPTED"


# ---------------------------------------------------------------- webhook verification
def fetcher(sim):
    return lambda url: sim.cert_for_url(url) or (_ for _ in ()).throw(WebhookError("no cert"))


def delivery(sim):
    sim.emit("CUSTOMER.DISPUTE.CREATED", {"dispute_id": "PP-D-1"}, "dispute")
    ev = sim.state.outbox.pop()
    return sim.build_delivery(ev)


def test_signature_valid(pp):
    sim, *_ = pp
    h, b = delivery(sim)
    verify_signature(h, b, "WEBHOOK_ID", fetcher(sim))


def test_signature_rejects_modified_body_wrong_webhook_id_and_missing_headers(pp):
    sim, *_ = pp
    h, b = delivery(sim)
    with pytest.raises(WebhookError):
        verify_signature(h, b + b" ", "WEBHOOK_ID", fetcher(sim))
    with pytest.raises(WebhookError):
        verify_signature(
            h, json.dumps(json.loads(b), indent=2).encode(), "WEBHOOK_ID", fetcher(sim)
        )  # re-serialised
    with pytest.raises(WebhookError):
        verify_signature(h, b, "OTHER_ID", fetcher(sim))
    h2 = dict(h)
    h2.pop("paypal-transmission-sig")
    with pytest.raises(WebhookError):
        verify_signature(h2, b, "WEBHOOK_ID", fetcher(sim))


def test_signature_rejects_stale_transmission(pp):
    from mandate import clock

    sim, *_ = pp
    h, b = delivery(sim)
    with pytest.raises(WebhookError):
        verify_signature(h, b, "WEBHOOK_ID", fetcher(sim), now=clock.now_f() + 5 * 86400)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.sandbox.paypal.com/v1/notifications/certs/X",
        "https://evil.example.com/v1/notifications/certs/X",
        "https://api.paypal.com.evil.com/v1/notifications/certs/X",
        "https://user@api.sandbox.paypal.com/v1/notifications/certs/X",
        "https://api.sandbox.paypal.com/other/path",
        "https://api.sandbox.paypal.com:8443/v1/notifications/certs/X",
        "https://api.sandbox.paypal.com/v1/notifications/certs/../x",
    ],
)
def test_cert_url_ssrf_validation(url):
    with pytest.raises(WebhookError):
        validate_cert_url(url)
    with pytest.raises(WebhookError):
        HttpCertFetcher()(url)


def test_cert_url_good():
    validate_cert_url(SIM_CERT_URL)


def test_forged_signature_from_other_key_rejected(pp):
    sim, *_ = pp
    other = PayPalSim()  # a different signing key
    h, b = delivery(other)
    with pytest.raises(WebhookError):
        verify_signature(h, b, "WEBHOOK_ID", fetcher(sim))


# ---------------------------------------------------------------- receiver
def test_receiver_dedupes_and_enqueues(env, pp):
    sim, *_ = pp
    queued: list[str] = []
    rx = Receiver(env.db, env.audit, "WEBHOOK_ID", fetcher(sim), queued.append)
    h, b = delivery(sim)
    r1 = rx.receive(h, b)
    r2 = rx.receive(h, b)  # PayPal retry
    assert (r1.status_code, r2.status_code) == (200, 200) and r2.duplicate and len(queued) == 1
    with env.db.session() as s:
        assert s.query(WebhookEvent).count() == 1


def test_receiver_rejects_forgery_and_does_not_store(env, pp):
    sim, *_ = pp
    queued: list[str] = []
    rx = Receiver(env.db, env.audit, "WEBHOOK_ID", fetcher(sim), queued.append)
    h, b = delivery(sim)
    r = rx.receive(h, b.replace(b"PP-D-1", b"PP-D-2"))
    assert r.status_code == 401 and not queued
    with env.db.session() as s:
        assert s.query(WebhookEvent).count() == 0
    assert any(e["event"] == "webhook.rejected" for e in env.audit.entries())


def test_postback_helper_shapes_request(pp):
    sim, client, *_ = pp
    seen = {}
    client.request = lambda method, path, **kw: (
        seen.update(path=path, body=kw["json_body"]) or {"verification_status": "SUCCESS"}
    )  # type: ignore[method-assign]
    h, b = delivery(sim)
    assert client.verify_webhook_postback(h, json.loads(b), "WEBHOOK_ID")
    assert seen["path"].endswith("verify-webhook-signature") and seen["body"]["webhook_id"] == "WEBHOOK_ID"
