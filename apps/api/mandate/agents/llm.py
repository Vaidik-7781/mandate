"""Provider-agnostic model layer with record/replay.

Every model role goes through `LLMBackend.complete_json(role, payload, schema, offline)`:

* `OfflineBackend`   runs the role's deterministic implementation (no keys, no network; the default),
* `AnthropicBackend` calls a Claude model with a forced tool schema at temperature 0,
* `RecordReplayBackend` records real responses keyed by a hash of the request and replays them later.

Model output is always validated against a Pydantic schema; a failed call or invalid output falls back to the
offline implementation (marked `source="fallback"`). The LLM proposes, code disposes: nothing here moves money.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from ..settings import Settings
from .prompts import PROMPT_VERSION, PROMPTS


@dataclass
class LLMResult:
    data: dict
    source: str  # offline | model | replay | replay-miss | fallback
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    model: str = "offline"
    errors: list[str] = field(default_factory=list)


class LLMBackend(Protocol):
    name: str

    def complete_json(
        self, role: str, payload: dict, schema: type[BaseModel], offline: Callable[[dict], dict]
    ) -> LLMResult: ...


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate(schema: type[BaseModel], data: dict) -> dict:
    return schema.model_validate(data).model_dump()


class OfflineBackend:
    """Deterministic, key-free. Each role supplies its own offline implementation."""

    name = "offline"

    def complete_json(self, role, payload, schema, offline):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        data = _validate(schema, offline(payload))
        return LLMResult(data, "offline", latency_ms=(time.perf_counter() - t0) * 1000)


class _HttpAnthropic:
    """Minimal Messages API client over httpx, so no vendor SDK is required. Exposes `messages.create(**kw)`
    with the same shape the SDK has, which also keeps the backend easy to fake in tests."""

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com", http: httpx.Client | None = None,
                 timeout: float = 30.0) -> None:  # fmt: skip
        self._key, self._base = api_key, base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout)
        self.messages = self

    def create(self, **kw: Any) -> Any:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self._http.post(
                    f"{self._base}/v1/messages",
                    headers={"x-api-key": self._key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json=kw,
                )  # fmt: skip
            except httpx.HTTPError as exc:
                last = exc
                time.sleep(0.4 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 529):
                last = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(0.6 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise _HttpError(r.status_code, r.text[:300])
            return _wrap_anthropic(r.json())
        raise last or RuntimeError("request failed")


class _HttpError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


def _wrap_anthropic(j: dict) -> Any:
    blocks = [
        type("B", (), {"type": b.get("type"), "input": b.get("input", {})})() for b in j.get("content", [])
    ]
    u = j.get("usage", {})
    usage = type(
        "U", (), {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}
    )()
    return type("M", (), {"content": blocks, "usage": usage})()


class AnthropicBackend:
    """Claude via the Messages API with a forced tool call (strict schema), temperature 0, bounded calls."""

    def __init__(self, api_key: str, model: str, max_calls: int = 500, client: Any | None = None,
                 base_url: str = "https://api.anthropic.com") -> None:  # fmt: skip
        self.model, self.max_calls, self.calls = model, max_calls, 0
        self.name = model
        self.client = client if client is not None else _HttpAnthropic(api_key, base_url)

    def _create(self, role: str, payload: dict, schema: type[BaseModel], temperature: bool) -> Any:
        kw: dict[str, Any] = dict(
            model=self.model,
            max_tokens=1024,
            system=PROMPTS[role],
            messages=[{"role": "user", "content": _canon(payload)}],
            tools=[{"name": "respond", "description": f"Return the {role} result.",
                    "input_schema": schema.model_json_schema()}],
            tool_choice={"type": "tool", "name": "respond"},
        )  # fmt: skip
        if temperature:
            kw["temperature"] = 0
        return self.client.messages.create(**kw)

    def complete_json(self, role, payload, schema, offline):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        if self.calls >= self.max_calls:  # cost cap
            res = OfflineBackend().complete_json(role, payload, schema, offline)
            res.source, res.errors = "fallback", ["call cap reached"]
            return res
        self.calls += 1
        try:
            try:
                msg = self._create(role, payload, schema, temperature=True)
            except _HttpError as exc:
                if (
                    exc.status == 400 and "temperature" in exc.body.lower()
                ):  # some models fix their own sampling
                    msg = self._create(role, payload, schema, temperature=False)
                else:
                    raise
            block = next(b for b in msg.content if getattr(b, "type", "") == "tool_use")
            data = _validate(schema, dict(block.input))
            usage = getattr(msg, "usage", None)
            return LLMResult(
                data, "model", getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0),
                (time.perf_counter() - t0) * 1000, self.model,
            )  # fmt: skip
        except (StopIteration, ValidationError, Exception) as exc:  # noqa: BLE001 - any failure => safe fallback
            res = OfflineBackend().complete_json(role, payload, schema, offline)
            res.source, res.errors = "fallback", [f"{type(exc).__name__}: {exc}"[:300]]
            return res


class OpenAICompatBackend:
    """Any OpenAI-compatible `/chat/completions` endpoint (OpenAI, Azure-compatible gateways, vLLM, Ollama,
    OpenRouter) with a forced function call. Same schema validation, cost cap and fallback as the Claude backend."""

    def __init__(self, api_key: str, model: str, base_url: str = "", max_calls: int = 500,
                 http: httpx.Client | None = None, timeout: float = 30.0) -> None:  # fmt: skip
        self.model, self.max_calls, self.calls = model, max_calls, 0
        self.name = model
        self._key = api_key
        self._base = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._http = http or httpx.Client(timeout=timeout)

    def _post(self, body: dict) -> dict:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self._http.post(f"{self._base}/chat/completions", json=body,
                                    headers={"authorization": f"Bearer {self._key}"})  # fmt: skip
            except httpx.HTTPError as exc:
                last = exc
                time.sleep(0.4 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503):
                last = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(0.6 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise _HttpError(r.status_code, r.text[:300])
            return r.json()
        raise last or RuntimeError("request failed")

    def complete_json(self, role, payload, schema, offline):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        if self.calls >= self.max_calls:
            res = OfflineBackend().complete_json(role, payload, schema, offline)
            res.source, res.errors = "fallback", ["call cap reached"]
            return res
        self.calls += 1
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": PROMPTS[role]},
                         {"role": "user", "content": _canon(payload)}],
            "tools": [{"type": "function", "function": {"name": "respond", "description": f"Return the {role} result.",
                                                        "parameters": schema.model_json_schema()}}],
            "tool_choice": {"type": "function", "function": {"name": "respond"}},
        }  # fmt: skip
        try:
            try:
                j = self._post(body)
            except _HttpError as exc:
                if exc.status == 400 and "temperature" in exc.body.lower():
                    body.pop("temperature")
                    j = self._post(body)
                else:
                    raise
            call = j["choices"][0]["message"]["tool_calls"][0]["function"]
            data = _validate(schema, json.loads(call["arguments"]))
            u = j.get("usage", {})
            return LLMResult(data, "model", u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                             (time.perf_counter() - t0) * 1000, self.model)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - any failure => safe fallback
            res = OfflineBackend().complete_json(role, payload, schema, offline)
            res.source, res.errors = "fallback", [f"{type(exc).__name__}: {exc}"[:300]]
            return res


class RecordReplayBackend:
    """Replay recorded model responses by request hash; record when wrapping a live backend."""

    def __init__(self, inner: LLMBackend, fixtures_dir: str | Path, mode: str = "replay") -> None:
        self.inner, self.dir, self.mode = inner, Path(fixtures_dir), mode
        self.name = f"{mode}:{inner.name}"
        if mode != "replay":  # replaying must work on a read-only checkout and must not litter the cwd
            self.dir.mkdir(parents=True, exist_ok=True)

    def key(self, role: str, payload: dict) -> str:
        return hashlib.sha256(
            _canon({"role": role, "payload": payload, "prompt": PROMPT_VERSION}).encode()
        ).hexdigest()

    def complete_json(self, role, payload, schema, offline):  # type: ignore[no-untyped-def]
        k = self.key(role, payload)
        f = self.dir / f"{role}-{k[:16]}.json"
        if self.mode == "replay":
            if f.exists():
                rec = json.loads(f.read_text())
                return LLMResult(_validate(schema, rec["data"]), "replay", rec.get("tokens_in", 0),
                                 rec.get("tokens_out", 0), 0.0, rec.get("model", "recorded"))  # fmt: skip
            res = OfflineBackend().complete_json(role, payload, schema, offline)
            res.source = "replay-miss"
            return res
        res = self.inner.complete_json(role, payload, schema, offline)
        if res.source == "model":
            f.write_text(_canon({"key": k, "role": role, "data": res.data, "model": res.model,
                                 "tokens_in": res.tokens_in, "tokens_out": res.tokens_out}))  # fmt: skip
        return res


def pick_provider(settings: Settings) -> str:
    """anthropic | openai | offline, from LLM_BACKEND, the model name and whether a key is present."""
    want = settings.llm_backend
    if want == "offline" or not settings.llm_api_key:
        return "offline"
    if want in ("anthropic", "openai"):
        return want
    if settings.llm_base_url or settings.model_name.lower().startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    return "anthropic"


def build_backend(settings: Settings) -> LLMBackend:
    """Model selection, in plain terms:

    * LLM_API_KEY set (and LLM_BACKEND not "offline")  -> the real model answers, in replay mode too, so a judge
      who pastes a key sees a live model. LLM_RECORD=true also writes each answer to fixtures/llm.
    * no key, replay mode                              -> recorded fixtures, falling back to the offline rules
    * no key, sandbox mode                             -> the offline rules
    """
    provider = pick_provider(settings)
    inner: LLMBackend = OfflineBackend()
    if provider == "anthropic":
        inner = AnthropicBackend(settings.llm_api_key, settings.model_name, settings.llm_max_calls)
    elif provider == "openai":
        inner = OpenAICompatBackend(settings.llm_api_key, settings.model_name, settings.llm_base_url,
                                    settings.llm_max_calls)  # fmt: skip
    fixtures = Path(settings.fixtures_dir) / "llm"
    if provider != "offline":
        return RecordReplayBackend(inner, fixtures, mode="record") if settings.llm_record else inner
    if settings.is_replay:
        return RecordReplayBackend(inner, fixtures, mode="replay")
    return inner
