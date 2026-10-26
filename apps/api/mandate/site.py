"""The website layer: pages with real canonical URLs, robots.txt, sitemap.xml, redirects, a custom 404, the
contact form and security headers. The pages themselves are prerendered by the web build (apps/web)."""

from __future__ import annotations

import hmac
import html
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .contact import ContactIn
from .settings import Settings

# public path -> built html file. Each page has one canonical URL (the key).
PAGES: dict[str, str] = {
    "/": "index.html",
    "/about": "about.html",
    "/console.html": "console.html",
    "/contact": "contact.html",
    "/privacy": "privacy.html",
}
SITEMAP_PRIORITY = {"/": "1.0", "/about": "0.8", "/console.html": "0.6", "/contact": "0.5", "/privacy": "0.3"}
# old and alternative addresses -> canonical (permanent redirects)
REDIRECTS: dict[str, str] = {
    "/index.html": "/", "/home": "/", "/buyer": "/", "/app": "/",
    "/console": "/console.html", "/merchant": "/console.html",
    "/about.html": "/about", "/about-us": "/about",
    "/privacy.html": "/privacy", "/privacy-policy": "/privacy",
    "/contact.html": "/contact", "/contact-us": "/contact",
    "/favicon.ico": "/favicon.svg",
}  # fmt: skip
NO_SLASH_PREFIXES = ("/api", "/docs", "/openapi", "/webhooks", "/mcp", "/assets", "/health", "/redoc")
NO_UI_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mandate API</title><style>body{font:16px/1.55 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;color:#0d1526}
code{background:#eaeef3;padding:1px 6px;border-radius:5px}a{color:#1a3bcb}</style>
<h1>Mandate API is running</h1>
<p>The web app has not been built, so this is the API only. Build it with <code>make web</code>, or run
<code>python scripts/quickstart.py serve</code>, which builds it for you when Node is installed.</p>
<ul><li><a href="/docs">Interactive API docs</a></li><li><a href="/api/config">What is real and what is simulated</a></li>
<li><a href="/health">Health</a></li></ul></html>"""
NOT_FOUND_FALLBACK = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Page not found | Mandate</title>
<h1>Page not found</h1><p><a href="/">Go to the Mandate buyer app</a></p></html>"""

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def site_url(request: Request, cfg: Settings) -> str:
    return (cfg.public_base_url or str(request.base_url)).rstrip("/")


class _Immutable(StaticFiles):
    """Hashed build assets can be cached for a year; everything else revalidates."""

    async def get_response(self, path: str, scope: Any) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if path.startswith("assets/")
                else "public, max-age=3600"
            )
        return resp


def install(app: FastAPI, cfg: Settings, web_dist: Path, contact: Any) -> None:
    """Register site routes. `contact` is a callable returning the ContactService (built lazily with the runtime)."""
    cache: dict[str, tuple[float, str]] = {}

    def read(name: str) -> str | None:
        f = web_dist / name
        if not f.is_file():
            return None
        m = f.stat().st_mtime
        hit = cache.get(name)
        if hit is None or hit[0] != m:
            cache[name] = (m, f.read_text(encoding="utf-8"))
        return cache[name][1]

    def fill(src: str, request: Request, notice: str = "") -> str:
        meta = ""
        if cfg.google_site_verification:
            meta = f'<meta name="google-site-verification" content="{html.escape(cfg.google_site_verification, quote=True)}">'
        return (src.replace("__SITE_URL__", site_url(request, cfg)).replace("<!--VERIFICATION-->", meta)
                .replace("<!--NOTICE-->", notice))  # fmt: skip

    # ------------------------------------------------------------------ middleware: https, trailing slash, headers
    @app.middleware("http")
    async def _site_rules(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if (
            cfg.force_https
            and request.headers.get("x-forwarded-proto") == "http"
            and not path.startswith(("/health", "/webhooks"))
        ):
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=301)
        if len(path) > 1 and path.endswith("/") and not path.startswith(NO_SLASH_PREFIXES):
            target = path.rstrip("/") or "/"
            return RedirectResponse(
                target + (f"?{request.url.query}" if request.url.query else ""), status_code=301
            )
        resp = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    # ------------------------------------------------------------------ robots, sitemap
    @app.get("/robots.txt", include_in_schema=False)
    def robots(request: Request) -> Response:
        return PlainTextResponse(
            "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /webhooks/\nDisallow: /mcp\nDisallow: /docs\n"
            f"Disallow: /openapi.json\n\nSitemap: {site_url(request, cfg)}/sitemap.xml\n"
        )

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap(request: Request) -> Response:
        base = site_url(request, cfg)
        rows = []
        for path, name in PAGES.items():
            f = web_dist / name
            if not f.is_file():
                continue
            last = datetime.fromtimestamp(f.stat().st_mtime, UTC).strftime("%Y-%m-%d")
            rows.append(f"  <url><loc>{html.escape(base + (path if path != '/' else '/'))}</loc><lastmod>{last}</lastmod>"
                        f"<priority>{SITEMAP_PRIORITY.get(path, '0.5')}</priority></url>")  # fmt: skip
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(rows)
            + "\n</urlset>\n"
        )
        return Response(xml, media_type="application/xml")

    # ------------------------------------------------------------------ redirects (permanent)
    for old, new in REDIRECTS.items():
        app.add_api_route(old, (lambda target: lambda: RedirectResponse(target, status_code=301))(new),
                          methods=["GET", "HEAD"], include_in_schema=False)  # fmt: skip

    # ------------------------------------------------------------------ contact form
    def _err_list(e: ValidationError) -> list[dict]:
        return [
            {"loc": ["body", *map(str, x["loc"])], "msg": x["msg"].removeprefix("Value error, ")}
            for x in e.errors()
        ]

    @app.post("/api/contact", tags=["site"])
    async def post_contact(request: Request) -> Response:
        is_json = request.headers.get("content-type", "").startswith("application/json")
        try:
            raw = await request.json() if is_json else dict(await request.form())
        except ValueError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        ip = request.client.host if request.client else "unknown"
        try:
            body = ContactIn.model_validate({k: v for k, v in raw.items() if isinstance(v, str)})
            result = contact().submit(body, ip)
        except ValidationError as e:
            if is_json:
                return JSONResponse({"detail": _err_list(e)}, status_code=422)
            first = _err_list(e)[0]
            return RedirectResponse(
                f"/contact?error={quote(first['loc'][-1] + ': ' + first['msg'])}", status_code=303
            )
        except PermissionError as e:
            if is_json:
                return JSONResponse({"detail": str(e)}, status_code=429, headers={"Retry-After": "3600"})
            return RedirectResponse(f"/contact?error={quote(str(e))}", status_code=303)
        if is_json:
            return JSONResponse({"ok": True, "id": result.get("id")})
        return RedirectResponse("/contact?sent=1", status_code=303)

    @app.get("/api/contact/messages", tags=["site"])
    def list_contact(request: Request, limit: int = 100) -> Response:
        if not cfg.admin_token:
            raise StarletteHTTPException(404, "Not Found")
        sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(sent.encode(), cfg.admin_token.encode()):
            return JSONResponse({"detail": "missing or wrong admin token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})  # fmt: skip
        return JSONResponse(contact().list(min(max(limit, 1), 500)))

    # ------------------------------------------------------------------ pages
    if not web_dist.exists():

        @app.get("/", include_in_schema=False)
        def no_ui() -> Response:
            return Response(NO_UI_PAGE, media_type="text/html")

        return

    def make_page(path: str, name: str):  # type: ignore[no-untyped-def]
        def page(request: Request) -> Response:
            src = read(name)
            if src is None:
                raise StarletteHTTPException(404, "Not Found")
            notice = ""
            if path == "/contact":
                if request.query_params.get("sent"):
                    notice = '<p class="notice ok" role="status">Thank you. Your message was received.</p>'
                elif err := request.query_params.get("error"):
                    notice = f'<p class="notice bad" role="alert">{html.escape(err[:200])}</p>'
            return HTMLResponse(fill(src, request, notice), headers={"Cache-Control": "no-cache"})

        return page

    for path, name in PAGES.items():
        app.add_api_route(path, make_page(path, name), methods=["GET", "HEAD"], include_in_schema=False)
    app.mount("/", _Immutable(directory=str(web_dist), html=False), name="static")

    # ------------------------------------------------------------------ custom 404 for browsers, JSON for the API
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path
        wants_html = (exc.status_code == 404 and request.method in ("GET", "HEAD")
                      and not path.startswith(("/api", "/mcp", "/webhooks", "/sim")))  # fmt: skip
        if wants_html:
            src = read("404.html") or NOT_FOUND_FALLBACK
            return HTMLResponse(fill(src, request), status_code=404, headers={"Cache-Control": "no-store"})
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None)
        )
