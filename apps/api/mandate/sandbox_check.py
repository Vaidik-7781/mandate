"""End-to-end check against PayPal's SANDBOX (or, with no credentials, the built-in simulator).

    python -m mandate.sandbox_check            # needs PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET from a sandbox REST app

It signs a mandate, lets the agent buy, waits for you to approve the order as a sandbox buyer, captures, checks that
PayPal stored our credential-chain digest in `custom_id`, verifies the signed receipt, refunds through revoke, and
checks the books and the audit chain. Each step prints PASS or FAIL and the result is written to
`sandbox_report.json`, which is the evidence to attach to a submission. It refuses to run against live PayPal.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .mcp import sign_demo_mandate
from .runtime import Runtime
from .settings import LIVE_BASE, get_settings


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    mode: str
    base_url: str
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "base_url": self.base_url, "ok": self.ok, "checked_at": int(time.time()),
                "steps": [s.__dict__ for s in self.steps]}  # fmt: skip


def run(rt: Runtime, say: Callable[[str], None] = print, wait_seconds: int = 600,
        sleep: Callable[[float], None] = time.sleep) -> Report:  # fmt: skip
    cfg = rt.settings
    rep = Report("replay (simulated PayPal)" if rt.replay else "paypal sandbox", cfg.effective_paypal_base)

    def step(name: str, ok: bool, detail: str = "") -> bool:
        rep.steps.append(Step(name, bool(ok), detail))
        say(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        return bool(ok)

    svc = rt.service
    if cfg.effective_paypal_base == LIVE_BASE:
        step("refuse live PayPal", False, "this project is sandbox-only")
        return rep
    try:
        rt.gateway.client._auth_header()  # noqa: SLF001 - OAuth round trip
        step("OAuth token", True, cfg.effective_paypal_base)
    except Exception as e:  # noqa: BLE001
        step("OAuth token", False, str(e)[:200])
        return rep

    m = sign_demo_mandate(rt, "runner")
    step("mandate signed", m["status"] == "active", m["id"])
    r = svc.agent_purchase(m["id"], "running shoes", 1)
    if not step("agent purchase created a PayPal order", r.get("outcome") == "created", r.get("outcome", "")):
        return rep
    order = r["order"]
    oid, pp_id = order["id"], order["paypal_order_id"]

    pp = rt.gateway.get_order(pp_id)
    custom = (pp.get("purchase_units") or [{}])[0].get("custom_id", "")
    step("PayPal stored the credential-chain digest as custom_id", custom == order["chain_digest"] and len(custom) == 43,
         f"{custom[:12]}…")  # fmt: skip

    if rt.replay:
        svc.simulate_buyer_approval(oid)
    else:
        say(f"\nOpen this link, sign in as a SANDBOX buyer account and approve:\n  {r.get('approve_url')}\n")
        deadline = time.time() + wait_seconds
        while time.time() < deadline and rt.gateway.get_order(pp_id).get("status") != "APPROVED":
            sleep(3)
    approved = rt.gateway.get_order(pp_id).get("status") == "APPROVED"
    if not step("buyer approved the order", approved):
        return rep

    cap = svc.capture(oid)
    step("capture", cap.get("outcome") == "captured", cap.get("outcome", ""))
    ov = svc.order_view(oid)
    step(
        "order is CAPTURED with a capture id",
        ov["status"] == "CAPTURED" and bool(ov["capture_id"]),
        ov["status"],
    )
    pp2 = rt.gateway.get_order(pp_id)
    cap_custom = ((pp2.get("purchase_units") or [{}])[0].get("payments", {}).get("captures") or [{}])[0].get(
        "custom_id"
    )
    step("capture echoes custom_id", cap_custom == order["chain_digest"], str(cap_custom)[:12])

    try:
        rec = svc.receipt(oid)
        step("signed receipt verifies", svc.verify_receipt(rec))
    except Exception as e:  # noqa: BLE001
        step("signed receipt verifies", False, str(e)[:200])

    rv = svc.revoke(m["id"])
    ov = svc.order_view(oid)
    step("revoke refunded the undelivered order", ov["status"] == "REFUNDED" and ov["refunded_minor"] == ov["amount_minor"],
         f"{ov['status']}, refunded {ov['refunded_minor']}")  # fmt: skip
    b = rt.ledger.balance(m["id"])
    step(
        "ledger exposure returned to zero",
        b is not None and b.exposure_minor == 0,
        f"{getattr(b, 'exposure_minor', '?')}",
    )
    v = rt.audit.verify()
    step("audit chain intact", v.ok, f"{v.count} entries")
    _ = rv
    return rep


def main(argv: list[str] | None = None) -> int:
    cfg = get_settings()
    if cfg.effective_paypal_base == LIVE_BASE:
        print("Refusing: this project is sandbox-only.")
        return 2
    if cfg.is_replay:
        print("No PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET: running against the built-in simulator.\n"
              "That proves the code path, not PayPal. Set sandbox credentials to check the real thing.\n")  # fmt: skip
    rep = run(Runtime.build(cfg))
    out = "sandbox_report.json"
    with open(out, "w") as f:
        json.dump(rep.to_dict(), f, indent=2)
    print(f"\n{'ALL PASSED' if rep.ok else 'FAILED'}; report written to {out}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
