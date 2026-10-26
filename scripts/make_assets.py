"""Regenerate the site's images: Open Graph card (PNG), touch icon (PNG) and the two screenshots (WebP).

    python scripts/make_assets.py        # then `make web` to copy them into the build

Needs Playwright with Chromium, Pillow, and a built UI. Screenshots come from the real running app in replay mode.
"""

from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "apps" / "web" / "public"
DIST = ROOT / "apps" / "web" / "dist"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def webp(png: bytes, dest: Path, width: int, height: int) -> None:
    im = Image.open(io.BytesIO(png)).convert("RGB").crop((0, 0, width, height))
    im.save(dest, "WEBP", quality=84, method=6)
    print(f"{dest.relative_to(ROOT)}  {dest.stat().st_size // 1024} KB  {width}x{height}")


OG_HTML = """<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/assets/{css}"></head>
<body style="margin:0;width:1200px;height:630px;background:#0d1526;color:#eef2f9;display:grid;align-content:center;padding:0 90px;box-sizing:border-box;position:relative;overflow:hidden">
<svg viewBox="0 0 560 112" style="position:absolute;right:-40px;top:-10px;width:900px;overflow:visible;opacity:.5;color:#9daeff" aria-hidden="true">{rosette}</svg>
<div style="display:flex;align-items:center;gap:18px;font-family:var(--font-display);font-size:54px;letter-spacing:-.01em">
<svg width="64" height="64" viewBox="0 0 28 28" fill="none"><circle cx="14" cy="14" r="12.5" stroke="#9daeff" stroke-width="1.4"/><path d="M9.4 14.4l3.2 3.2 6-6.9" stroke="#9daeff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>Mandate</div>
<h1 style="font-family:var(--font-display);font-weight:400;font-size:92px;line-height:1.02;letter-spacing:-.02em;margin:34px 0 0;max-width:980px">Agents can pay. Mandate lets them prove they were allowed to.</h1>
<p style="font:500 28px/1.4 var(--font-ui);color:#a0abc0;margin:34px 0 0">Signed limits, a guard in plain code, and evidence for disputes. Open source, PayPal sandbox.</p>
</body></html>"""


def rosette() -> str:
    import math

    out = []
    R, r = 64, 23
    for k in range(7):
        d = 18 + k * 7
        pts = []
        for i in range(421):
            t = (i / 420) * math.pi * 2 * 23
            x = (R - r) * math.cos(t) + d * math.cos(((R - r) / r) * t)
            y = (R - r) * math.sin(t) - d * math.sin(((R - r) / r) * t)
            pts.append(f"{'M' if i == 0 else 'L'}{x * 0.85 + 430:.1f} {y * 0.85 + 56:.1f}")
        out.append(
            f'<path d="{"".join(pts)}" fill="none" stroke="currentColor" stroke-width="0.45" opacity="{0.35 + (k % 3) * 0.15:.2f}"/>'
        )
    return "".join(out)


def main() -> int:
    if not (DIST / "index.html").exists():
        print("build the UI first: make web")
        return 2
    css = next((DIST / "assets").glob("common-*.css")).name
    port = free_port()
    env = {
        **os.environ,
        "DATABASE_URL": "sqlite://",
        "REPLAY_MODE": "true",
        "PYTHONPATH": str(ROOT / "apps/api"),
    }
    venv_py = ROOT / ".venv" / "bin" / "python"
    srv = subprocess.Popen([str(venv_py) if venv_py.exists() else sys.executable, "-m", "uvicorn", "mandate.main:app",
                            "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env)  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                if urllib.request.urlopen(f"{base}/health", timeout=1).status == 200:
                    break
            except OSError:
                time.sleep(0.25)
        with sync_playwright() as p:
            exe = os.environ.get("CHROMIUM_PATH")
            br = (
                p.chromium.launch(executable_path=exe, args=["--no-sandbox"])
                if exe
                else p.chromium.launch(args=["--no-sandbox"])
            )
            page = br.new_page(viewport={"width": 1200, "height": 900}, color_scheme="light")
            page.goto(base)
            page.get_by_role("button", name="Turn into limits").click()
            page.get_by_role("button", name="Sign this mandate").click()
            expect(page.get_by_role("heading", name="Your mandate")).to_be_visible()
            page.get_by_role("button", name="Buy for me").click()
            expect(page.locator(".seal.allow").first).to_be_visible()
            page.get_by_role("button", name="Approve in simulated PayPal").click()
            page.get_by_role("button", name="Capture payment").click()
            page.get_by_role("button", name="Signed receipt").click()
            expect(page.get_by_text("Signature valid.")).to_be_visible()
            page.get_by_role("button", name="Simulate a hijacked agent").click()
            expect(page.locator(".seal.deny").first).to_be_visible()
            time.sleep(1.2)
            page.evaluate("window.scrollTo(0, 190)")
            time.sleep(0.4)
            (PUBLIC / "img").mkdir(parents=True, exist_ok=True)
            webp(page.screenshot(), PUBLIC / "img" / "buyer.webp", 1200, 760)

            page.goto(f"{base}/console.html")
            page.get_by_role("tab", name="Guard decisions").click()
            expect(page.get_by_role("gridcell").filter(has_text="allow").first).to_be_visible()
            time.sleep(0.8)
            page.evaluate("window.scrollTo(0, 120)")
            time.sleep(0.4)
            webp(page.screenshot(), PUBLIC / "img" / "console.webp", 1200, 760)

            og = br.new_page(viewport={"width": 1200, "height": 630})
            og.route("**/*", lambda route: route.continue_())
            og.goto(f"{base}/404-og-blank-for-origin")  # any same-origin URL so /assets/... resolves
            og.set_content(OG_HTML.format(css=css, rosette=rosette()))
            og.evaluate("document.fonts.ready")
            time.sleep(0.8)
            (PUBLIC / "og.png").write_bytes(og.screenshot())
            print(f"{(PUBLIC / 'og.png').relative_to(ROOT)}  1200x630")

            icon = br.new_page(viewport={"width": 180, "height": 180})
            svg = (PUBLIC / "favicon.svg").read_text()
            icon.set_content(
                f'<body style="margin:0">{svg.replace("<svg ", "<svg width=180 height=180 ", 1)}</body>'
            )
            (PUBLIC / "apple-touch-icon.png").write_bytes(icon.screenshot())
            br.close()
    finally:
        srv.terminate()
        srv.wait(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
