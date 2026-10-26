"""Signed purchase receipt: a JSON document (canonical, ES256-signed) plus a PDF rendering of it.

The receipt lets anyone holding the Mandate receipt public key check that the figures, the credential chain
digest and the audit-log head were issued together and not edited afterwards.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..credentials.keys import KeyPair
from ..db import Mandate, Order
from ..money import fmt

RECEIPT_VERSION = "mandate-receipt-1"


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def build_receipt(
    order: Order, mandate: Mandate, decision_checks: list[dict], audit_head: str, key: KeyPair
) -> dict:
    body = {
        "version": RECEIPT_VERSION,
        "order_id": order.id,
        "paypal_order_id": order.paypal_order_id,
        "capture_id": order.capture_id,
        "merchant_id": order.merchant_id,
        "items": order.items,
        "amount_minor": order.captured_amount_minor
        if order.captured_amount_minor is not None
        else order.amount_minor,
        "currency": order.currency,
        "mandate_id": mandate.id,
        "mandate_chain_digest": mandate.chain_digest,
        "order_chain_digest": order.chain_digest,
        "authorised_by": mandate.user_id,
        "captured_at": order.captured_at,
        "guard_checks": decision_checks,
        "audit_head": audit_head,
        "note": "Sandbox record. Not a tax document. Not endorsed by PayPal.",
    }
    sig = key.private_key.sign(_canon(body), ec.ECDSA(hashes.SHA256()))
    return {
        "receipt": body,
        "signature": base64.urlsafe_b64encode(sig).decode().rstrip("="),
        "kid": key.kid,
        "alg": "ES256-DER",
    }


def verify_receipt(doc: dict, public_key: ec.EllipticCurvePublicKey, kid: str | None = None) -> bool:
    """The envelope fields `alg` and `kid` are not covered by the signature, so they are pinned here instead:
    a different algorithm label or key id is rejected rather than ignored."""
    try:
        if doc.get("alg") != "ES256-DER" or (kid is not None and doc.get("kid") != kid):
            return False
        raw = doc["signature"] + "=" * (-len(doc["signature"]) % 4)
        public_key.verify(base64.urlsafe_b64decode(raw), _canon(doc["receipt"]), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


def render_receipt_pdf(doc: dict) -> bytes:
    r = doc["receipt"]
    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch, invariant=1,
                            title="Mandate purchase receipt", author="Mandate")  # fmt: skip
    st = getSampleStyleSheet()
    body = st["BodyText"]
    story: list = [
        Paragraph("Mandate purchase receipt", st["Title"]),
        Paragraph(r["note"], st["Italic"]),
        Spacer(1, 10),
    ]
    rows = [["Order", r["order_id"]], ["PayPal order / capture", f"{r['paypal_order_id']} / {r['capture_id']}"],
            ["Merchant", r["merchant_id"]], ["Amount", fmt(r["amount_minor"], r["currency"])],
            ["Authorised by", r["authorised_by"]], ["Mandate", r["mandate_id"]],
            ["Credential chain digest", r["order_chain_digest"]], ["Audit log head", r["audit_head"]]]  # fmt: skip
    t = Table([[a, Paragraph(str(b), body)] for a, b in rows], colWidths=[1.9 * inch, 4.8 * inch])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8),
                           ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke), ("VALIGN", (0, 0), (-1, -1), "TOP")]))  # fmt: skip
    story += [t, Spacer(1, 8), Paragraph("Items", st["Heading3"])]
    for i in r["items"]:
        story.append(
            Paragraph(
                f"{i['quantity']} x {i['name']} ({i['sku']}) at {fmt(i['unit_price_minor'], r['currency'])}",
                body,
            )
        )
    story += [Spacer(1, 8), Paragraph("Guard checks passed", st["Heading3"])]
    for c in r["guard_checks"]:
        story.append(Paragraph(f"{'PASS' if c.get('ok') else 'FAIL'}: {c.get('check')}", body))
    story += [Spacer(1, 8), Paragraph("Signature", st["Heading3"]),
              Paragraph(f"{doc['alg']} key {doc['kid']}<br/>{doc['signature']}", body)]  # fmt: skip
    pdf.build(story)
    return buf.getvalue()
