"""One command from a fresh checkout to a running demo. Works on macOS, Linux and Windows.

    python scripts/quickstart.py serve     # install what is missing, then API + UI on http://localhost:8000
    python scripts/quickstart.py demo      # the scripted end-to-end run in the terminal (no server)
    python scripts/quickstart.py test      # the test suite
    python scripts/quickstart.py mcp       # MCP server on stdio with one demo mandate signed for you
    python scripts/quickstart.py sandbox   # end-to-end check against PayPal's sandbox (needs your sandbox keys)

No PayPal keys and no model key are needed: the default is replay mode (an in-process PayPal simulator and offline
rules). Python 3.12+ is required. The UI ships prebuilt; Node is only needed if you change the web code.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps" / "api"


def say(msg: str) -> None:
    print(f"[mandate] {msg}", flush=True)


def ensure_python() -> None:
    if sys.version_info[:2] < (3, 12):  # noqa: UP036
        sys.exit(f"Python 3.12 or newer is required (this is {sys.version.split()[0]}).")


def ensure_deps() -> None:
    need = ("cryptography", "fastapi", "httpx", "pydantic", "reportlab", "sqlalchemy", "uvicorn", "yaml")
    if all(importlib.util.find_spec(m) for m in need):
        return
    say("Installing Python dependencies (one time) ...")
    extra = ["pytest", "pytest-asyncio"] if (len(sys.argv) > 1 and sys.argv[1] == "test") else []
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-e", str(ROOT), *extra])


def ensure_ui() -> None:
    if (ROOT / "apps" / "web" / "dist" / "index.html").exists():
        return
    npm = shutil.which("npm")
    if not npm:
        say("The web UI is not built and Node is not installed: starting the API only.")
        return
    say("Building the web UI (one time) ...")
    web = ROOT / "apps" / "web"
    subprocess.check_call([npm, "ci"], cwd=web)
    subprocess.check_call([npm, "run", "build"], cwd=web)


def env() -> dict[str, str]:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(API) + os.pathsep + e.get("PYTHONPATH", "")
    return e


def run(cmd: list[str], cwd: Path = ROOT) -> int:
    try:
        return subprocess.call(cmd, cwd=cwd, env=env())
    except KeyboardInterrupt:  # Ctrl+C is how a server is stopped; not an error
        say("Stopped.")
        return 0


def main() -> int:
    ensure_python()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd not in {"serve", "demo", "test", "mcp", "sandbox"}:
        print(__doc__)
        return 2
    ensure_deps()
    if cmd == "serve":
        ensure_ui()
        port = os.environ.get("PORT", "8000")
        say(
            f"Open http://localhost:{port}  (buyer app)  and  http://localhost:{port}/console.html  (merchant console)"
        )
        say("Replay mode: PayPal is simulated unless PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are set.")
        return run([sys.executable, "-m", "uvicorn", "mandate.main:app", "--port", port], cwd=API)
    if cmd == "demo":
        return run([sys.executable, "scripts/demo.py"])
    if cmd == "test":
        return run([sys.executable, "-m", "pytest", "-q"])
    if cmd == "mcp":
        return run([sys.executable, "-m", "mandate.mcp", "--demo-mandate", *sys.argv[2:]], cwd=API)
    return run([sys.executable, "-m", "mandate.sandbox_check"])


if __name__ == "__main__":
    sys.exit(main())
