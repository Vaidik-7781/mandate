"""MCP server: the narrow door. No tool can create, widen or revoke a mandate, and the guard still decides."""

from __future__ import annotations

import io
import json

import pytest
from helpers import build
from mandate import clock
from mandate.main import create_app
from mandate.mcp import TOOL_NAMES, McpServer, serve_stdio, sign_demo_mandate
from starlette.testclient import TestClient


@pytest.fixture()
def rt():
    r = build()
    yield r
    clock.set_clock(None)


def rpc(srv: McpServer, method: str, params: dict | None = None, mid: int = 1) -> dict:
    out = srv.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
    assert out is not None
    return out


def call(srv: McpServer, name: str, args: dict | None = None) -> dict:
    r = rpc(srv, "tools/call", {"name": name, "arguments": args or {}})
    return r["result"]


@pytest.fixture()
def srv(rt):  # type: ignore[no-untyped-def]
    return McpServer(rt)


def test_initialize_and_list(srv: McpServer) -> None:
    r = rpc(srv, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert r["protocolVersion"] == "2025-06-18" and r["serverInfo"]["name"] == "mandate"
    assert (
        rpc(srv, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    )
    names = {t["name"] for t in rpc(srv, "tools/list")["result"]["tools"]}
    assert names == TOOL_NAMES
    # the point of the design: nothing here can mint or widen authority
    assert not any(w in n for n in names for w in ("create", "sign", "widen", "revoke", "mandate_update"))


def test_notification_gets_no_response(srv: McpServer) -> None:
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_protocol_errors(srv: McpServer) -> None:
    assert srv.handle("nope")["error"]["code"] == -32600  # type: ignore[index]
    assert rpc(srv, "nope/method")["error"]["code"] == -32601
    assert rpc(srv, "tools/call", {"name": "create_mandate", "arguments": {}})["error"]["code"] == -32602
    assert rpc(srv, "tools/call", {"name": "purchase", "arguments": "x"})["error"]["code"] == -32602


def test_unexpected_and_bad_arguments_are_tool_errors(srv: McpServer) -> None:
    r = call(srv, "get_mandate", {"mandate_id": "x", "budget_minor": 10**9})
    assert r["isError"] and "unexpected" in r["content"][0]["text"]
    assert call(srv, "get_mandate", {})["isError"]
    assert call(srv, "get_mandate", {"mandate_id": "mnd_missing"})["isError"]
    assert call(srv, "purchase", {"mandate_id": "m", "quantity": 0})["isError"]
    assert call(srv, "purchase", {"mandate_id": "m", "quantity": True})["isError"]


def test_purchase_capture_receipt_through_mcp(rt, srv: McpServer) -> None:  # type: ignore[no-untyped-def]
    m = sign_demo_mandate(rt, "runner")
    assert [x["id"] for x in json.loads(call(srv, "list_mandates")["content"][0]["text"])] == [m["id"]]
    cat = json.loads(call(srv, "search_catalog", {"query": "running shoes"})["content"][0]["text"])
    assert cat and all("price_minor" in c for c in cat)

    r = call(srv, "purchase", {"mandate_id": m["id"], "query": "running shoes", "quantity": 1})
    assert not r["isError"]
    out = r["structuredContent"]
    assert out["outcome"] == "created" and out["order"]["status"] == "CREATED"

    # capture before the buyer approves is refused by PayPal/the gateway, surfaced as a tool error
    assert call(srv, "capture", {"order_id": out["order"]["id"]})["isError"]
    rt.service.simulate_buyer_approval(out["order"]["id"])
    cap = call(srv, "capture", {"order_id": out["order"]["id"]})
    assert not cap["isError"]
    rec = call(srv, "receipt", {"order_id": out["order"]["id"]})["structuredContent"]
    assert rt.service.verify_receipt(rec)
    assert (
        call(srv, "order_status", {"order_id": out["order"]["id"]})["structuredContent"]["status"]
        == "CAPTURED"
    )


def test_mcp_purchase_over_the_cap_is_refused(rt, srv: McpServer) -> None:  # type: ignore[no-untyped-def]
    m = sign_demo_mandate(rt, "runner")
    r = call(srv, "purchase", {"mandate_id": m["id"], "query": "running shoes", "quantity": 50})
    out = r["structuredContent"]
    assert out["outcome"] != "created" and out["order"] is None
    assert rt.service.list_orders(m["id"]) == []


def test_http_endpoint_and_stdio_share_the_handler(rt) -> None:  # type: ignore[no-untyped-def]
    c = TestClient(create_app(rt))
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert r.status_code == 200 and {t["name"] for t in r.json()["result"]["tools"]} == TOOL_NAMES
    assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert c.post("/mcp", content=b"{not json").json()["error"]["code"] == -32700
    batch = c.post(
        "/mcp",
        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}],
    )
    assert [x["id"] for x in batch.json()] == [1, 2]

    inp = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        + "\n\nnot json\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
    )
    out = io.StringIO()
    serve_stdio(rt, inp, out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0]["result"] == {} and lines[1]["error"]["code"] == -32700 and len(lines) == 2


def test_no_ui_fallback_page(rt, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import mandate.main as main

    monkeypatch.setattr(main, "WEB_DIST", main.Path("/nonexistent-dist"))
    r = TestClient(create_app(rt)).get("/")
    assert r.status_code == 200 and "Mandate API is running" in r.text


def test_sandbox_check_passes_in_replay(rt) -> None:  # type: ignore[no-untyped-def]
    from mandate.sandbox_check import run

    lines: list[str] = []
    rep = run(rt, say=lines.append)
    assert rep.ok, "\n".join(lines)
    assert any("custom_id" in s.name for s in rep.steps) and len(rep.steps) >= 10
    assert rep.to_dict()["mode"].startswith("replay")


def test_stdio_accepts_a_byte_order_mark(rt) -> None:  # type: ignore[no-untyped-def]
    """PowerShell can pipe text with a leading BOM; the first message must still parse."""
    out = io.StringIO()
    serve_stdio(rt, io.StringIO('\ufeff{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'), out)
    assert len(json.loads(out.getvalue())["result"]["tools"]) == 8
