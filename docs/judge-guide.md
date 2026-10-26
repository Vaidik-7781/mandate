# Judge guide (five minutes)

**One sentence:** agents can pay; Mandate lets them prove they were allowed to.

## Try it

```bash
python scripts/quickstart.py serve        # http://localhost:8000, no keys, about a minute
```

Pick **Family caregiver**, turn it into limits, sign, press *Buy for me*, then *Simulate a hijacked agent*. The
merchant console (`/console.html`) has orders, guard decisions, disputes, the audit log and the benchmark.

## What is real, what is not

The app prints this on every page. In short: the guard, ledger, credentials, liability rules, receipts and audit
log are real code; PayPal is an in-process **simulator** unless you set sandbox keys; dispute outcomes are
**scripted**; the benchmark is **synthetic**; the language model is offline unless you set `LLM_API_KEY`.
Details and the per-claim documentation check: [paypal-verification.md](paypal-verification.md).

## Questions a skeptical judge asks

| Question | Answer and where to look |
|---|---|
| Does it actually call PayPal? | The client code (`gateway/paypal/`) targets Orders v2, Payments v2 refunds, Disputes v1 and webhooks. By default it runs against the simulator. `python -m mandate.sandbox_check` runs the whole flow against the real sandbox with your keys and writes `sandbox_report.json`. The author could not run it (no credentials). |
| Where is the AI? | Five model roles (intent compiler, quarantined extractor, planner, evidence note, liability explainer) behind one provider layer (Anthropic or any OpenAI-compatible endpoint). Money never depends on a model: the guard is plain code. Offline by default so it reproduces; a key makes it live. Tested with mock transports, not yet with a live key. |
| Is the safety number rigged? | Partly synthetic, and it says so. The attackers are scripted. Two additions answer that: an **adaptive attacker** that evades the detector 100% of the time yet cannot get an out-of-mandate purchase past the guard (and is *not* stopped inside the mandate; the report says so), and `make bench-llm`, which puts a real model in the planner seat on two arms. No model numbers are shipped that were not measured. |
| Are the liability weights invented? | Yes, they are design choices. [liability-sensitivity.md](../spec/liability-sensitivity.md) shows which ones can change what the dispute agent does (only the user's share) and which only change who pays. |
| Is the idea crowded? | Agent payment protocols exist. Mandate is the accountability layer under them: signed limits bound to the PayPal order, a guard, a dispute agent and a published fault rule. It does not replace any of them. |
| Can another agent use it? | MCP server (stdio and `POST /mcp`), eight tools, none able to create or widen a mandate: [mcp.md](mcp.md). |
| What if PayPal already protects the buyer? | The dispute agent defers to PayPal's process; there is a test and a simulator case for it. |
| What is not built? | PayPal JS SDK v6 button, vaulting, a Render Workflows adapter (sandbox mode sends the buyer to the PayPal approve page). Live-sandbox and live-LLM runs. Docker image build was not run locally (no daemon); CI is configured to build it. |
| How do I check the code? | `make check` (ruff, mypy, 650+ tests, both benches, web build); `make e2e` for the Chromium test; `docs/openapi.json` and a Postman collection for the API. |

## Prize fit, honestly

AG Grid Community tables power the console. Nothing else from the sponsor list is claimed. PayPal APIs are used
through the documented endpoints above.
