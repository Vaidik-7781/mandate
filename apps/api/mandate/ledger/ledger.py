"""Stateful ledger: cumulative spend, occurrence counts, replay set, revocation, velocity breaker.

The Verifiable Intent spec leaves cumulative budgets, occurrence counts and revocation to the verifier. This is
that verifier state. All mutations happen inside one transaction that first locks the mandate row, so two
concurrent purchases cannot both slip under the budget (Postgres: SELECT ... FOR UPDATE; SQLite serialises
writers).

Accounting (minor units):
    exposure = sum(reserve) - sum(release) - sum(refund)      (commit moves a hold to spent; it changes nothing)
    a purchase is allowed only if exposure + amount <= budget
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..db import Database, LedgerEntry, Mandate, ReplaySet
from ..settings import get_settings

ACTIVE, PAUSED, REVOKED, EXPIRED = "active", "paused", "revoked", "expired"


@dataclass
class ReserveResult:
    ok: bool
    code: str  # OK | REVOKED | PAUSED | EXPIRED | BUDGET | OCCURRENCES | REPLAY | VELOCITY | UNKNOWN_MANDATE | CURRENCY
    detail: str = ""
    entry_id: int | None = None
    exposure_minor: int = 0
    remaining_minor: int = 0
    idempotent: bool = False


@dataclass
class Balance:
    budget_minor: int
    reserved_minor: int  # held, not yet committed
    spent_minor: int  # committed, net of refunds
    exposure_minor: int
    remaining_minor: int
    occurrences: int


class Ledger:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- mandates ---------------------------------------------------------------------------------------
    def register(self, m: Mandate) -> None:
        with self.db.session() as s:
            s.add(m)

    def get(self, mandate_id: str) -> Mandate | None:
        with self.db.session() as s:
            return s.get(Mandate, mandate_id)

    def status(self, mandate_id: str) -> str:
        m = self.get(mandate_id)
        if m is None:
            return "unknown"
        if m.status == ACTIVE and clock.now() >= m.expires_at:
            return EXPIRED
        return m.status

    def revoke(self, mandate_id: str, reason: str = "user") -> int | None:
        """Kill switch. Returns the revocation time (None if the mandate is unknown)."""
        with self.db.session() as s:
            m = s.get(Mandate, mandate_id, with_for_update=True)
            if m is None:
                return None
            if m.status != REVOKED:
                m.status = REVOKED
                m.revoked_at = clock.now()
            return m.revoked_at

    def pause(self, mandate_id: str) -> None:
        with self.db.session() as s:
            m = s.get(Mandate, mandate_id, with_for_update=True)
            if m and m.status == ACTIVE:
                m.status = PAUSED
                m.paused_at = clock.now()

    def resume(self, mandate_id: str) -> None:
        with self.db.session() as s:
            m = s.get(Mandate, mandate_id, with_for_update=True)
            if m and m.status == PAUSED:
                m.status = ACTIVE
                m.paused_at = None

    def was_revoked_at(self, mandate_id: str, ts: int) -> bool:
        """True if the mandate had been revoked at or before `ts` (used for P7 at capture time)."""
        m = self.get(mandate_id)
        return bool(m and m.revoked_at is not None and m.revoked_at <= ts)

    # -- balances ---------------------------------------------------------------------------------------
    def _sum(self, s: Session, mandate_id: str, kind: str) -> int:
        return int(
            s.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
                    LedgerEntry.mandate_id == mandate_id, LedgerEntry.kind == kind
                )
            ).scalar_one()
        )

    def _balance(self, s: Session, m: Mandate) -> Balance:
        r = self._sum(s, m.id, "reserve")
        c = self._sum(s, m.id, "commit")
        rel = self._sum(s, m.id, "release")
        rf = self._sum(s, m.id, "refund")
        occ = int(
            s.execute(
                select(func.count()).where(LedgerEntry.mandate_id == m.id, LedgerEntry.kind == "reserve")
            ).scalar_one()
        ) - int(
            s.execute(
                select(func.count()).where(LedgerEntry.mandate_id == m.id, LedgerEntry.kind == "release")
            ).scalar_one()
        )
        exposure = r - rel - rf
        return Balance(
            budget_minor=m.budget_minor,
            reserved_minor=r - c - rel,
            spent_minor=c - rf,
            exposure_minor=exposure,
            remaining_minor=m.budget_minor - exposure,
            occurrences=max(occ, 0),
        )

    def balance(self, mandate_id: str) -> Balance | None:
        with self.db.session() as s:
            m = s.get(Mandate, mandate_id)
            return self._balance(s, m) if m else None

    # -- velocity breaker -------------------------------------------------------------------------------
    def _record_attempt(self, s: Session, m: Mandate) -> bool:
        """Record an attempt; returns True if the breaker trips (mandate is paused)."""
        cfg = get_settings()
        now = clock.now()
        s.add(
            LedgerEntry(
                mandate_id=m.id,
                kind="attempt",
                amount_minor=0,
                order_id=None,
                idempotency_key=f"attempt:{uuid.uuid4().hex}",
                created_at=now,
            )
        )
        s.flush()
        n = int(
            s.execute(
                select(func.count()).where(
                    LedgerEntry.mandate_id == m.id,
                    LedgerEntry.kind == "attempt",
                    LedgerEntry.created_at >= now - cfg.velocity_window_seconds,
                )
            ).scalar_one()
        )
        if n > cfg.velocity_max_attempts:
            m.status = PAUSED
            m.paused_at = now
            return True
        return False

    # -- reserve / commit / release / refund -------------------------------------------------------------
    def reserve(
        self,
        mandate_id: str,
        amount_minor: int,
        idempotency_key: str,
        *,
        currency: str,
        transaction_id: str,
        order_id: str | None = None,
    ) -> ReserveResult:
        now = clock.now()
        with self.db.session() as s:
            m = s.get(Mandate, mandate_id, with_for_update=True)
            if m is None:
                return ReserveResult(False, "UNKNOWN_MANDATE", "no such mandate")

            # Idempotent retry: same key returns the original reservation untouched.
            prior = s.execute(
                select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            if prior is not None:
                bal = self._balance(s, m)
                return ReserveResult(
                    True, "OK", "idempotent retry", prior.id, bal.exposure_minor, bal.remaining_minor, True
                )

            if m.status == ACTIVE and now >= m.expires_at:
                m.status = EXPIRED
            if m.status == REVOKED:
                return ReserveResult(False, "REVOKED", "mandate was revoked")
            if m.status == EXPIRED:
                return ReserveResult(False, "EXPIRED", "mandate expired")
            if m.status == PAUSED:
                return ReserveResult(False, "PAUSED", "mandate is paused")

            if self._record_attempt(s, m):
                return ReserveResult(False, "VELOCITY", "velocity breaker tripped; mandate paused")

            if currency != m.currency:
                return ReserveResult(False, "CURRENCY", f"mandate currency is {m.currency}")

            bal = self._balance(s, m)
            if m.max_occurrences is not None and bal.occurrences >= m.max_occurrences:
                return ReserveResult(False, "OCCURRENCES", f"occurrence cap {m.max_occurrences} reached")
            if bal.exposure_minor + amount_minor > m.budget_minor:
                return ReserveResult(
                    False,
                    "BUDGET",
                    f"{bal.exposure_minor} + {amount_minor} exceeds budget {m.budget_minor}",
                    exposure_minor=bal.exposure_minor,
                    remaining_minor=bal.remaining_minor,
                )

            if s.get(ReplaySet, transaction_id) is not None:
                return ReserveResult(False, "REPLAY", "this final mandate was already used")
            try:
                s.add(ReplaySet(transaction_id=transaction_id, mandate_id=m.id, created_at=now))
                s.flush()
            except (
                IntegrityError
            ):  # lost a race with a concurrent identical request; unique key is the backstop
                s.rollback()
                return ReserveResult(False, "REPLAY", "this final mandate was already used")

            entry = LedgerEntry(
                mandate_id=m.id,
                kind="reserve",
                amount_minor=amount_minor,
                order_id=order_id,
                idempotency_key=idempotency_key,
                created_at=now,
            )
            s.add(entry)
            s.flush()
            exposure = bal.exposure_minor + amount_minor
            return ReserveResult(True, "OK", "reserved", entry.id, exposure, m.budget_minor - exposure)

    def _move(self, kind: str, mandate_id: str, amount_minor: int, order_id: str, key: str) -> bool:
        """Insert a commit/release/refund entry once (unique idempotency key). Returns False on duplicate."""
        with self.db.session() as s:
            m = s.get(Mandate, mandate_id, with_for_update=True)
            if m is None:
                return False
            exists = s.execute(select(LedgerEntry.id).where(LedgerEntry.idempotency_key == key)).first()
            if exists:
                return False
            s.add(
                LedgerEntry(
                    mandate_id=mandate_id,
                    kind=kind,
                    amount_minor=amount_minor,
                    order_id=order_id,
                    idempotency_key=key,
                    created_at=clock.now(),
                )
            )
            return True

    def commit(self, mandate_id: str, amount_minor: int, order_id: str) -> bool:
        """Move a hold to spent. If the hold was already released (the order was cancelled but PayPal captured it
        anyway, for example a webhook that arrives after a revocation sweep), first re-reserve the amount so the
        exposure never undercounts: the money really left the buyer."""
        with self.db.session() as s:
            m = s.get(Mandate, mandate_id, with_for_update=True)
            if m is None:
                return False
            key = f"commit:{order_id}"
            if s.execute(select(LedgerEntry.id).where(LedgerEntry.idempotency_key == key)).first():
                return False
            released = s.execute(
                select(LedgerEntry.id).where(LedgerEntry.idempotency_key == f"release:{order_id}")
            ).first()
            now = clock.now()
            if released:
                s.add(LedgerEntry(mandate_id=mandate_id, kind="reserve", amount_minor=amount_minor,
                                  order_id=order_id, idempotency_key=f"rereserve:{order_id}", created_at=now))  # fmt: skip
            s.add(LedgerEntry(mandate_id=mandate_id, kind="commit", amount_minor=amount_minor, order_id=order_id,
                              idempotency_key=key, created_at=now))  # fmt: skip
            return True

    def release(self, mandate_id: str, amount_minor: int, order_id: str) -> bool:
        return self._move("release", mandate_id, amount_minor, order_id, f"release:{order_id}")

    def refund(self, mandate_id: str, amount_minor: int, order_id: str, tag: str = "") -> bool:
        return self._move("refund", mandate_id, amount_minor, order_id, f"refund:{order_id}:{tag}")

    def in_flight(self, mandate_id: str) -> list[str]:
        """Order ids with a reserve but no commit/release yet (used by the kill switch)."""
        with self.db.session() as s:
            reserved = {
                r.order_id
                for r in s.execute(
                    select(LedgerEntry).where(
                        LedgerEntry.mandate_id == mandate_id, LedgerEntry.kind == "reserve"
                    )
                ).scalars()
                if r.order_id
            }
            closed = {
                r.order_id
                for r in s.execute(
                    select(LedgerEntry).where(
                        LedgerEntry.mandate_id == mandate_id, LedgerEntry.kind.in_(["commit", "release"])
                    )
                ).scalars()
                if r.order_id
            }
            return sorted(reserved - closed)
