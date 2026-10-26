"""Intent compiler: plain English -> enforceable constraints, asking instead of guessing.

Enforceable: merchants, SKUs, per-purchase price, total budget, quantity, expiry. Descriptive (informational only,
as in the Verifiable Intent spec): colour, size, weight, waterproof. A validator rejects empty allowlists,
unbounded budgets and unknown constraint types, and the user must confirm the exact fields and pick concrete SKUs
(an empty acceptable-items list would mean "any item").
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..credentials.chain import IntentSpec
from ..money import from_paypal
from .llm import LLMBackend, LLMResult

VAGUE_PRICE_WORDS = (
    "cheap",
    "affordable",
    "inexpensive",
    "budget-friendly",
    "nice",
    "good",
    "decent",
    "reasonable",
    "best",
)
NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
KNOWN_FIELDS = {"max_per_txn", "budget", "max_quantity", "merchants", "valid_seconds", "category"}


class Question(BaseModel):
    field: str
    question: str
    options: list[str] = Field(default_factory=list)


class CompiledDraft(BaseModel):
    max_per_txn_minor: int | None = None
    budget_minor: int | None = None
    currency: str = "USD"
    max_quantity: int | None = None
    valid_seconds: int | None = None
    category: str | None = None
    merchant_ids: list[str] = Field(default_factory=list)
    descriptive: dict[str, str | int | float | bool] = Field(default_factory=dict)
    questions: list[Question] = Field(default_factory=list)


@dataclass
class CompiledIntent:
    draft: CompiledDraft
    prompt: str
    candidates: list[dict] = field(default_factory=list)  # eligible catalog items the user can pick from
    ambiguity_flagged: bool = False
    source: str = "offline"

    @property
    def ready(self) -> bool:
        return not self.draft.questions

    def to_dict(self) -> dict:
        return {
            "draft": self.draft.model_dump(),
            "prompt": self.prompt,
            "candidates": self.candidates,
            "ambiguity_flagged": self.ambiguity_flagged,
            "ready": self.ready,
            "source": self.source,
        }


def _money(text: str) -> int | None:
    try:
        return from_paypal(text.replace(",", ""), "USD")
    except Exception:
        return None


def _parse_validity(text: str, now: int) -> int | None:
    t = text.lower()
    m = re.search(r"for\s+(\d+)\s+(day|week)s?", t)
    if m:
        return int(m.group(1)) * (86400 if m.group(2) == "day" else 7 * 86400)
    if "today" in t or "tonight" in t:
        return 86400
    if "this week" in t:
        return 7 * 86400
    for i, wd in enumerate(WEEKDAYS):
        if re.search(rf"\bby\s+{wd}\b", t):
            today = dt.datetime.fromtimestamp(now, dt.UTC)
            delta = (i - today.weekday()) % 7 or 7
            end = (today + dt.timedelta(days=delta)).replace(hour=23, minute=59, second=59, microsecond=0)
            return max(900, min(int((end - today).total_seconds()), 30 * 86400))
    return None


def _offline(payload: dict) -> dict:
    text: str = payload["prompt"]
    t = text.lower()
    now: int = payload["now"]
    answers: dict = payload.get("answers") or {}
    merchants: list[dict] = payload["merchants"]
    categories: list[str] = payload["categories"]
    questions: list[dict] = []
    d: dict = {"currency": "USD", "descriptive": {}, "merchant_ids": []}

    # price (per purchase)
    if "max_per_txn" in answers:
        d["max_per_txn_minor"] = _money(str(answers["max_per_txn"]).lstrip("$"))
    else:
        m = re.search(
            r"(?:under|below|less than|max(?:imum)?(?: of)?|at most|up to|no more than|<)\s*\$?\s*(\d[\d,]*(?:\.\d{1,2})?)",
            t,
        )
        if m:
            d["max_per_txn_minor"] = _money(m.group(1))
        elif any(w in t for w in VAGUE_PRICE_WORDS) or not re.search(r"\$\s*\d", t):
            questions.append({"field": "max_per_txn", "question": "What is the most you want to pay per purchase (for example $120)?",
                              "options": []})  # fmt: skip

    # quantity
    if "max_quantity" in answers:
        d["max_quantity"] = int(answers["max_quantity"])
    else:
        m = re.search(r"\b(a|an|one|two|three|four|five|\d+)\s+(?:pairs?|items?|units?|pieces?|of)\b", t)
        if m:
            tok = m.group(1)
            d["max_quantity"] = NUM_WORDS.get(tok, int(tok) if tok.isdigit() else 1)
        else:
            questions.append(
                {
                    "field": "max_quantity",
                    "question": "How many may the agent buy at most?",
                    "options": ["1", "2", "3"],
                }
            )

    # total budget
    m = re.search(r"(?:budget(?: of)?|total(?: of)?)\s*\$?\s*(\d[\d,]*(?:\.\d{1,2})?)", t) or re.search(
        r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(?:total|in total)", t
    )
    if "budget" in answers:
        d["budget_minor"] = _money(str(answers["budget"]).lstrip("$"))
    elif m:
        d["budget_minor"] = _money(m.group(1))
    elif d.get("max_per_txn_minor") and d.get("max_quantity"):
        d["budget_minor"] = d["max_per_txn_minor"] * d["max_quantity"]

    # merchants
    if "merchants" in answers:
        d["merchant_ids"] = list(answers["merchants"])
    else:
        first = [mm["name"].lower().split()[0] for mm in merchants]
        words = set(re.findall(r"[a-z0-9]+", t))
        named = [
            mm["id"]
            for mm in merchants
            if mm["name"].lower() in t
            or (first.count(mm["name"].lower().split()[0]) == 1 and mm["name"].lower().split()[0] in words)
        ]  # a shared first word ("RunFast" store vs outlet) is ambiguous, so it triggers a question
        if named:
            d["merchant_ids"] = named
        else:
            questions.append({"field": "merchants", "question": "Which stores may the agent buy from?",
                              "options": [f"{mm['id']}|{mm['name']} ({mm['website']})" for mm in merchants]})  # fmt: skip

    # authorisation duration
    if "valid_seconds" in answers:
        d["valid_seconds"] = int(answers["valid_seconds"])
    else:
        v = _parse_validity(text, now)
        if v:
            d["valid_seconds"] = v
        else:
            questions.append({"field": "valid_seconds", "question": "How long should this authorisation last?",
                              "options": ["86400|1 day", f"{7 * 86400}|7 days", f"{30 * 86400}|30 days"]})  # fmt: skip

    # category
    if "category" in answers:
        d["category"] = answers["category"]
    else:
        hit = next((c for c in categories if c in t or c.rstrip("s") in t), None)
        if hit:
            d["category"] = hit
        else:
            questions.append(
                {"field": "category", "question": "What kind of product is it?", "options": categories}
            )

    # descriptive (informational) constraints
    desc = d["descriptive"]
    if "waterproof" in t:
        desc["waterproof"] = True
    m = re.search(r"(?:under|below|less than|lighter than)\s*(\d+)\s*(?:g|grams)\b", t)
    if m:
        desc["max_weight_g"] = int(m.group(1))
    m = re.search(r"\bsize\s*(\d+(?:\.\d)?)", t)
    if m:
        desc["size"] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
    for color in ("black", "white", "blue", "green", "red", "grey", "gray"):
        if re.search(rf"\b{color}\b", t):
            desc["color"] = color
    d["questions"] = questions
    return d


def compile_intent(
    backend: LLMBackend,
    prompt: str,
    *,
    now: int,
    merchants: list[dict],
    categories: list[str],
    catalog: list[dict],
    answers: dict | None = None,
) -> tuple[CompiledIntent, LLMResult]:
    payload = {
        "prompt": prompt,
        "now": now,
        "answers": answers or {},
        "merchants": merchants,
        "categories": categories,
    }
    res = backend.complete_json("intent_compiler", payload, CompiledDraft, _offline)
    draft = CompiledDraft.model_validate(res.data)
    draft = _enforce(draft, answers or {})
    cands: list[dict] = []
    if draft.category and draft.max_per_txn_minor:
        for p in catalog:
            if p["category"] != draft.category or p["price_minor"] > draft.max_per_txn_minor:
                continue
            if draft.merchant_ids and p["merchant_id"] not in draft.merchant_ids:
                continue
            cands.append({k: p[k] for k in ("sku", "name", "merchant_id", "price_minor", "attributes")})
    return CompiledIntent(draft, prompt, cands, ambiguity_flagged=bool(res.data.get("questions")) or bool(draft.questions),
                          source=res.source), res  # fmt: skip


def _enforce(d: CompiledDraft, answers: dict) -> CompiledDraft:
    """Code-side guard on any model output: never let a missing field through as a guess."""
    asked = {q.field for q in d.questions}

    def need(fld: str, cond: bool, text: str, options: list[str] | None = None) -> None:
        if cond and fld not in asked and fld not in answers:
            d.questions.append(Question(field=fld, question=text, options=options or []))
            asked.add(fld)

    need(
        "max_per_txn",
        not d.max_per_txn_minor or d.max_per_txn_minor <= 0,
        "What is the most you want to pay per purchase?",
    )
    need(
        "max_quantity",
        not d.max_quantity or d.max_quantity < 1,
        "How many may the agent buy at most?",
        ["1", "2", "3"],
    )
    need("merchants", not d.merchant_ids, "Which stores may the agent buy from?")
    need("valid_seconds", not d.valid_seconds, "How long should this authorisation last?")
    need("category", not d.category, "What kind of product is it?")
    if d.max_per_txn_minor and d.max_quantity and not d.budget_minor:
        d.budget_minor = d.max_per_txn_minor * d.max_quantity
    # drop questions that the user has since answered
    d.questions = [q for q in d.questions if q.field not in answers]
    return d


def to_spec(
    user_id: str, prompt: str, draft: CompiledDraft, selected: list[dict], merchants: list[dict]
) -> IntentSpec:
    """Build the IntentSpec the user is about to sign. `selected` = [{id, title}] explicit SKUs."""
    by_id = {m["id"]: m for m in merchants}
    spec = IntentSpec(
        user_id=user_id,
        prompt_summary=prompt[:200],
        merchants=[by_id[i] for i in draft.merchant_ids if i in by_id],
        items=selected,
        max_per_txn_minor=draft.max_per_txn_minor or 0,
        budget_minor=draft.budget_minor or 0,
        currency=draft.currency,
        max_quantity=draft.max_quantity or 1,
        valid_seconds=draft.valid_seconds or 0,
        descriptive=dict(draft.descriptive),
    )
    problems = spec.validate()
    if problems:
        raise ValueError("; ".join(problems))
    return spec
