# Devpost text (paste into the form)

**Tagline:** Agents can pay. Mandate lets them prove they were allowed to.

## Inspiration
An AI agent with a payment method is a confused deputy: a line of text in a product review can talk it into spending
someone's money. When that payment is disputed, nobody can show what the person actually allowed, or who is at fault.

## What it does
You sign what your agent may buy (items, merchants, per-purchase and total limits, quantity, expiry). Every purchase
is checked in plain code against that signed mandate before money moves, and the PayPal order carries the hash of
the credential chain so the payment is provably bound to the permission. If the payment is disputed, a dispute
agent applies a published rule set (Liability Matrix v0.1), builds an evidence packet, and decides refund, defend
or human, with a money gate. Receipts are signed and every decision is in a hash-chained audit log.
Other agents can use it over MCP, with no tool that can create or widen a mandate.

## How we built it
Python 3.12 and FastAPI; SQLAlchemy and Alembic; a vendored copy of Mastercard's open Verifiable Intent
credential library (Apache-2.0) with a proposed PayPal binding; React and Vite with AG Grid Community for the
console; PayPal Orders v2, Payments v2, Disputes v1 and webhooks through one client that talks to either an
in-process simulator or the real sandbox. Model roles (compiler, quarantined extractor, planner, evidence note,
explainer) sit behind a provider layer for Anthropic or any OpenAI-compatible model. The rule is: the model proposes,
code disposes.

## Challenges
Making the safety claim honest. Our benchmark started with scripted attackers, which flatters any defence, so we
added an adaptive attacker (it beats the pattern detector every time; the signed limits still stop out-of-mandate
purchases; steering inside a mandate is the residual risk) and a harness that runs a real model. We also checked the
dispute evidence types against PayPal's documentation and found we were sending one that does not exist.

## Accomplishments
A complete prove, protect, resolve, attribute loop; 650+ tests; a published, replayable liability rule set with a
sensitivity analysis that shows which of its numbers matter; a console and buyer app that state on screen what is
simulated.

## What we learned
The valuable boundary is not detecting bad text; it is making the permission machine-checkable and bound to the
payment. Narrow mandates make most injections irrelevant.

## What's next
Run webhooks and disputes against the real sandbox; measure two model families with `make bench-llm`;
take the liability weights and the PayPal binding to people who can challenge them.

## Honest limits (also in the app)
PayPal is simulated by default (the Orders flow was also run once on the real sandbox); dispute outcomes are scripted; the benchmark is synthetic; no webhook, dispute or
live-model run has been performed by the authors. Sandbox project, not production-ready, not legal advice, not
endorsed by PayPal.

## Built with
python, fastapi, sqlalchemy, alembic, pydantic, react, vite, typescript, ag-grid, paypal-orders-api, paypal-disputes-api,
paypal-webhooks, verifiable-intent, model-context-protocol, sqlite, postgresql

## Links to fill in
Repository (public, license visible) · Demo video (public YouTube, your voice) · Live demo (if you deploy)
