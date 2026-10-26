PY ?= .venv/bin/python
.PHONY: install web test lint type bench bench-adaptive bench-llm liability demo e2e check run package api-export sandbox-check mcp quickstart

install:            ## python deps (editable) + web deps
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && cd apps/web && npm install

web:                ## build the React apps into apps/web/dist
	cd apps/web && npm run build

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

type:
	$(PY) -m mypy apps/api/mandate

bench:              ## Mandate-Bench: writes bench/results/{latest.json,results.md}
	cd apps/api && ../../$(PY) -m mandate.bench.run

demo:               ## scripted end-to-end run in replay mode (no keys)
	$(PY) scripts/demo.py

run:                ## API + built UI on :8000
	cd apps/api && ../../.venv/bin/uvicorn mandate.main:app --port 8000

e2e: web            ## Chromium smoke test (needs playwright)
	python3 scripts/e2e_smoke.py

bench-adaptive:     ## adaptive attacker vs the injection detector, then the guard
	cd apps/api && ../../$(PY) -m mandate.bench.adaptive

bench-llm:          ## real-model bench (needs LLM_API_KEY; LABEL=claude|gpt|...)
	cd apps/api && ../../$(PY) -m mandate.bench.llm_bench --label $(or $(LABEL),model)

liability:          ## regenerate spec/liability-sensitivity.md
	cd apps/api && ../../$(PY) -m mandate.liability.sensitivity

api-export:         ## docs/openapi.json and the Postman collection
	$(PY) scripts/export_api.py

sandbox-check:      ## end-to-end check against PayPal sandbox (your keys) or the simulator
	$(PY) -m mandate.sandbox_check

mcp:                ## MCP server on stdio with one demo mandate
	cd apps/api && ../../$(PY) -m mandate.mcp --demo-mandate

quickstart:         ## install what is missing and serve on :8000
	python3 scripts/quickstart.py serve

check: lint type test bench bench-adaptive web   ## everything CI runs, minus the browser

package:
	./scripts/package.sh
