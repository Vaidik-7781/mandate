# Fixtures

`llm/` holds recorded LLM answers (one JSON per prompt hash); `paypal/` is reserved for recorded PayPal responses.
Both ship empty. In replay mode a missing LLM fixture falls back to the offline backend (`source: "replay-miss"`).
Record once: `LLM_BACKEND=anthropic LLM_API_KEY=... python scripts/demo.py   # replay mode is the default without PayPal keys`, then commit `llm/`.
