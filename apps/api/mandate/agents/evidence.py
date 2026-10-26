"""Dispute evidence: facts with ids, a cited-sentence note, a PDF packet and a completeness score.

The model may *draft* the note; code then drops any sentence that does not cite a known fact id and trims to
PayPal's 2000-character limit, so the note can never assert something the records do not show.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..db import Mandate, Order
from ..liability.facts import FactsBundle
from ..liability.matrix import Attribution, explain
from ..money import fmt
from .llm import LLMBackend, LLMResult

NOTE_LIMIT = 2000
NOTE_TARGET = 1800

# evidence_type values PayPal accepts per dispute reason (developer.paypal.com/disputes/reasons-evidence).
# There is no "proof of authorization" type: the mandate packet is submitted as OTHER (plus fulfilment proof).
ALLOWED_EVIDENCE_TYPES: dict[str, tuple[str, ...]] = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": ("PROOF_OF_FULFILLMENT", "PROOF_OF_REFUND"),
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": ("OTHER", "PROOF_OF_REFUND"),
    "UNAUTHORISED": ("PROOF_OF_FULFILLMENT", "PROOF_OF_REFUND", "OTHER"),
    "CREDIT_NOT_PROCESSED": ("PROOF_OF_REFUND", "OTHER"),
    "DUPLICATE_TRANSACTION": ("PROOF_OF_REFUND", "OTHER"),
    "INCORRECT_AMOUNT": ("PROOF_OF_REFUND", "OTHER"),
    "PAYMENT_BY_OTHER_MEANS": ("PROOF_OF_REFUND", "OTHER"),
    "CANCELED_RECURRING_BILLING": ("PROOF_OF_REFUND", "OTHER"),
    "OTHER": ("PROOF_OF_REFUND", "OTHER"),
}


def packet_evidence_type(reason: str) -> str:
    """The type the evidence packet itself is filed under: fulfilment proof when the reason takes it, else OTHER."""
    allowed = ALLOWED_EVIDENCE_TYPES.get(reason, ("OTHER",))
    if reason == "UNAUTHORISED":
        return (
            "OTHER"  # the mandate chain is the evidence; tracking goes in a second PROOF_OF_FULFILLMENT entry
        )
    return "PROOF_OF_FULFILLMENT" if "PROOF_OF_FULFILLMENT" in allowed else "OTHER"


# Evidence each dispute reason needs (fact ids). Completeness = share of these that are available.
REQUIRED: dict[str, list[str]] = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": ["F1", "F5", "F6", "F7"],
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": ["F1", "F2", "F3", "F4", "F7"],
    "UNAUTHORISED": ["F1", "F2", "F5", "F8", "F9"],
    "DUPLICATE_TRANSACTION": ["F4", "F5", "F9"],
}
DEFAULT_REQUIRED = ["F1", "F5", "F6", "F7"]


@dataclass
class EvidenceFact:
    id: str
    title: str
    text: str
    available: bool
    source: str  # where in our records it comes from

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "text": self.text, "available": self.available,
                "source": self.source}  # fmt: skip


@dataclass
class Packet:
    facts: list[EvidenceFact]
    completeness: float
    note: str
    pdf: bytes
    sha256: str
    evidence_type: str
    dropped_sentences: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "completeness": self.completeness,
            "sha256": self.sha256,
            "note_chars": len(self.note),
            "facts": [f.to_dict() for f in self.facts],
            "missing": self.missing,
            "dropped_sentences": self.dropped_sentences,
            "evidence_type": self.evidence_type,
        }


def _ts(t: int | None) -> str:
    return "n/a" if t is None else datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_facts(
    order: Order,
    mandate: Mandate | None,
    bundle: FactsBundle,
    *,
    audit_head: str,
    audit_ok: bool,
) -> list[EvidenceFact]:
    rep = bundle.report
    f = bundle.facts
    items = ", ".join(f"{i['quantity']} x {i['name']} ({i['sku']})" for i in order.items) or "n/a"
    total = fmt(rep.checkout_total_minor, order.currency) if rep and rep.checkout_total_minor else "n/a"
    t = order.tracking or {}
    return [
        EvidenceFact(
            "F1",
            "Verified authorisation",
            f"The buyer signed a delegation (mandate {order.mandate_id[:8]}) and the credential chain verified "
            f"at purchase time ({', '.join(rep.checks_performed[:3]) if rep else 'no report'}).",
            f.p1,
            "layer 1 + layer 2 + layer 3 credentials",
        ),
        EvidenceFact(
            "F2",
            "Constraints satisfied",
            f"The purchase of {items} from {(rep.merchant or {}).get('name', order.merchant_id) if rep else order.merchant_id} "
            "was inside every limit the buyer signed (merchant, item, amount, quantity).",
            f.p2,
            "constraint evaluation",
        ),
        EvidenceFact(
            "F3",
            "Merchant-signed cart",
            f"The merchant signed a cart for {items} totalling {total}; the agent's final checkout mandate "
            "commits to the hash of that signed cart.",
            bool(rep and rep.checkout_payload),
            "merchant checkout JWT",
        ),
        EvidenceFact(
            "F4",
            "Charge equals signed cart",
            f"The amount charged ({fmt(order.captured_amount_minor or order.amount_minor, order.currency)}) "
            f"equals the signed cart total ({total}).",
            f.p5,
            "capture record vs signed cart",
        ),
        EvidenceFact(
            "F5",
            "Payment captured",
            f"Payment was captured at {_ts(order.captured_at)} (capture {order.capture_id}).",
            bool(order.capture_id and order.captured_at),
            "PayPal capture",
        ),
        EvidenceFact(
            "F6",
            "Shipment tracking",
            f"The order shipped with {t.get('carrier', 'carrier')} tracking number {t.get('tracking_number', 'n/a')}.",
            bool(order.tracking),
            "PayPal tracker",
        ),
        EvidenceFact(
            "F7",
            "Delivery confirmed",
            f"Delivery was confirmed at {_ts(order.delivered_at)}.",
            order.delivered_at is not None,
            "carrier status",
        ),
        EvidenceFact(
            "F8",
            "Authorisation active at capture",
            "The buyer's authorisation had not been revoked or paused when the payment was captured.",
            f.p7,
            "mandate ledger",
        ),
        EvidenceFact(
            "F9",
            "Payment tied to the credential chain",
            f"The PayPal order carries custom_id {order.chain_digest}, the hash of the signed credential chain, "
            "and each final mandate can be used once.",
            bool(order.chain_digest),
            "PayPal custom_id + replay set",
        ),
        EvidenceFact(
            "F10",
            "Tamper-evident audit log",
            f"The decision record is in a hash-chained audit log (head {audit_head[:16]}...); "
            + ("the chain verifies." if audit_ok else "verification FAILED."),
            audit_ok,
            "audit log",
        ),
    ]


def completeness(reason: str, facts: list[EvidenceFact]) -> tuple[float, list[str]]:
    need = REQUIRED.get(reason, DEFAULT_REQUIRED)
    have = {f.id for f in facts if f.available}
    missing = [i for i in need if i not in have]
    return round((len(need) - len(missing)) / len(need), 2), missing


# ---------------------------------------------------------------------------------------------------------
# Note: model drafts, code filters
# ---------------------------------------------------------------------------------------------------------
class NoteOut(BaseModel):
    note: str = ""


_CITE = re.compile(r"\[(F\d+(?:\s*,\s*F\d+)*)\]")
_SENT = re.compile(r"(?:(?<=[.!?])|(?<=\]))\s+(?=[A-Z])")


def _offline_note(payload: dict) -> dict:
    avail = [f for f in payload["facts"] if f["available"]]
    order = {"F1": 0, "F3": 1, "F2": 2, "F4": 3, "F5": 4, "F6": 5, "F7": 6, "F8": 7, "F9": 8, "F10": 9}
    avail.sort(key=lambda f: order.get(f["id"], 99))
    parts = [f"{f['text'].rstrip('.')}. [{f['id']}]" for f in avail]
    return {"note": " ".join(parts)}


def filter_note(note: str, valid_ids: set[str], limit: int = NOTE_TARGET) -> tuple[str, list[str]]:
    """Keep only sentences that cite at least one known, *available* fact id and cite nothing else."""
    kept: list[str] = []
    dropped: list[str] = []
    total = 0
    for sent in _SENT.split(note.strip()):
        s = sent.strip()
        if not s:
            continue
        cited = [c.strip() for m in _CITE.finditer(s) for c in m.group(1).split(",")]
        if not cited or any(c not in valid_ids for c in cited):
            dropped.append(s[:120])
            continue
        if total + len(s) + 1 > limit:
            dropped.append(s[:120] + " (over length)")
            continue
        kept.append(s)
        total += len(s) + 1
    return " ".join(kept), dropped


def draft_note(backend: LLMBackend, facts: list[EvidenceFact]) -> tuple[str, list[str], LLMResult]:
    payload = {"facts": [f.to_dict() for f in facts]}
    res = backend.complete_json("evidence_note", payload, NoteOut, _offline_note)
    valid = {f.id for f in facts if f.available}
    note, dropped = filter_note(NoteOut.model_validate(res.data).note, valid)
    if not note:  # a model that produced nothing usable: fall back to the deterministic note
        note, more = filter_note(_offline_note(payload)["note"], valid)
        dropped += more
    return note, dropped, res


# ---------------------------------------------------------------------------------------------------------
# PDF packet
# ---------------------------------------------------------------------------------------------------------
def render_pdf(
    *,
    dispute: dict,
    order: Order,
    facts: list[EvidenceFact],
    attribution: Attribution,
    note: str,
    completeness_score: float,
    audit_head: str,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch, invariant=1,
                            title="Mandate evidence packet", author="Mandate")  # fmt: skip
    st = getSampleStyleSheet()
    body = st["BodyText"]
    story: list = [
        Paragraph("Mandate evidence packet", st["Title"]),
        Paragraph(
            "Generated from the merchant's own records. Mandate is a sandbox project, not legal advice, and is "
            "not endorsed by PayPal.",
            st["Italic"],
        ),
        Spacer(1, 10),
        Paragraph("Dispute", st["Heading2"]),
    ]
    amt = dispute.get("dispute_amount", {})
    rows = [
        ["Dispute id", dispute.get("dispute_id", "")],
        ["Reason", dispute.get("reason", "")],
        ["Stage / status", f"{dispute.get('dispute_life_cycle_stage', '')} / {dispute.get('status', '')}"],
        ["Disputed amount", f"{amt.get('value', '')} {amt.get('currency_code', '')}"],
        ["Order", order.id],
        ["PayPal order / capture", f"{order.paypal_order_id} / {order.capture_id}"],
        ["Credential chain digest", order.chain_digest],
        ["Audit log head", audit_head],
        ["Evidence completeness", f"{completeness_score:.0%}"],
    ]
    tbl = Table(rows, colWidths=[1.8 * inch, 4.9 * inch])
    tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                             ("FONTSIZE", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke)]))  # fmt: skip
    story += [
        tbl,
        Spacer(1, 10),
        Paragraph("Liability analysis (Liability Matrix v0.1, a proposal)", st["Heading2"]),
    ]
    story.append(Paragraph(explain(attribution), body))
    story += [Spacer(1, 10), Paragraph("Evidence facts", st["Heading2"])]
    frows = [["ID", "Fact", "Present", "Source"]]
    for f in facts:
        frows.append(
            [f.id, Paragraph(f"<b>{f.title}.</b> {f.text}", body), "yes" if f.available else "NO", f.source]
        )
    ft = Table(frows, colWidths=[0.4 * inch, 4.1 * inch, 0.6 * inch, 1.6 * inch], repeatRows=1)
    ft.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))  # fmt: skip
    story += [
        ft,
        Spacer(1, 10),
        Paragraph("Note sent to PayPal", st["Heading2"]),
        Paragraph(note or "(none)", body),
    ]
    doc.build(story)
    return buf.getvalue()


def build_packet(
    backend: LLMBackend,
    *,
    dispute: dict,
    order: Order,
    mandate: Mandate | None,
    bundle: FactsBundle,
    attribution: Attribution,
    audit_head: str,
    audit_ok: bool,
) -> Packet:
    facts = build_facts(order, mandate, bundle, audit_head=audit_head, audit_ok=audit_ok)
    reason = dispute.get("reason", "")
    score, missing = completeness(reason, facts)
    note, dropped, _ = draft_note(backend, facts)
    pdf = render_pdf(dispute=dispute, order=order, facts=facts, attribution=attribution, note=note,
                     completeness_score=score, audit_head=audit_head)  # fmt: skip
    ev_type = packet_evidence_type(reason)
    return Packet(facts, score, note, pdf, hashlib.sha256(pdf).hexdigest(), ev_type, dropped, missing)
