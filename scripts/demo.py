"""End-to-end demo in replay mode: no API keys, no network. Prints one row per step.

python scripts/demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from mandate.db import Database  # noqa: E402
from mandate.main import create_app  # noqa: E402
from mandate.runtime import Runtime  # noqa: E402
from mandate.settings import Settings  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PROMPT = "Buy me black running shoes under $120, max 2 pairs, only from RunFast or TrailCo, valid for 7 days"
ROWS: list[tuple[str, str, str]] = []


def row(step: str, result: str, note: str = "") -> None:
    ROWS.append((step, result, note))


def main() -> int:
    cfg = Settings(database_url="sqlite://", replay_mode=True, secret_seed="demo-seed")
    api = TestClient(create_app(Runtime.build(cfg, db=Database("sqlite://"))))

    c = api.post("/api/compile", json={"prompt": PROMPT}).json()
    row("compile intent", "ready" if c["ready"] else "needs answers", f"{len(c['candidates'])} candidates")
    m = api.post("/api/mandates", json={"prompt": PROMPT, "draft": c["draft"], "confirmed": True,
                                        "selected_skus": [x["sku"] for x in c["candidates"]]}).json()  # fmt: skip
    row("sign mandate (L1+L2)", m["status"], m["id"])

    p = api.post(f"/api/mandates/{m['id']}/purchase", json={}).json()
    row(
        "agent purchase (planner ignores the injected text)",
        p["outcome"],
        f"chose {p['plan']['chosen_sku']}, untrusted cited={p['plan']['cites_untrusted']}",
    )
    oid = p["order_id"]
    api.post(f"/api/orders/{oid}/simulate-approval")
    cap = api.post(f"/api/orders/{oid}/capture").json()
    row("capture", cap["outcome"], f"order {oid}")
    api.post(f"/api/orders/{oid}/deliver")
    rc = api.get(f"/api/orders/{oid}/receipt").json()
    row("receipt verifies", str(api.post("/api/receipts/verify", json=rc).json()["valid"]))

    hj = api.post(f"/api/mandates/{m['id']}/purchase", json={"hijacked": True}).json()
    row(
        "hijacked agent obeys the injection (5 units)",
        hj["outcome"],
        "; ".join(hj["decision"]["reasons"])[:80],
    )

    d = api.post(f"/api/orders/{oid}/sim-dispute", json={"message": "Item never arrived."}).json()
    h = api.get(f"/api/disputes/{d['dispute_id']}").json()
    row(
        "dispute: agent decision (webhook-triggered)",
        str(h["decision"]),
        f"rule {h['rule_id']}, fault split {h['fault_split']}",
    )

    api.post(f"/api/mandates/{m['id']}/revoke")
    after = api.post(f"/api/mandates/{m['id']}/purchase", json={}).json()
    row("purchase after revoke", after["outcome"], ",".join(after["decision"]["codes"]))
    row("audit chain intact", str(api.get("/api/audit/verify").json()["ok"]))

    w = max(len(r[0]) for r in ROWS)
    print(f"{'step'.ljust(w)} | result       | note")
    print("-" * (w + 40))
    for s, r, n in ROWS:
        print(f"{s.ljust(w)} | {r.ljust(12)} | {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
