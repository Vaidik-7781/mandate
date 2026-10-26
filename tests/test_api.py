"""HTTP API over the in-process app (replay mode)."""

from __future__ import annotations

import pytest
from helpers import PROMPT, build
from mandate import clock
from mandate.main import create_app
from starlette.testclient import TestClient


@pytest.fixture()
def api():
    rt = build()
    client = TestClient(create_app(rt))
    yield client, rt
    clock.set_clock(None)


def _mandate(client: TestClient) -> dict:
    c = client.post("/api/compile", json={"prompt": PROMPT}).json()
    assert c["ready"]
    r = client.post(
        "/api/mandates",
        json={
            "prompt": PROMPT,
            "draft": c["draft"],
            "selected_skus": [x["sku"] for x in c["candidates"]],
            "confirmed": True,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_health_and_config(api):
    client, _ = api
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["replay"] is True and h["vi_commit"].startswith("356c296")
    assert "not endorsed by PayPal" in client.get("/api/config").json()["disclaimer"]


def test_full_purchase_over_http(api):
    client, _ = api
    m = _mandate(client)
    p = client.post(f"/api/mandates/{m['id']}/purchase", json={}).json()
    assert p["outcome"] == "created"
    oid = p["order_id"]
    assert client.post(f"/api/orders/{oid}/simulate-approval").status_code == 200
    cap = client.post(f"/api/orders/{oid}/capture").json()
    assert cap["outcome"] == "captured"
    client.post(f"/api/orders/{oid}/deliver")
    rc = client.get(f"/api/orders/{oid}/receipt").json()
    assert client.post("/api/receipts/verify", json=rc).json() == {"valid": True}
    rc["receipt"]["amount_minor"] = 1
    assert client.post("/api/receipts/verify", json=rc).json() == {"valid": False}
    assert client.get(f"/api/orders/{oid}/receipt.pdf").content[:4] == b"%PDF"
    assert client.get("/api/audit/verify").json()["ok"] is True
    assert client.get("/api/analytics").json()["orders"] == 1


def test_errors_are_json(api):
    client, _ = api
    r = client.get("/api/mandates/nope")
    assert r.status_code == 404 and "detail" in r.json()
    r = client.post(
        "/api/mandates", json={"prompt": PROMPT, "draft": {}, "selected_skus": ["RF-100"], "confirmed": False}
    )
    assert r.status_code == 400


def test_revoke_over_http(api):
    client, _ = api
    m = _mandate(client)
    client.post(f"/api/mandates/{m['id']}/revoke")
    assert client.get(f"/api/mandates/{m['id']}").json()["status"] == "revoked"
    assert client.post(f"/api/mandates/{m['id']}/purchase", json={}).json()["outcome"] == "denied"


def test_webhook_endpoint_rejects_unsigned(api):
    client, _ = api
    r = client.post(
        "/webhooks/paypal",
        content=b'{"id":"WH-1","event_type":"X"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 401


def test_webhook_endpoint_accepts_signed_and_dedupes(api):
    client, rt = api
    ev = rt.sim.emit("CHECKOUT.ORDER.APPROVED", {"id": "NOPE"}, "checkout-order")
    rt.sim.state.outbox.remove(ev)
    h, b = rt.sim.build_delivery(ev)
    assert client.post("/webhooks/paypal", content=b, headers=h).status_code == 200
    r2 = client.post("/webhooks/paypal", content=b, headers=h)
    assert r2.status_code == 200 and r2.json()["duplicate"] is True


def test_catalog_exposes_untrusted_text_for_the_demo(api):
    client, _ = api
    cat = client.get("/api/catalog").json()
    assert any("IGNORE ALL PREVIOUS" in r for p in cat for r in p["reviews"])


def test_handling_a_dispute_twice_is_safe(api):
    client, _ = api
    m = _mandate(client)
    oid = client.post(f"/api/mandates/{m['id']}/purchase", json={}).json()["order_id"]
    client.post(f"/api/orders/{oid}/simulate-approval")
    client.post(f"/api/orders/{oid}/capture")
    d = client.post(f"/api/orders/{oid}/sim-dispute", json={"message": "never arrived"}).json()
    did = d.get("dispute_id") or d.get("id")
    r = client.post(f"/api/disputes/{did}/handle")  # the webhook already handled it once
    assert r.status_code == 200, r.text
    assert client.get("/api/audit/verify").json()["ok"] is True


def test_unknown_dispute_is_404_not_500(api):
    client, _ = api
    for path in ("handle", "adjudicate"):
        r = client.post(f"/api/disputes/nope/{path}")
        assert r.status_code == 404, (path, r.status_code, r.text)
