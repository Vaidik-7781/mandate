python -m pip install -e ".[dev]"# Mandate

**Agents can pay. Mandate lets them prove they were allowed to.**

An open-source accountability layer for AI-agent purchases on PayPal. A person signs what their agent may buy;
every purchase is checked against it in plain code before money moves; and when a payment is disputed, the
evidence and the verdict come from published rules, not from a model's mood.

> Sandbox project. Not production-ready, not legal advice, not endorsed by PayPal. The PayPal binding of
> Verifiable Intent is a **proposal**.

## What is real and what is simulated (read this first)

| Part | Default (no keys) | With your keys |
|---|---|---|
| PayPal Orders, Payments, Disputes, Webhooks | **Simulated** in-process, built from PayPal's documentation | **Real PayPal sandbox**: same client code, `python -m mandate.sandbox_check` proves it end to end |
| Buyer approval | A button standing in for the PayPal popup | The PayPal approve page |
| Dispute outcome | **Scripted** (the sandbox adjudication is a test call; no real win rate exists) | Same |
| Language model | Offline rules or recorded answers | **Live model** if `LLM_API_KEY` is set (Anthropic or any OpenAI-compatible endpoint) |
| Guard, ledger, credentials, liability rules, receipts, audit log | **Real code**, identical in every mode | same |
| Mandate-Bench | **Synthetic**: scripted attackers, generated scenarios | `make bench-llm` runs it against a real model |

The app shows this table on screen (footer of both pages). Nothing above has been run against the real PayPal
sandbox *by the author*: no sandbox credentials were available. [docs/paypal-verification.md](docs/paypal-verification.md)
lists which PayPal facts were checked against documentation and which are untested.

## Why it exists

An agent with a payment method is a confused deputy: text on a web page can talk it into spending. Mandate's rule is
**the model proposes, code disposes**: money only moves through a gateway that demands a token the guard issues, and the
guard checks signed limits the model cannot edit.

| Job | How |
|---|---|
| Prove | The user signs a Verifiable Intent credential chain (Mastercard's open draft, vendored under Apache-2.0). The PayPal order's `custom_id` is the hash of the chain. Receipts are signed. |
| Protect | A guard checks the chain, every limit, the merchant, the cart, untrusted-input provenance and a budget/replay ledger before money moves. A quarantined extractor keeps web text away from the planner. |
| Resolve | A dispute agent applies a published rule set, assembles an evidence packet and decides refund, fight or human, with a money gate. If PayPal's own protection already applies, it defers to PayPal. |
| Attribute | Fault is split user / agent platform / merchant by deterministic rules (Liability Matrix v0.1). A model may explain it but cannot change it. |

## Run it (about a minute, no keys)

```bash
python scripts/quickstart.py serve    # installs what is missing, serves http://localhost:8000
python scripts/quickstart.py demo     # the same story in the terminal
python scripts/quickstart.py mcp      # MCP server on stdio, one demo mandate signed for you
```

Needs Python 3.12+. The UI ships prebuilt; Node is only needed to change it. `make` targets do the same
(`make check` runs lint, types, tests, both benches and the web build; `make e2e` is the Chromium test).
The buyer app starts from three situations (a runner, a caregiver whose parent shops through an assistant, an office
manager), each a plain prompt for the same intent compiler.

Docker: `docker build -t mandate . && docker run -p 8000:8000 mandate` (the CI workflow builds it on every push; I could not run Docker where this was built).
Real sandbox: set `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` (see `.env.example`);
live PayPal is refused at startup.

## The evidence, and its limits

**Mandate-Bench**: 360 synthetic scenarios, five configurations, each adding one defence. Attack success rate:

| Config | Adds | ASR | Hold-out ASR | False escalations on benign |
|---|---|---|---|---|
| C0 | nothing (gullible agent) | 100% | 100% | 0% |
| C1 | credential chain, constraints, registry | 49.0% | 44.6% | 0% |
| C2 | taint step-up | 31.0% | 33.8% | 1.4% |
| C3 | quarantined extractor, ask-don't-guess compiler | 17.2% | 25.7% | 5.7% |
| C4 | ledger, revocation, hardened gateway | 0% | 0% | 5.7% |

Read it with these in hand, because the table alone flatters the project:

* The attackers are scripted and the scenarios synthetic. C3 and C4 do not show model robustness: the deterministic
  planner never reads raw text. `make bench-llm` is the harness that puts a **real model** in the planner's seat, on
  two arms (raw page text vs quarantined); it needs your key and this repo ships **no** model numbers it did not measure.
* **Adaptive attacker** ([bench/results/adaptive.md](bench/results/adaptive.md)): an attacker who mutates payloads
  evades the pattern detector 100% of the time within a few queries, so the detector is a tripwire, not the boundary.
  What stops those payloads is the signed mandate: 24/24 out-of-mandate goals are still refused when the agent obeys.
  Steering *inside* the mandate (6/6) is not stopped; narrow mandates are the defence, and the report says so.
* The 4/70 benign escalations are deliberate trap texts and are explained in
  [bench/results/results.md](bench/results/results.md).
* **Liability Matrix weights are design choices**, not law. [spec/liability-sensitivity.md](spec/liability-sensitivity.md)
  shows the dispute decision is insensitive to the platform/merchant split and depends only on the user's share.

## For agent builders: MCP

`POST /mcp` and `python -m mandate.mcp` expose eight tools (search, spend within a signed mandate, capture, receipt,
statuses). There is deliberately no tool to create, widen or revoke a mandate. See [docs/mcp.md](docs/mcp.md).

## The website

Five server-rendered pages (buyer app, merchant console, About, Contact, Privacy) with unique titles and descriptions, one
`h1` each, canonical URLs, Open Graph cards, JSON-LD (Organization, WebSite, SoftwareApplication, breadcrumbs),
`robots.txt`, `sitemap.xml`, permanent redirects, a custom 404, WebP lazy-loaded images with alt text, a working contact
form (stored; optional email), a privacy page, and Google Analytics that loads only after consent and only if you set an
ID. Every item and the settings to flip before going public are in [docs/launch-checklist.md](docs/launch-checklist.md).

## Layout

`apps/api/mandate` (Python 3.12, FastAPI) · `apps/web` (Vite React: buyer app, merchant console with AG Grid
Community tables) · `spec/` (binding, matrix, sensitivity, JSON vectors) · `bench/` · `alembic/` · `scripts/` ·
`docs/` (incl. `openapi.json` and a Postman collection) · `tests/` (678).

## Status of each claim

| Verified here | Not verified here |
|---|---|
| ruff, mypy, pytest, Alembic, both benches, web build, Chromium test, quickstart from a clean extract, MCP over stdio and HTTP, all in replay mode | **Live PayPal sandbox** (script ready: `python -m mandate.sandbox_check`) |
| Webhook signature code against the simulator's signed events | **A live LLM** (harness and provider code tested with mock transports) |
| | Docker image build (no Docker daemon here; the CI workflow is set up to build it), Render deploy |
| | Several PayPal sandbox behaviours the simulator assumes (`spec/paypal-binding.md`) |

Out of scope on purpose: the PayPal JS SDK v6 button, vaulting, and a Render Workflows adapter are not built and are
not claimed. Sandbox mode sends the buyer to the PayPal approve page. Replay-mode simulator state is in memory (a
restart drops open simulated orders; the database and audit log persist).

Docs: [judge guide](docs/judge-guide.md) · [architecture](docs/architecture.md) · [demo script](docs/demo-script.md) ·
[Devpost text](docs/devpost-submission.md) · [MCP](docs/mcp.md) · [PayPal verification](docs/paypal-verification.md) ·
[design system](docs/design-system.md) · [launch checklist](docs/launch-checklist.md). License: Apache-2.0 (vendored Verifiable Intent code keeps its own notice).
