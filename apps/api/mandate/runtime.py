"""Wiring: builds every component once. Replay mode mounts the in-process PayPal simulator (no keys, no network);
sandbox mode talks to the real PayPal sandbox with the same gateway code."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from starlette.testclient import TestClient

from .agents.dispute import DisputeAgent
from .agents.llm import LLMBackend, build_backend
from .audit.log import Audit
from .contact import ContactService
from .credentials.keys import KeyRing
from .db import Database
from .gateway.paypal.client import PayPalClient
from .gateway.paypal.gateway import PayPalGateway
from .gateway.paypal.webhooks import HttpCertFetcher, WebhookError, validate_cert_url
from .guard.guard import Guard
from .ledger.ledger import Ledger
from .merchant.store import MerchantService
from .receiver.receiver import Receiver
from .service import MandateService
from .settings import LIVE_BASE, SANDBOX_BASE, Settings, get_settings
from .sim.paypal import PayPalSim, create_sim_app
from .workflows.tasks import LocalRunner, TaskRunner


@dataclass
class Runtime:
    settings: Settings
    db: Database
    keys: KeyRing
    merchants: MerchantService
    ledger: Ledger
    audit: Audit
    guard: Guard
    backend: LLMBackend
    gateway: PayPalGateway
    receiver: Receiver
    disputes: DisputeAgent
    service: MandateService
    runner: TaskRunner
    sim: PayPalSim | None = None
    sim_app: Any | None = None
    contact: ContactService | None = None

    @property
    def replay(self) -> bool:
        return self.sim is not None

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        db: Database | None = None,
        runner: TaskRunner | None = None,
    ) -> Runtime:
        cfg = settings or get_settings()
        db = db or Database(cfg.database_url)
        db.create_all()
        seed = cfg.secret_seed
        keys = KeyRing.from_seed(seed)
        merchants = MerchantService(seed)
        ledger, audit = Ledger(db), Audit(db)
        guard_secret = hashlib.sha256(f"mandate/guard/{seed}".encode()).hexdigest()
        guard = Guard(db, ledger, audit, keys.issuer.public_key, merchants.public_jwks, secret=guard_secret)
        backend = build_backend(cfg)

        sim: PayPalSim | None = None
        sim_app = None
        sim_http: TestClient | None = None
        if cfg.is_replay:
            sim = PayPalSim(webhook_id=cfg.paypal_webhook_id)
            sim_app = create_sim_app(sim)
            sim_http = TestClient(sim_app, base_url=SANDBOX_BASE)
            client = PayPalClient(SANDBOX_BASE, sim.client_id, sim.client_secret, http=sim_http)  # type: ignore[arg-type]
            sim_ref = sim

            def fetch(url: str) -> bytes:
                validate_cert_url(url)
                pem = sim_ref.cert_for_url(url)
                if pem is None:
                    raise WebhookError("unknown certificate url")
                return pem

            fetcher: Any = fetch
        else:
            if cfg.effective_paypal_base == LIVE_BASE:
                raise RuntimeError("Mandate is sandbox-only: refusing to talk to live PayPal")
            client = PayPalClient(cfg.effective_paypal_base, cfg.paypal_client_id, cfg.paypal_client_secret)
            fetcher = HttpCertFetcher()

        gateway = PayPalGateway(client, guard_secret)
        disputes = DisputeAgent(db, ledger, audit, guard, gateway, backend, cfg, keys.issuer.public_key,
                                merchants.public_jwks)  # fmt: skip
        service = MandateService(settings=cfg, db=db, keys=keys, merchants=merchants, ledger=ledger, audit=audit,
                                 guard=guard, backend=backend, gateway=gateway, disputes=disputes, sim=sim,
                                 sim_http=sim_http)  # fmt: skip
        runner = runner or LocalRunner(inline=True)
        runner.register("process_event", service.process_event)
        receiver = Receiver(
            db, audit, cfg.paypal_webhook_id, fetcher, lambda eid: runner.submit("process_event", eid)
        )
        if sim is not None:

            def _deliver(headers: dict[str, str], body: bytes) -> None:
                receiver.receive(headers, body)

            sim.subscribe(_deliver)
        service.seed_registry()
        return cls(cfg, db, keys, merchants, ledger, audit, guard, backend, gateway, receiver, disputes, service,
                   runner, sim, sim_app, ContactService(db, cfg))  # fmt: skip
