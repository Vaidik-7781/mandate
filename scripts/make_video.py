"""Record the demo: a captioned screen recording plus the script and subtitles for YOUR voice-over.

    python scripts/make_video.py [--out docs/demo]

Outputs (in --out): mandate-demo-captioned.mp4 (burned-in captions), mandate-demo-silent.mp4 (no captions, for
adding your own voice and editing), mandate-demo.srt, narration.txt and docs/demo-script.md (all generated from the
SEGMENTS below, so the words, the timing and the picture cannot drift apart).

It does not synthesise a voice: a judge should hear a person. Record the narration yourself (read narration.txt,
roughly 150 words per minute, one paragraph per segment) and join it:

    ffmpeg -i mandate-demo-silent.mp4 -i voice.m4a -c:v copy -c:a aac -shortest mandate-demo-final.mp4

Needs Playwright with Chromium, ffmpeg, and the built UI (apps/web/dist). Uses the simulator, and says so on screen
and in the narration.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]

# (id, seconds, what is on screen, narration)
SEGMENTS: list[tuple[str, float, str, str]] = [
    (
        "hook",
        12,
        "Buyer app, top of the page.",
        "Imagine your mum shops through an AI assistant. One poisoned product review could talk it into spending her "
        "money. Agents can pay. Mandate lets them prove they were allowed to.",
    ),
    (
        "limits",
        22,
        "Pick the Family caregiver situation, press Turn into limits.",
        "I say what her assistant may buy: household essentials, under thirty dollars, one approved store, one week. "
        "Mandate turns that into limits, and if I leave one out it asks me instead of guessing.",
    ),
    (
        "sign",
        11,
        "Press Sign this mandate.",
        "I sign it. That creates a credential chain. The assistant can only spend inside it, and the PayPal order will "
        "carry the hash of that chain.",
    ),
    (
        "buy",
        24,
        "Buy for me, approve, capture, open the signed receipt.",
        "The assistant buys a night light. I approve, it captures, and I get a receipt that is signed and tied to my "
        "mandate. In this demo PayPal is a simulator; the real sandbox runs with your own keys.",
    ),
    (
        "hijack",
        32,
        "Simulate a hijacked agent; the Refused slip appears.",
        "Now a review hides an instruction to buy something she never allowed. This agent obeys it. The guard refuses "
        "and shows why. The model proposes; plain code decides.",
    ),
    (
        "dispute",
        32,
        "Open a dispute, then the console: fault split, decision, evidence note, injection message.",
        "A buyer opens a dispute. The dispute agent applies a published rule, builds an evidence packet and decides. "
        "Sandbox outcomes are scripted, so I claim no win rate. And a message telling it to refund is flagged for a human.",
    ),
    (
        "proof",
        26,
        "Audit log, Verify the whole chain, then the Mandate-Bench tab.",
        "Every decision lands in a hash-chained log you can verify. And the benchmark: scripted attackers, two hundred "
        "and ninety attacks, none get through the full system. That is synthetic. A real-model run is one command.",
    ),
    (
        "close",
        9,
        "Back to the buyer app, Revoke.",
        "One switch ends the authority and returns what was not delivered. Mandate. Open source, Apache two.",
    ),
]
TOTAL = sum(s[1] for s in SEGMENTS)

CURSOR_JS = """
(() => {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;z-index:99999;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;' +
    'border:3px solid #1a3bcb;background:rgba(26,59,203,.18);pointer-events:none;left:-50px;top:-50px;transition:left .35s,top .35s';
  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(d));
  document.addEventListener('mousemove', e => { d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px'; }, true);
})();
"""


def ts(sec: float, comma: bool = True) -> str:
    h, rem = divmod(int(sec * 1000), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02}{',' if comma else '.'}{ms:03}"


def write_texts(out: Path) -> None:
    srt, t = [], 0.0
    for i, (_, dur, _, text) in enumerate(SEGMENTS, 1):
        # split into two lines of caption per segment so it stays readable at a glance
        words = text.split()
        half = len(words) // 2
        cap = " ".join(words[:half]) + "\n" + " ".join(words[half:])
        srt.append(f"{i}\n{ts(t + 0.3)} --> {ts(t + dur - 0.3)}\n{cap}\n")
        t += dur
    (out / "mandate-demo.srt").write_text("\n".join(srt))
    (out / "narration.txt").write_text("\n\n".join(f"[{ts(sum(s[1] for s in SEGMENTS[:i]), False)[3:8]}] {s[3]}"
                                                    for i, s in enumerate(SEGMENTS)) + "\n")  # fmt: skip
    rows, t = [], 0.0
    for _sid, dur, screen, text in SEGMENTS:
        rows.append(f"| {ts(t, False)[3:8]} | {screen} | {text} |")
        t += dur
    words = sum(len(s[3].split()) for s in SEGMENTS)
    (
        ROOT / "docs" / "demo-script.md"
    ).write_text(f"""# Demo script ({int(TOTAL // 60)}:{int(TOTAL % 60):02d}, one story)

*Generated by `scripts/make_video.py` from the same table that drives the recording and the subtitles.*

**One story:** a caregiver delegates her mother's shopping to an assistant. The seven things on screen (limits,
signature, purchase, refusal, dispute, proof, revoke) are the same product seen from one person's side.

The recording is silent on purpose, with burned-in captions in `docs/demo/`. **Record your own voice**: judges
should hear a person, and a synthetic voice reads as filler. About {words} words, about {words * 60 // int(TOTAL)} words per minute
at this pacing, so read it a little briskly or trim a sentence per segment.

| Time | On screen | Say |
|---|---|---|
{chr(10).join(rows)}

Join your recording to the picture:

```bash
ffmpeg -i docs/demo/mandate-demo-silent.mp4 -i voice.m4a -c:v copy -c:a aac -shortest mandate-demo-final.mp4
```

What the video says out loud so nobody has to discover it later: PayPal is simulated unless you bring sandbox
keys; dispute outcomes are scripted; the benchmark is synthetic. Upload it as a public YouTube video and keep
the title free of the word "sandbox-proven": it is not.

To redo the picture after a UI change: `python scripts/make_video.py`.
""")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def click(page: Page, locator, pause: float = 0.5) -> None:  # type: ignore[no-untyped-def]
    locator.scroll_into_view_if_needed()
    locator.hover()
    time.sleep(pause)
    locator.click()


def record(out: Path) -> Path:
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
    tmp = Path(tempfile.mkdtemp())
    try:
        for _ in range(80):
            try:
                if urllib.request.urlopen(f"{base}/health", timeout=1).status == 200:
                    break
            except OSError:
                time.sleep(0.25)
        with sync_playwright() as p:
            exe = os.environ.get("CHROMIUM_PATH")
            browser = (
                p.chromium.launch(executable_path=exe, args=["--no-sandbox"])
                if exe
                else p.chromium.launch(args=["--no-sandbox"])
            )
            ctx = browser.new_context(viewport={"width": 1280, "height": 720}, record_video_dir=str(tmp),
                                      record_video_size={"width": 1280, "height": 720}, color_scheme="light")  # fmt: skip
            ctx.add_init_script(CURSOR_JS)
            page = ctx.new_page()
            t0 = time.monotonic()
            starts = [sum(s[1] for s in SEGMENTS[:i]) for i in range(len(SEGMENTS))]

            def hold(i: int) -> None:  # wait until segment i is over; fail loudly if the actions overran it
                end = starts[i] + SEGMENTS[i][1]
                now = time.monotonic() - t0
                if now > end + 0.4:
                    raise RuntimeError(
                        f"segment {SEGMENTS[i][0]} overran by {now - end:.1f}s: shorten its actions"
                    )
                time.sleep(max(0.0, end - now))

            page.goto(base)
            expect(page.get_by_role("heading", name="What may your agent buy?")).to_be_visible()
            hold(0)
            click(page, page.get_by_role("button", name="Family caregiver"))
            time.sleep(2.0)
            click(page, page.get_by_role("button", name="Turn into limits"))
            expect(page.get_by_role("button", name="Sign this mandate")).to_be_visible()
            hold(1)
            click(page, page.get_by_role("button", name="Sign this mandate"))
            expect(page.get_by_role("heading", name="Your mandate")).to_be_visible()
            hold(2)
            click(page, page.get_by_role("button", name="Buy for me"))
            expect(page.locator(".seal.allow").first).to_be_visible()
            time.sleep(1.5)
            click(page, page.get_by_role("button", name="Approve in simulated PayPal"))
            click(page, page.get_by_role("button", name="Capture payment"))
            click(page, page.get_by_role("button", name="Signed receipt"))
            expect(page.get_by_text("Signature valid.")).to_be_visible()
            hold(3)
            click(page, page.get_by_role("button", name="Simulate a hijacked agent"))
            expect(page.locator(".seal.deny, .seal.step_up").first).to_be_visible()
            page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            time.sleep(3)
            hold(4)
            click(page, page.get_by_role("button", name="Open a dispute").first)
            expect(page.get_by_text("opened. Review it in the Merchant console.")).to_be_visible()
            time.sleep(1.5)
            click(
                page, page.get_by_role("navigation", name="Main").get_by_role("link", name="Merchant console")
            )
            expect(page.get_by_text("Agent decision:").first).to_be_visible()
            time.sleep(2.5)
            click(page, page.get_by_role("button", name="Show evidence note").first)
            time.sleep(3.0)
            click(page, page.get_by_role("button", name="Buyer sends an injection").first)
            time.sleep(2.0)
            hold(5)
            click(page, page.get_by_role("tab", name="Audit log"))
            time.sleep(1.0)
            click(page, page.get_by_role("button", name="Verify the whole chain"))
            expect(page.get_by_text("entries verified")).to_be_visible()
            time.sleep(2.0)
            click(page, page.get_by_role("tab", name="Mandate-Bench"))
            expect(page.get_by_text("How often attacks succeed")).to_be_visible()
            time.sleep(1.0)
            hold(6)
            click(page, page.get_by_role("navigation", name="Main").get_by_role("link", name="Buyer app"))
            expect(page.get_by_role("heading", name="Your mandate")).to_be_visible()
            click(page, page.get_by_role("button", name="Revoke and refund anything undelivered"))
            expect(page.get_by_text("You revoked the mandate")).to_be_visible()
            hold(7)
            ctx.close()
            video = Path(page.video.path())
            browser.close()
        dest = out / "raw.webm"
        shutil.copy(video, dest)
        return dest
    finally:
        srv.terminate()
        srv.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)


def encode(out: Path, raw: Path) -> None:
    common = ["-an", "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
              "-t", f"{TOTAL:.1f}", "-movflags", "+faststart"]  # fmt: skip
    subprocess.check_call(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), *common, str(out / "mandate-demo-silent.mp4")]
    )
    style = "Fontname=Inter,Fontsize=10,PrimaryColour=&H00FFFFFF,BackColour=&HC0201010,BorderStyle=3,Outline=4,Shadow=0,MarginV=14,Alignment=2"
    subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out / "mandate-demo-silent.mp4"),
                           "-vf", f"subtitles={out / 'mandate-demo.srt'}:original_size=1280x720:force_style='{style}'",
                           "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-an",
                           "-movflags", "+faststart", str(out / "mandate-demo-captioned.mp4")])  # fmt: skip
    raw.unlink()


def main() -> int:
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "docs" / "demo"
    out.mkdir(parents=True, exist_ok=True)
    write_texts(out)
    if not (ROOT / "apps/web/dist/index.html").exists():
        print("build the UI first: make web")
        return 2
    encode(out, record(out))
    print(
        f"wrote {out}/mandate-demo-captioned.mp4, mandate-demo-silent.mp4, mandate-demo.srt, narration.txt "
        f"({TOTAL:.0f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
