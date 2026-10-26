"""Database layer: SQLAlchemy 2 models for every table in the build plan.

Timestamps are integer unix seconds (matches JWT iat/exp and avoids SQLite timezone surprises).
Amounts are integers in minor units.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from .settings import get_settings


class Base(DeclarativeBase):
    pass


class Mandate(Base):
    __tablename__ = "mandates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    chain_digest: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|paused|revoked|expired
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    paused_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    budget_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    max_occurrences: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intent: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # compiled intent + descriptive parts
    l1: Mapped[str] = mapped_column(Text)
    l2: Mapped[str] = mapped_column(Text)
    ambiguity_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mandate_id: Mapped[str] = mapped_column(ForeignKey("mandates.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # reserve|commit|release
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[int] = mapped_column(BigInteger)


class ReplaySet(Base):
    __tablename__ = "replay_set"
    transaction_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mandate_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # internal id, sent as invoice_id
    paypal_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    mandate_id: Mapped[str] = mapped_column(ForeignKey("mandates.id"), index=True)
    chain_digest: Mapped[str] = mapped_column(String(64), index=True)  # == custom_id sent to PayPal
    merchant_id: Mapped[str] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String(24), default="CREATED")
    capture_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    captured_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    refunded_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    tracking: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    items: Mapped[list[Any]] = mapped_column(JSON, default=list)
    checkout_jwt: Mapped[str] = mapped_column(Text, default="")
    l3a: Mapped[str] = mapped_column(Text, default="")
    l3b: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(BigInteger)
    captured_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivered_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AgentDecision(Base):
    __tablename__ = "agent_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mandate_id: Mapped[str] = mapped_column(ForeignKey("mandates.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    inputs: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{label, source, sha256, summary}]
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    verdict: Mapped[str] = mapped_column(String(24), default="")  # allow|deny|step_up
    taint_flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[int] = mapped_column(BigInteger)


class AuditLog(Base):
    __tablename__ = "audit_log"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True)
    actor: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ts: Mapped[int] = mapped_column(BigInteger)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    raw_body: Mapped[str] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[int] = mapped_column(BigInteger)
    processed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="received")  # received|processed|failed


class Dispute(Base):
    __tablename__ = "disputes"
    dispute_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(String(64), default="")
    stage: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(48), default="")
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    due_time: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fault_split: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    rule_id: Mapped[str | None] = mapped_column(String(8), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(24), nullable=True)  # fight|accept|offer|human
    outcome: Mapped[str | None] = mapped_column(String(48), nullable=True)
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class EvidencePacket(Base):
    __tablename__ = "evidence_packets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.dispute_id"), index=True)
    completeness: Mapped[float] = mapped_column(Float)
    sha256: Mapped[str] = mapped_column(String(64))
    pdf_path: Mapped[str] = mapped_column(String(512), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Registry(Base):
    __tablename__ = "registry"
    merchant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    website: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="allowed")  # allowed|blocked


class Approval(Base):
    """Step-up queue (B5): out-of-bounds or tainted actions wait here for a human."""

    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # purchase|dispute
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|approved|declined
    created_at: Mapped[int] = mapped_column(BigInteger)
    decided_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class BenchRun(Base):
    __tablename__ = "bench_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    configuration: Mapped[str] = mapped_column(String(8))
    model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class BenchResult(Base):
    __tablename__ = "bench_results"
    __table_args__ = (UniqueConstraint("run_id", "scenario_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("bench_runs.id"), index=True)
    scenario_id: Mapped[str] = mapped_column(String(32))
    family: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(24))
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)


def normalize_url(url: str) -> str:
    """Render and Heroku hand out `postgres://` or `postgresql://`; SQLAlchemy needs the psycopg 3 driver name."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


class ContactMessage(Base):
    """Messages from the website's contact form. The sender's address is stored only as a salted hash."""

    __tablename__ = "contact_messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(150), default="")
    message: Mapped[str] = mapped_column(Text)
    sender_hash: Mapped[str] = mapped_column(String(64), default="")
    emailed: Mapped[bool] = mapped_column(Boolean, default=False)


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, url: str | None = None) -> None:
        url = normalize_url(url or get_settings().database_url)
        kwargs: dict[str, Any] = {}
        # An in-memory SQLite database is one connection shared by every thread. Two threads cannot safely
        # interleave transactions on it, so sessions take turns. File SQLite and Postgres use real pools.
        self._serial: threading.RLock | None = None
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool
                self._serial = threading.RLock()
        self.engine: Engine = create_engine(url, future=True, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def _pragma(dbapi_conn: Any, _rec: Any) -> None:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                if ":memory:" not in url and url not in ("sqlite://",):
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        self._factory = sessionmaker(self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        if self._serial is not None:
            self._serial.acquire()
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
            if self._serial is not None:
                self._serial.release()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
        _db.create_all()
    return _db


def set_db(db: Database | None) -> None:
    global _db
    _db = db
