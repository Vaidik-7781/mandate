"""Browser smoke test: starts the API (serving apps/web/dist), drives the buyer app and the console in Chromium.

    python scripts/e2e_smoke.py [--shots DIR]

Needs `pip install playwright`, Chromium (PLAYWRIGHT_BROWSERS_PATH), and `npm run build` in apps/web.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    shots = Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    if not (ROOT / "apps/web/dist/index.html").exists():
        print("run `npm run build` in apps/web first")
        return 2
    port = free_port()
    env = {
        **os.environ,
        "DATABASE_URL": "sqlite://",
        "REPLAY_MODE": "true",
        "PYTHONPATH": str(ROOT / "apps/api"),
    }
    venv_py = ROOT / ".venv" / "bin" / "python"
    server_py = str(venv_py) if venv_py.exists() else sys.executable
    srv = subprocess.Popen([server_py, "-m", "uvicorn", "mandate.main:app", "--port", str(port), "--log-level", "warning"],
                           cwd=ROOT, env=env)  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                if urllib.request.urlopen(f"{base}/health", timeout=1).status == 200:
                    break
            except OSError:
                time.sleep(0.25)
        else:
            print("server did not start")
            return 1
        with sync_playwright() as p:
            exe = os.environ.get("CHROMIUM_PATH")
            browser = (
                p.chromium.launch(executable_path=exe, args=["--no-sandbox"])
                if exe
                else p.chromium.launch(args=["--no-sandbox"])
            )
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            page.goto(base)
            expect(page.get_by_role("heading", name="What may your agent buy?")).to_be_visible()
            page.get_by_role("button", name="Turn into limits").click()
            page.get_by_role("button", name="Sign this mandate").click()
            expect(page.get_by_role("heading", name="Your mandate")).to_be_visible()
            page.get_by_role("button", name="Buy for me").click()
            expect(page.locator(".seal.allow").first).to_be_visible()
            page.get_by_role("button", name="Approve in simulated PayPal").click()
            page.get_by_role("button", name="Capture payment").click()
            page.get_by_role("button", name="Mark delivered").click()
            page.get_by_role("button", name="Signed receipt").click()
            expect(page.get_by_text("Signature valid.")).to_be_visible()
            page.get_by_role("button", name="Simulate a hijacked agent").click()
            expect(page.locator(".seal.deny").first).to_be_visible()
            page.get_by_role("button", name="Open a dispute").click()
            if shots:
                page.screenshot(path=str(shots / "buyer.png"), full_page=True)
            page.get_by_role("button", name="Revoke and refund anything undelivered").click()
            expect(page.get_by_text("You revoked the mandate")).to_be_visible()
            expect(page.get_by_role("button", name="Start a new mandate")).to_be_visible()
            page.reload()  # the mandate survives a reload within the tab
            expect(page.get_by_role("heading", name="Your mandate")).to_be_visible()

            # reality panel is honest on screen
            p2 = browser.new_page(viewport={"width": 1280, "height": 900})
            p2.goto(base)
            p2.get_by_text("What is real in this demo, and what is simulated").click()
            expect(p2.get_by_text("An in-process simulator")).to_be_visible()
            # a second persona, in a fresh tab (the mandate is remembered per tab)
            p2.get_by_role("button", name="Family caregiver").click()
            p2.get_by_role("button", name="Turn into limits").click()
            p2.get_by_role("button", name="Sign this mandate").click()
            expect(p2.get_by_role("heading", name="Your mandate")).to_be_visible()
            expect(p2.get_by_text("CareWell").first).to_be_visible()
            p2.get_by_role("button", name="Buy for me").click()
            expect(p2.locator(".seal.allow").first).to_be_visible()
            p2.get_by_role("button", name="Simulate a hijacked agent").click()
            expect(p2.locator(".seal.deny, .seal.step_up").first).to_be_visible()

            p2.close()
            page.goto(f"{base}/console.html")
            expect(page.get_by_text("Agent decision:").first).to_be_visible()
            if shots:
                page.screenshot(path=str(shots / "console-disputes.png"), full_page=True)
            page.get_by_role("tab", name="Disputes").focus()
            page.keyboard.press("ArrowRight")  # segmented control is keyboard operable
            expect(page.get_by_role("tab", name="Needs a human")).to_have_attribute("aria-selected", "true")
            page.get_by_role("tab", name="Orders").click()
            expect(page.get_by_role("grid").first).to_be_visible()
            expect(
                page.get_by_role("gridcell")
                .filter(has_text="CAPTURED")
                .or_(page.get_by_role("gridcell").filter(has_text="REFUNDED"))
                .first
            ).to_be_visible()
            page.get_by_role("tab", name="Guard decisions").click()
            expect(page.get_by_role("gridcell").filter(has_text="allow").first).to_be_visible()
            if shots:
                page.screenshot(path=str(shots / "console-decisions.png"), full_page=True)
            page.get_by_role("tab", name="Audit log").click()
            expect(page.get_by_role("gridcell").first).to_be_visible()
            page.get_by_role("button", name="Verify the whole chain").click()
            expect(page.get_by_text("entries verified")).to_be_visible()
            page.get_by_role("tab", name="Analytics").click()
            expect(page.get_by_text("Guard verdicts")).to_be_visible()
            page.get_by_role("tab", name="Mandate-Bench").click()
            expect(page.get_by_text("How often attacks succeed")).to_be_visible()
            if shots:
                page.screenshot(path=str(shots / "console-bench.png"), full_page=True)

            # ---- the rest of the site: pages, SEO basics, hydration, 404, contact form
            for path, h1 in [
                ("/about", "Agents can pay"),
                ("/contact", "Contact"),
                ("/privacy", "Privacy policy"),
                ("/console.html", "Disputes"),
            ]:
                page.goto(base + path)
                expect(page.locator("h1")).to_have_count(1)
                expect(page.locator("h1")).to_contain_text(h1)
                expect(page.locator('link[rel="canonical"]')).to_have_count(1)
                expect(page.get_by_role("navigation", name="Breadcrumb")).to_be_visible()
            page.goto(base + "/about")
            for im in page.locator("img").all():
                assert im.get_attribute("alt"), "image without alt text"
                expect(im).to_have_attribute("loading", "lazy")
            page.locator("img").first.scroll_into_view_if_needed()
            page.wait_for_function("[...document.images].every(i => i.complete && i.naturalWidth > 0)")
            seen = len(errors)
            r404 = page.goto(base + "/definitely-missing")
            assert r404 is not None and r404.status == 404
            expect(page.get_by_role("heading", name="That page does not exist")).to_be_visible()
            del errors[seen:]  # the browser logs the intentional 404 as a console error
            page.goto(base + "/about.html")
            assert page.url.endswith("/about"), page.url
            # analytics: banner is shown, nothing loads before consent, declining keeps it off
            ctx = browser.new_context()
            fresh = ctx.new_page()
            requested: list[str] = []
            fresh.on("request", lambda rq: requested.append(rq.url))
            fresh.goto(base)
            assert not any("googletagmanager" in u for u in requested), "analytics loaded before consent"
            ctx.close()
            # contact form: client validation message, then a real submission
            page.goto(base + "/contact")
            page.get_by_label("Your name").fill("Ada Lovelace")
            page.get_by_label("Email address").fill("ada@example.com")
            page.get_by_label("Message").fill("Hello, I would like to ask about mandates.")
            page.get_by_role("button", name="Send message").click()
            expect(page.get_by_role("status").filter(has_text="received").first).to_be_visible()
            for path in ("/", "/about", "/contact", "/privacy", "/console.html"):
                page.set_viewport_size({"width": 390, "height": 800})
                page.goto(base + path)
                assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth"), (
                    f"overflow on {path}"
                )
            page.set_viewport_size({"width": 390, "height": 800})
            page.goto(base)
            if shots:
                page.screenshot(path=str(shots / "buyer-mobile.png"), full_page=True)
                page.emulate_media(color_scheme="dark")
                page.set_viewport_size({"width": 1280, "height": 900})
                page.goto(f"{base}/console.html")
                page.get_by_role("tab", name="Mandate-Bench").click()
                expect(page.get_by_text("How often attacks succeed")).to_be_visible()
                page.screenshot(path=str(shots / "console-bench-dark.png"), full_page=True)
                page.emulate_media(color_scheme="light")
                page.set_viewport_size({"width": 390, "height": 800})
                page.goto(base)
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
            browser.close()
        if errors:
            print("browser errors:", *errors, sep="\n  ")
            return 1
        if overflow:
            print("horizontal scroll at 390px")
            return 1
        print("e2e smoke: OK")
        return 0
    finally:
        srv.terminate()
        srv.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
