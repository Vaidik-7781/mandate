"""Write docs/openapi.json and a Postman collection (docs/mandate.postman_collection.json) from the live app.

python scripts/export_api.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from mandate.db import Database  # noqa: E402
from mandate.main import create_app  # noqa: E402
from mandate.runtime import Runtime  # noqa: E402
from mandate.settings import Settings  # noqa: E402


def postman(spec: dict) -> dict:
    items = []
    for path, ops in sorted(spec["paths"].items()):
        for method, op in ops.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            url = "{{base_url}}" + "".join(
                f"/:{p[1:-1]}" if p.startswith("{") else f"/{p}" for p in path.split("/") if p
            )
            req: dict = {"method": method.upper(), "header": [], "url": {"raw": url, "host": ["{{base_url}}"],
                         "path": [(f":{p[1:-1]}" if p.startswith("{") else p) for p in path.split("/") if p]}}  # fmt: skip
            if "requestBody" in op:
                req["header"].append({"key": "Content-Type", "value": "application/json"})
                req["body"] = {"mode": "raw", "raw": "{}", "options": {"raw": {"language": "json"}}}
            items.append({"name": op.get("summary") or f"{method.upper()} {path}", "request": req})
    return {"info": {"name": "Mandate API", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            "variable": [{"key": "base_url", "value": "http://localhost:8000"}], "item": items}  # fmt: skip


def main() -> int:
    cfg = Settings(database_url="sqlite://", replay_mode=True)
    app = create_app(Runtime.build(cfg, db=Database("sqlite://")))
    spec = app.openapi()
    docs = ROOT / "docs"
    (docs / "openapi.json").write_text(json.dumps(spec, indent=1) + "\n")
    (docs / "mandate.postman_collection.json").write_text(json.dumps(postman(spec), indent=1) + "\n")
    print(f"wrote docs/openapi.json ({len(spec['paths'])} paths) and the Postman collection")
    return 0


if __name__ == "__main__":
    sys.exit(main())
