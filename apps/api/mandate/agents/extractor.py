"""Quarantined extractor: reads UNTRUSTED text (catalog descriptions, reviews, dispute messages) with no tools and
returns fixed fields only. The planner and the dispute agent see this output, never the raw text.

The offline implementation is a deterministic pattern detector. With a real model the output is a union of the
model's answer and these patterns, so a model that misses an obvious injection is still caught.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .llm import LLMBackend, LLMResult

INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore\s+(all\s+|any\s+|the\s+)?(previous\s+|prior\s+|above\s+|earlier\s+)?(instructions|rules|limits|constraints|spending)",
        r"disregard\s+(the\s+|all\s+|any\s+)?(previous|prior|above|mandate|limits|instructions|rules)",
        r"\b(system|assistant|developer)\s*:",
        r"\byou\s+(must|should|need\s+to|have\s+to)\s+(now\s+)?(buy|purchase|order|override|transfer|send|pay|refund|accept)",
        r"override\s+(the\s+)?(mandate|limits?|budget|instructions|policy)",
        r"do\s+not\s+(tell|mention|inform|notify|alert)\s+(the\s+)?(user|buyer|customer|merchant)",
        r"\b(buy|purchase|order)\s+(sku\s+|item\s+)?[A-Z]{2,4}-\d+\s+(from|at)\s+\S+",
        r"\binstead\s+(buy|purchase|order|refund|accept)",
        r"new\s+instructions?\s*:",
        r"\b(send|transfer|wire)\s+(the\s+)?(money|funds|payment)\b",
        r"(refund|accept)\s+(the\s+)?(buyer|claim|dispute)\s+(in\s+full|immediately|now)",
        r"skip\s+(the\s+)?(evidence|review|approval|verification)",
    )
]
_POSITIVE = ("great", "love", "comfortable", "fast", "solid", "sturdy", "dry", "plush", "good", "excellent")
_NEGATIVE = ("narrow", "pricey", "broke", "poor", "bad", "uncomfortable", "leaks", "slow", "returned")
_SPLIT = re.compile(r"(?<=[.!?])\s+")


class Extracted(BaseModel):
    title: str = ""
    price_minor: int | None = None
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)
    rating_summary: str = ""
    suspected_instruction: bool = False
    flagged_spans: list[str] = Field(default_factory=list)


def detect_injection(text: str) -> list[str]:
    """Return the sentences of `text` that look like instructions aimed at an AI."""
    spans: list[str] = []
    for sent in _SPLIT.split(text.strip()):
        if any(p.search(sent) for p in INJECTION_PATTERNS) and sent not in spans:
            spans.append(sent.strip()[:300])
    # a pattern may straddle a sentence boundary; also test the whole text
    if not spans and any(p.search(text) for p in INJECTION_PATTERNS):
        spans.append(text.strip()[:300])
    return spans


def _offline(payload: dict) -> dict:
    texts = [payload.get("description", "")] + list(payload.get("reviews", []))
    spans: list[str] = []
    for t in texts:
        spans.extend(s for s in detect_injection(t) if s not in spans)
    reviews = payload.get("reviews", [])
    pos = sum(any(w in r.lower() for w in _POSITIVE) for r in reviews)
    neg = sum(any(w in r.lower() for w in _NEGATIVE) for r in reviews)
    summary = "no reviews" if not reviews else (
        f"{len(reviews)} reviews, mostly positive" if pos >= neg else f"{len(reviews)} reviews, mixed"
    )  # fmt: skip
    attrs: dict[str, str | int | float | bool] = {}
    desc = payload.get("description", "").lower()
    if "waterproof" in desc:
        attrs["waterproof"] = True
    return {
        "title": str(payload.get("name", ""))[:120],
        "price_minor": payload.get("price_minor"),
        "attributes": attrs,
        "rating_summary": summary,
        "suspected_instruction": bool(spans),
        "flagged_spans": spans,
    }


def extract(backend: LLMBackend, payload: dict) -> LLMResult:
    """payload: {sku, name, price_minor, description, reviews}. Returns validated `Extracted` data.

    The model path always unions in the pattern detector, so `suspected_instruction` can only go up.
    """
    res = backend.complete_json("extractor", payload, Extracted, _offline)
    patterns = _offline(payload)
    if patterns["flagged_spans"] and not res.data.get("suspected_instruction"):
        res.data["suspected_instruction"] = True
        res.data["flagged_spans"] = sorted(
            set(res.data.get("flagged_spans", [])) | set(patterns["flagged_spans"])
        )
    return res
