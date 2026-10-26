"""The website's contact form: validation, storage, optional email notification.

Messages are always stored. Email is a convenience: it needs SMTP settings and a recipient, runs on a background
thread, and a failure never loses the message or fails the request. The sender's network address is not stored,
only a salted hash used to rate-limit.
"""

from __future__ import annotations

import hashlib
import re
import smtplib
import threading
import time
import uuid
from collections import defaultdict, deque
from email.message import EmailMessage

from pydantic import BaseModel, Field, field_validator

from . import clock
from .db import ContactMessage, Database
from .settings import Settings

EMAIL_RE = re.compile(r"^[^@\s<>\",;]{1,64}@[^@\s<>\",;]{1,190}\.[A-Za-z]{2,}$")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_PER_HOUR = 5


def _tidy(s: str, multiline: bool = False) -> str:
    s = _CTRL.sub("", s).replace("\r\n", "\n").replace("\r", "\n")
    if not multiline:
        s = " ".join(s.split())
    return s.strip()


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=6, max_length=200)
    subject: str = Field("", max_length=150)
    message: str = Field(min_length=10, max_length=3000)
    website: str = Field("", max_length=200)  # honeypot: humans never see this field

    @field_validator("name", "subject")
    @classmethod
    def _one_line(cls, v: str) -> str:
        return _tidy(v)

    @field_validator("message")
    @classmethod
    def _body(cls, v: str) -> str:
        v = _tidy(v, multiline=True)
        if len(v) < 10:
            raise ValueError("message is too short")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL_RE.match(v):
            raise ValueError("enter a valid email address")
        return v


class ContactService:
    def __init__(self, db: Database, cfg: Settings) -> None:
        self.db, self.cfg = db, cfg
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _sender_hash(self, ip: str) -> str:
        return hashlib.sha256(f"contact|{self.cfg.secret_seed}|{ip}".encode()).hexdigest()

    def submit(self, data: ContactIn, ip: str) -> dict:
        """Store a message. Returns {"ok": True}; raises PermissionError when the sender is over the limit."""
        if data.website.strip():  # a bot filled the hidden field: pretend success, keep nothing
            return {"ok": True}
        who = self._sender_hash(ip)
        now = time.monotonic()
        with self._lock:
            q = self._recent[who]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= MAX_PER_HOUR:
                raise PermissionError("too many messages from this address; try again in an hour")
            q.append(now)
        mid = uuid.uuid4().hex[:12]
        row = ContactMessage(id=mid, created_at=clock.now(), name=data.name, email=data.email, subject=data.subject,
                             message=data.message, sender_hash=who, emailed=False)  # fmt: skip
        with self.db.session() as s:
            s.add(row)
        if self.cfg.smtp_host and self.cfg.contact_to:
            threading.Thread(target=self._send, args=(mid, data), daemon=True).start()
        return {"ok": True, "id": mid}

    def _send(self, mid: str, data: ContactIn) -> bool:
        cfg = self.cfg
        try:
            msg = EmailMessage()
            msg["Subject"] = _tidy(f"[Mandate contact] {data.subject or 'New message'}")[:200]
            msg["From"] = cfg.smtp_from or cfg.smtp_user or cfg.contact_to
            msg["To"] = cfg.contact_to
            msg["Reply-To"] = data.email
            msg.set_content(f"From: {data.name} <{data.email}>\n\n{data.message}\n")
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as smtp:
                smtp.starttls()
                if cfg.smtp_user:
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                smtp.send_message(msg)
            with self.db.session() as s:
                row = s.get(ContactMessage, mid)
                if row is not None:
                    row.emailed = True
            return True
        except Exception:  # noqa: BLE001 - the message is already stored; never raise from a background thread
            return False

    def list(self, limit: int = 100) -> list[dict]:
        from sqlalchemy import select

        with self.db.session() as s:
            rows = s.execute(
                select(ContactMessage).order_by(ContactMessage.created_at.desc()).limit(limit)
            ).scalars()
            return [{"id": r.id, "created_at": r.created_at, "name": r.name, "email": r.email, "subject": r.subject,
                     "message": r.message, "emailed": r.emailed} for r in rows]  # fmt: skip
