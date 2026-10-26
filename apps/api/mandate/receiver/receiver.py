"""Webhook receiver: verify the raw body, store the event once, queue the task, answer 2xx fast.

PayPal retries failed deliveries up to 25 times over 3 days, so every step here is idempotent: the event id is
the primary key of `webhook_events`; a duplicate delivery is acknowledged and dropped.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from .. import clock
from ..audit.log import Audit
from ..db import Database, WebhookEvent
from ..gateway.paypal.webhooks import CertFetcher, WebhookError, verify_signature

SUBSCRIBED_EVENTS = (
    "CHECKOUT.ORDER.APPROVED",
    "PAYMENT.CAPTURE.COMPLETED",
    "PAYMENT.CAPTURE.DECLINED",
    "PAYMENT.CAPTURE.PENDING",
    "PAYMENT.CAPTURE.REFUNDED",
    "PAYMENT.CAPTURE.REVERSED",
    "CUSTOMER.DISPUTE.CREATED",
    "CUSTOMER.DISPUTE.UPDATED",
    "CUSTOMER.DISPUTE.RESOLVED",
)


@dataclass
class ReceiveResult:
    status_code: int
    detail: str
    event_id: str | None = None
    duplicate: bool = False


class Receiver:
    def __init__(
        self,
        db: Database,
        audit: Audit,
        webhook_id: str,
        cert_fetcher: CertFetcher,
        enqueue: Callable[[str], None],
    ) -> None:
        self.db, self.audit = db, audit
        self.webhook_id, self.cert_fetcher, self.enqueue = webhook_id, cert_fetcher, enqueue

    def receive(self, headers: dict[str, str], raw_body: bytes) -> ReceiveResult:
        try:
            verify_signature(headers, raw_body, self.webhook_id, self.cert_fetcher)
        except WebhookError as exc:
            self.audit.append("receiver", "webhook.rejected", {"reason": str(exc)})
            return ReceiveResult(401, f"signature verification failed: {exc}")
        try:
            event = json.loads(raw_body)
            event_id, event_type = event["id"], event["event_type"]
        except (ValueError, KeyError, TypeError):
            return ReceiveResult(400, "malformed event")
        try:
            with self.db.session() as s:
                s.add(
                    WebhookEvent(
                        event_id=event_id,
                        event_type=event_type,
                        raw_body=raw_body.decode("utf-8"),
                        verified=True,
                        received_at=clock.now(),
                        status="received",
                    )
                )
        except IntegrityError:
            return ReceiveResult(200, "duplicate delivery ignored", event_id, duplicate=True)
        self.audit.append("receiver", "webhook.received", {"event_id": event_id, "event_type": event_type})
        self.enqueue(event_id)
        return ReceiveResult(200, "accepted", event_id)
