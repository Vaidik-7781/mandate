"""Append-only, hash-chained audit log.

Each entry's hash covers the previous entry's hash, so editing, deleting or reordering any row breaks every
hash after it. `verify()` re-derives the whole chain and reports the first bad sequence number.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .. import clock
from ..db import AuditLog, Database

GENESIS = "0" * 64
_lock = threading.Lock()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(prev_hash: str, seq: int, actor: str, event: str, payload: dict, ts: int) -> str:
    body = canonical({"seq": seq, "actor": actor, "event": event, "payload": payload, "ts": ts})
    return hashlib.sha256(f"{prev_hash}|{body}".encode()).hexdigest()


@dataclass
class VerifyResult:
    ok: bool
    count: int
    head: str
    bad_seq: int | None = None
    reason: str = ""


class Audit:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, actor: str, event: str, payload: dict | None = None) -> AuditLog:
        payload = json.loads(canonical(payload or {}))  # normalise to JSON-native types
        with _lock, self.db.session() as s:
            last = s.execute(select(AuditLog).order_by(AuditLog.seq.desc()).limit(1)).scalar_one_or_none()
            prev = last.entry_hash if last else GENESIS
            seq = (last.seq if last else 0) + 1
            ts = clock.now()
            row = AuditLog(
                seq=seq,
                prev_hash=prev,
                entry_hash=entry_hash(prev, seq, actor, event, payload, ts),
                actor=actor,
                event=event,
                payload=payload,
                ts=ts,
            )
            s.add(row)
            s.flush()
            return row

    def head(self) -> str:
        with self.db.session() as s:
            last = s.execute(select(AuditLog).order_by(AuditLog.seq.desc()).limit(1)).scalar_one_or_none()
            return last.entry_hash if last else GENESIS

    def entries(self, limit: int = 200, event_prefix: str | None = None) -> list[dict]:
        with self.db.session() as s:
            q = select(AuditLog).order_by(AuditLog.seq.desc()).limit(limit)
            if event_prefix:
                q = (
                    select(AuditLog)
                    .where(AuditLog.event.like(f"{event_prefix}%"))
                    .order_by(AuditLog.seq.desc())
                    .limit(limit)
                )
            return [
                {
                    "seq": r.seq,
                    "ts": r.ts,
                    "actor": r.actor,
                    "event": r.event,
                    "payload": r.payload,
                    "hash": r.entry_hash,
                }
                for r in s.execute(q).scalars()
            ]

    def verify(self) -> VerifyResult:
        with self.db.session() as s:
            rows = list(s.execute(select(AuditLog).order_by(AuditLog.seq.asc())).scalars())
        prev = GENESIS
        expected_seq = 1
        for r in rows:
            if r.seq != expected_seq:
                return VerifyResult(False, len(rows), prev, r.seq, f"sequence gap: expected {expected_seq}")
            if r.prev_hash != prev:
                return VerifyResult(False, len(rows), prev, r.seq, "previous-hash link broken")
            if r.entry_hash != entry_hash(r.prev_hash, r.seq, r.actor, r.event, r.payload, r.ts):
                return VerifyResult(False, len(rows), prev, r.seq, "entry content does not match its hash")
            prev = r.entry_hash
            expected_seq += 1
        return VerifyResult(True, len(rows), prev)
