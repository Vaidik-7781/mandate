"""FastAPI app: the HTTP surface for the buyer app, the merchant console, PayPal webhooks and the bench."""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .gateway.paypal.client import PayPalError
from .guard.token import TokenError
from .mcp import McpServer
from .presets import PRESETS, reality
from .runtime import Runtime
from .service import ServiceError
from .settings import Settings, get_settings
from .site import install as install_site
from .workflows.render_app import build_runner

BENCH_DIR = Path(__file__).resolve().parents[3] / "bench" / "results"
WEB_DIST = Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"


MAX_BODY_BYTES = 1_000_000  # no endpoint needs more; webhooks are a few KB


class CompileIn(BaseModel):
    user_id: str = Field("demo-user", min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=2000)
    answers: dict[str, Any] = Field(default_factory=dict, max_length=20)


class MandateIn(BaseModel):
    user_id: str = Field("demo-user", min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=2000)
    draft: dict[str, Any]
    selected_skus: list[str] = Field(max_length=50)
    confirmed: bool = False
    ambiguity_flagged: bool = False
    max_occurrences: int | None = Field(None, ge=1, le=1000)


class PurchaseIn(BaseModel):
    query: str | None = Field(None, max_length=500)
    quantity: int = Field(1, ge=1, le=1000)
    hijacked: bool = False


class RevokeIn(BaseModel):
    reason: str = Field("user", max_length=200)


class DecideIn(BaseModel):
    approve: bool


class DisputeIn(BaseModel):
    reason: str = Field("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", max_length=64)
    message: str = Field("", max_length=2000)
    amount_minor: int | None = Field(None, ge=1, le=100_000_000)
    seller_protection: bool = False  # replay mode: PayPal's own protection already covers this dispute


class BuyerMessageIn(BaseModel):
    message: str = Field(max_length=2000)


class BuyerRespondIn(BaseModel):
    accept: bool


def create_app(rt: Runtime | None = None, settings: Settings | None = None) -> FastAPI:
    cfg = settings or (rt.settings if rt else get_settings())
    app = FastAPI(
        title="Mandate",
        version=__version__,
        description="Accountability layer for agent purchases on PayPal. Sandbox project, not production-ready, "
        "not legal advice, not endorsed by PayPal.",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["*"], allow_headers=["*"]
    )

    hits: dict[str, deque[float]] = defaultdict(deque)
    hits_lock = threading.Lock()

    @app.middleware("http")
    async def _guard_rails(request: Request, call_next):  # type: ignore[no-untyped-def]
        # 1) body cap: refuse early on the declared length (the webhook signature covers the raw bytes, so the
        #    body is never truncated or rewritten)
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                declared = int(request.headers.get("content-length", "0"))
            except ValueError:
                declared = 0
            if declared > MAX_BODY_BYTES:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        # 2) per-address write limit for the public demo (reads and PayPal webhooks are exempt)
        limit = cfg.rate_limit_per_minute
        if (
            limit > 0
            and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and not request.url.path.startswith("/webhooks/")
        ):
            who = request.client.host if request.client else "unknown"
            now = time.monotonic()
            with hits_lock:
                q = hits[who]
                while q and now - q[0] > 60:
                    q.popleft()
                if len(q) >= limit:
                    retry = max(1, int(60 - (now - q[0])))
                    return JSONResponse({"detail": "rate limit: too many requests, slow down"}, status_code=429,
                                        headers={"Retry-After": str(retry)})  # fmt: skip
                q.append(now)
        return await call_next(request)

    def runtime() -> Runtime:
        if not hasattr(app.state, "rt"):
            app.state.rt = Runtime.build(cfg, runner=None if cfg.is_replay else build_runner(cfg.task_runner))
        return app.state.rt

    if rt is not None:
        app.state.rt = rt

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(TokenError)
    async def _token_error(_: Request, exc: TokenError) -> JSONResponse:
        return JSONResponse({"detail": f"gateway refused: {exc}"}, status_code=403)

    @app.exception_handler(PayPalError)
    async def _paypal_error(_: Request, exc: PayPalError) -> JSONResponse:
        # PayPal said no (unknown id, wrong state): the caller's problem for 4xx, a bad gateway otherwise
        code = 404 if exc.status == 404 else 409 if exc.status in (400, 409, 422) else 502
        return JSONResponse({"detail": f"PayPal: {exc}"}, status_code=code)

    # ---------------------------------------------------------------------------------- meta
    @app.get("/health")
    def health() -> dict:
        r = runtime()
        return {"status": "ok", "version": __version__, "replay": r.replay, "paypal_env": cfg.paypal_env,
                "vi_commit": cfg.vi_commit, "llm": r.backend.name}  # fmt: skip

    @app.get("/api/config")
    def config() -> dict:
        r = runtime()
        return {"replay": r.replay, "mode": "replay" if r.replay else "sandbox", "paypal_client_id": cfg.paypal_client_id,
                "paypal_env": cfg.paypal_env, "vi_commit": cfg.vi_commit, "currency": "USD", "llm": r.backend.name,
                "reality": reality(r.replay, r.backend.name, cfg.paypal_env),
                "ga_id": cfg.ga_measurement_id, "contact_email": cfg.contact_email,
                "disclaimer": "Sandbox only. Not production-ready, not legal advice, not endorsed by PayPal."}  # fmt: skip

    @app.get("/api/presets")
    def presets() -> list[dict]:
        return PRESETS

    @app.get("/api/catalog")
    def catalog() -> list[dict]:
        r = runtime()
        return [{"sku": p.sku, "name": p.name, "merchant_id": p.merchant_id, "category": p.category,
                 "price_minor": p.price_minor, "attributes": p.attributes, "description": p.description,
                 "reviews": p.reviews} for p in r.merchants.catalog.values()]  # fmt: skip

    # ---------------------------------------------------------------------------------- mandates
    @app.post("/api/compile")
    def compile_(body: CompileIn) -> dict:
        return runtime().service.compile(body.user_id, body.prompt, body.answers)

    @app.post("/api/mandates")
    def create_mandate(body: MandateIn) -> dict:
        return runtime().service.create_mandate(body.user_id, body.prompt, body.draft, body.selected_skus,
                                                body.confirmed, body.ambiguity_flagged, body.max_occurrences)  # fmt: skip

    @app.get("/api/mandates")
    def list_mandates(user_id: str | None = None) -> list[dict]:
        return runtime().service.list_mandates(user_id)

    @app.get("/api/mandates/{mandate_id}")
    def get_mandate(mandate_id: str) -> dict:
        return runtime().service.mandate_view(mandate_id)

    @app.get("/api/mandates/{mandate_id}/credentials")
    def credentials(mandate_id: str) -> dict:
        return runtime().service.credentials(mandate_id)

    @app.post("/api/mandates/{mandate_id}/purchase")
    def purchase(mandate_id: str, body: PurchaseIn) -> dict:
        return runtime().service.agent_purchase(mandate_id, body.query, body.quantity, body.hijacked)

    @app.post("/api/mandates/{mandate_id}/revoke")
    def revoke(mandate_id: str, body: RevokeIn | None = None) -> dict:
        return runtime().service.revoke(mandate_id, (body.reason if body else "user"))

    @app.get("/api/mandates/{mandate_id}/decisions")
    def mandate_decisions(mandate_id: str) -> list[dict]:
        return runtime().service.decisions(mandate_id)

    # ---------------------------------------------------------------------------------- orders
    @app.get("/api/orders")
    def list_orders(mandate_id: str | None = None) -> list[dict]:
        return runtime().service.list_orders(mandate_id)

    @app.get("/api/orders/{order_id}")
    def get_order(order_id: str) -> dict:
        return runtime().service.order_view(order_id)

    @app.post("/api/orders/{order_id}/simulate-approval")
    def simulate_approval(order_id: str) -> dict:
        return runtime().service.simulate_buyer_approval(order_id)

    @app.post("/api/orders/{order_id}/capture")
    def capture(order_id: str) -> dict:
        return runtime().service.capture(order_id)

    @app.post("/api/orders/{order_id}/deliver")
    def deliver(order_id: str) -> dict:
        return runtime().service.deliver(order_id)

    @app.get("/api/orders/{order_id}/liability")
    def liability(order_id: str) -> dict:
        return runtime().service.liability(order_id)

    @app.get("/api/orders/{order_id}/receipt")
    def receipt(order_id: str) -> dict:
        return runtime().service.receipt(order_id)

    @app.get("/api/orders/{order_id}/receipt.pdf")
    def receipt_pdf(order_id: str) -> Response:
        return Response(runtime().service.receipt_pdf(order_id), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="receipt-{order_id}.pdf"'})  # fmt: skip

    @app.post("/api/receipts/verify")
    def verify_receipt(doc: dict[str, Any]) -> dict:
        return {"valid": runtime().service.verify_receipt(doc)}

    # ---------------------------------------------------------------------------------- approvals
    @app.get("/api/approvals")
    def approvals(status: str | None = None) -> list[dict]:
        return runtime().service.list_approvals(status)

    @app.post("/api/approvals/{approval_id}/decide")
    def decide(approval_id: str, body: DecideIn) -> dict:
        return runtime().service.decide_approval(approval_id, body.approve)

    # ---------------------------------------------------------------------------------- disputes
    @app.get("/api/disputes")
    def disputes() -> list[dict]:
        return runtime().service.list_disputes()

    @app.get("/api/disputes/{dispute_id}")
    def dispute(dispute_id: str) -> dict:
        return runtime().service.dispute_view(dispute_id)

    @app.post("/api/orders/{order_id}/sim-dispute")
    def sim_dispute(order_id: str, body: DisputeIn) -> dict:
        return runtime().service.open_sim_dispute(
            order_id, body.reason, body.message, body.amount_minor, body.seller_protection
        )

    @app.post("/api/disputes/{dispute_id}/buyer-message")
    def buyer_message(dispute_id: str, body: BuyerMessageIn) -> dict:
        return runtime().service.sim_buyer_message(dispute_id, body.message)

    @app.post("/api/disputes/{dispute_id}/buyer-respond")
    def buyer_respond(dispute_id: str, body: BuyerRespondIn) -> dict:
        return runtime().service.sim_buyer_respond(dispute_id, body.accept)

    @app.post("/api/disputes/{dispute_id}/adjudicate")
    def adjudicate(dispute_id: str) -> dict:
        return runtime().service.adjudicate(dispute_id)

    @app.post("/api/disputes/{dispute_id}/handle")
    def handle_dispute(dispute_id: str) -> dict:
        return runtime().disputes.handle(dispute_id).to_dict()

    # ---------------------------------------------------------------------------------- audit + analytics
    @app.get("/api/audit")
    def audit(limit: int = 100, prefix: str | None = None) -> list[dict]:
        return runtime().audit.entries(limit=min(limit, 500), event_prefix=prefix)

    @app.get("/api/audit/verify")
    def audit_verify() -> dict:
        v = runtime().audit.verify()
        return {"ok": v.ok, "count": v.count, "head": v.head, "bad_seq": v.bad_seq, "reason": v.reason}

    @app.get("/api/analytics")
    def analytics() -> dict:
        return runtime().service.analytics()

    @app.get("/api/decisions")
    def decisions(limit: int = 100) -> list[dict]:
        return runtime().service.decisions(None, min(limit, 500))

    # ---------------------------------------------------------------------------------- MCP
    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        """Model Context Protocol over plain JSON. No tool can create, widen or revoke a mandate."""
        try:
            msg = await request.json()
        except ValueError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            )
        srv = McpServer(runtime())
        if isinstance(msg, list):  # JSON-RPC batch
            out = [r for r in (srv.handle(m) for m in msg[:20]) if r is not None]
            return JSONResponse(out) if out else Response(status_code=202)
        resp = srv.handle(msg)
        return JSONResponse(resp) if resp is not None else Response(status_code=202)

    # ---------------------------------------------------------------------------------- bench
    @app.get("/api/bench/latest")
    def bench_latest() -> dict:
        f = BENCH_DIR / "latest.json"
        if not f.exists():
            raise ServiceError("no bench results yet: run `make bench`", 404)
        return json.loads(f.read_text())

    # ---------------------------------------------------------------------------------- webhooks
    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request) -> JSONResponse:
        raw = await request.body()  # the signature covers the raw bytes, never a re-serialised body
        res = runtime().receiver.receive({k.lower(): v for k, v in request.headers.items()}, raw)
        return JSONResponse({"detail": res.detail, "duplicate": res.duplicate}, status_code=res.status_code)

    install_site(app, cfg, WEB_DIST, lambda: runtime().contact)

    return app


def app_factory() -> FastAPI:  # uvicorn --factory mandate.main:app_factory
    return create_app()


try:  # `uvicorn mandate.main:app` for the simple case
    app = create_app()
except Exception:  # noqa: BLE001 - import must never crash tooling that only wants the module
    app = None  # type: ignore[assignment]
