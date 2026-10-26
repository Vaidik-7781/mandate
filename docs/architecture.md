# Architecture

```
 buyer app (React) ──┐                         ┌── Mandate-Bench (scripted attackers, recording executor)
 merchant console ───┤                         │
                     ▼                         │
              FastAPI (main.py) ── MandateService ── Guard ── Ledger (budget, occurrences, replay, velocity)
                     │                  │            │  └─ credential chain (Verifiable Intent, SD-JWT)
   /webhooks/paypal  │                  │            └──── Taint (USER / SIGNED / UNTRUSTED)
        Receiver ────┘                  ├── agents: extractor (quarantined) → planner (deterministic) · compiler · dispute
   (verify, dedupe)                     ├── Audit (hash-chained log) · Receipts (ES256-signed)
                                        └── PayPalGateway (guard tokens) ── PayPalClient ── PayPal sandbox
                                                                              └─ replay mode: in-process PayPalSim over HTTP
```

## Rules the code is built around

1. **The model proposes; code disposes.** Every money decision (allow, step up, deny, capture, refund,
   fight) is plain code in `guard/` and `agents/dispute.py::decide`. No model output reaches a payment call
   without passing the guard, and the gateway refuses calls without a guard token bound to the exact cart.
2. **Untrusted text never reaches the planner.** A quarantined extractor returns fixed fields only (price,
   attributes, a `suspected_instruction` flag, spans). The planner sees those fields, never raw catalog or
   buyer text. This is an architectural property, not a measured robustness of a model.
3. **Provenance labels.** Inputs to a decision are labelled USER, SIGNED or UNTRUSTED. A choice that cites an
   untrusted instruction, or deviates from the deterministic policy, steps up to a human.
4. **Amounts are integer minor units** everywhere; decimal strings appear only at the PayPal boundary.
5. **No gateway calls inside an open DB session.** Webhooks can arrive re-entrantly (the simulator delivers
   synchronously), so holding a transaction across a PayPal call would deadlock.
6. **Refunds are booked once per (order, tag)**, whichever of the API response or the webhook arrives first.
7. **Replay mode needs no keys.** `PayPalSim` runs in-process and is reached over HTTP through a Starlette
   `TestClient`, so the same `PayPalClient` code runs against it and against the real sandbox.

## Modules

| Path | Job |
|---|---|
| `credentials/` | key ring, VI chain issue and verify, constraint re-checks, checkout JWTs |
| `guard/` | the one place a purchase is judged (`guard.py`), taint (`taint.py`), gateway tokens (`token.py`) |
| `ledger/` | atomic budget/occurrence/replay/velocity reservations, revocation and pause |
| `agents/` | `extractor`, `planner` (+ scripted `GullibleAgent` for the bench), `compiler`, `dispute`, `evidence`, `llm` backends (offline, Anthropic, record/replay) |
| `liability/` | Liability Matrix v0.1 and the facts derived from an order |
| `gateway/paypal/` | REST client with retries and re-auth, webhook verification, guarded gateway |
| `receiver/` | webhook intake: verify, dedupe, hand to the task runner |
| `sim/paypal.py` | in-process PayPal simulator (orders, captures, refunds, disputes, signed webhooks) |
| `audit/` | hash-chained audit log; ES256-signed receipts and PDFs |
| `bench/` | Mandate-Bench harness, scorer, report |
| `workflows/` | `LocalRunner`; optional Render Workflows adapter (unverified) |

## Known limits

- The PayPal sandbox Orders flow was run once (26 Oct 2026, `docs/sandbox-report.json`); webhooks and the Disputes API were not. Not run against a live LLM, a Render deployment or AG Studio. Replay mode, the
  simulator, the offline backend and the test suite are what was exercised.
- The simulator implements PayPal behaviour from documentation; several details are guesses (see
  `spec/paypal-binding.md`).
- The PayPal JS SDK v6 button is not integrated. In sandbox mode the UI links to the order's `approve_url`;
  in replay mode a button simulates the buyer's approval.
- Sandbox dispute adjudication is scripted. Bench scenarios are synthetic.
- Replay-mode simulator state (orders, disputes) lives in memory. After a server restart the database and audit
  log persist but open simulator orders are gone; acting on one returns a clear 404 JSON error, not a crash.
- With no recorded LLM fixtures (`fixtures/llm` ships empty) replay mode falls back to the deterministic offline
  backend and labels results `source: "replay-miss"`. Set `LLM_BACKEND=anthropic` and `LLM_API_KEY` once to
  record fixtures; later runs replay them with no key.
- The bench uses scripted attackers. C3/C4 results show the defences hold against those scripts; they are not
  a measure of model robustness.
- Not production-ready, not legal advice, not endorsed by PayPal. Sandbox only: live PayPal is refused.

## Added in the judge-driven pass

| Module | Purpose |
|---|---|
| `mcp.py` | MCP server (stdio and `POST /mcp`); eight tools, none can mint or widen authority |
| `sandbox_check.py` | End-to-end check against the real PayPal sandbox (or the simulator); writes `sandbox_report.json` |
| `presets.py` | Persona presets and the "what is real" table served at `/api/config` |
| `bench/adaptive.py` | Adaptive attacker against the detector, then the guard under a fully obedient agent |
| `bench/llm_bench.py` | Real-model bench, raw-text arm vs quarantined arm; refuses to run without a key |
| `liability/sensitivity.py` | Which Liability Matrix numbers can change a dispute decision |
| `agents/llm.py` | Provider layer: Anthropic and OpenAI-compatible over httpx, record/replay, cost cap |
