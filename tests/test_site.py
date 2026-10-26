"""The website layer: SEO basics, redirects, 404, contact form, headers. Uses the built pages in apps/web/dist."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from helpers import build
from mandate import clock
from mandate.main import WEB_DIST, create_app
from starlette.testclient import TestClient

pytestmark = pytest.mark.skipif(not (WEB_DIST / "index.html").exists(), reason="web UI not built (make web)")

BASE = "https://mandate.example.org"
PAGES = ["/", "/about", "/console.html", "/contact", "/privacy"]
GOOD = {
    "name": "Ada",
    "email": "ada@example.com",
    "subject": "Hello",
    "message": "I have a question about mandates.",
}


def make(**over):  # type: ignore[no-untyped-def]
    rt = build(public_base_url=BASE, **over)
    return rt, TestClient(create_app(rt), follow_redirects=False)


@pytest.fixture(autouse=True)
def _reset():  # type: ignore[no-untyped-def]
    yield
    clock.set_clock(None)


def tags(html: str, pattern: str) -> list[str]:
    return re.findall(pattern, html, flags=re.S)


@pytest.mark.parametrize("path", PAGES)
def test_page_seo_basics(path: str) -> None:
    _, c = make()
    r = c.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    h = r.text
    assert "__SITE_URL__" not in h and "<!--VERIFICATION-->" not in h and "<!--NOTICE-->" not in h
    assert len(tags(h, r"<h1[ >]")) == 1, "exactly one h1"
    title = tags(h, r"<title>(.*?)</title>")
    assert len(title) == 1 and 15 <= len(title[0]) <= 70
    desc = tags(h, r'<meta name="description" content="(.*?)"')
    assert len(desc) == 1 and 70 <= len(desc[0]) <= 175
    canon = BASE + ("" if path == "/" else path)
    assert tags(h, r'<link rel="canonical" href="(.*?)"') == [canon if path != "/" else BASE + "/"]
    assert tags(h, r'<meta property="og:image" content="(.*?)"') == [f"{BASE}/og.png"]
    assert tags(h, r'<meta property="og:url" content="(.*?)"') == [canon if path != "/" else BASE + "/"]
    assert 'lang="en"' in h and 'name="viewport"' in h
    for blob in tags(h, r'<script type="application/ld\+json">(.*?)</script>'):
        assert json.loads(blob)["@context"] == "https://schema.org"


def test_titles_and_descriptions_are_unique() -> None:
    _, c = make()
    titles, descs = [], []
    for p in PAGES:
        h = c.get(p).text
        titles += tags(h, r"<title>(.*?)</title>")
        descs += tags(h, r'<meta name="description" content="(.*?)"')
    assert len(set(titles)) == len(PAGES) and len(set(descs)) == len(PAGES)


def test_structured_data_and_breadcrumbs() -> None:
    _, c = make()
    home = [
        json.loads(b) for b in tags(c.get("/").text, r'<script type="application/ld\+json">(.*?)</script>')
    ]
    assert {"Organization", "WebSite", "SoftwareApplication"} <= {d["@type"] for d in home}
    assert next(d for d in home if d["@type"] == "Organization")["url"] == BASE + "/"
    for p in ("/about", "/contact", "/privacy", "/console.html"):
        h = c.get(p).text
        kinds = [
            json.loads(b)["@type"] for b in tags(h, r'<script type="application/ld\+json">(.*?)</script>')
        ]
        assert "BreadcrumbList" in kinds, p
        assert 'aria-label="Breadcrumb"' in h, p


def test_images_have_alt_lazy_and_webp() -> None:
    _, c = make()
    h = c.get("/about").text
    imgs = tags(h, r"<img\b[^>]*>")
    assert len(imgs) >= 2
    for im in imgs:
        assert re.search(r'alt="[^"]{12,}"', im), im
        assert 'loading="lazy"' in im and ".webp" in im, im
        assert "width=" in im and "height=" in im, im
    for f in ("img/buyer.webp", "img/console.webp", "og.png", "apple-touch-icon.png"):
        assert c.get("/" + f).status_code == 200, f
    assert c.get("/og.png").headers["content-type"] == "image/png"


def test_pages_are_server_rendered() -> None:
    """The HTML a crawler receives already contains the content, not an empty root div."""
    _, c = make()
    assert "Let your agent pay. Keep the proof." in c.get("/").text
    assert "Privacy" in c.get("/privacy").text and "<form" in c.get("/contact").text


def test_robots_and_sitemap() -> None:
    _, c = make()
    robots = c.get("/robots.txt")
    assert robots.status_code == 200 and f"Sitemap: {BASE}/sitemap.xml" in robots.text
    assert "Disallow: /api/" in robots.text and "Allow: /" in robots.text
    sm = c.get("/sitemap.xml")
    assert sm.status_code == 200 and "xml" in sm.headers["content-type"]
    locs = tags(sm.text, r"<loc>(.*?)</loc>")
    assert locs == [BASE + ("/" if p == "/" else p) for p in PAGES]
    for p in PAGES:  # every sitemap URL really resolves
        assert c.get(p).status_code == 200


def test_robots_uses_request_host_without_public_url() -> None:
    rt = build()
    c = TestClient(create_app(rt), base_url="http://example.test", follow_redirects=False)
    assert "Sitemap: http://example.test/sitemap.xml" in c.get("/robots.txt").text


@pytest.mark.parametrize(
    ("old", "new"),
    [("/index.html", "/"), ("/home", "/"), ("/console", "/console.html"), ("/about.html", "/about"),
     ("/privacy-policy", "/privacy"), ("/contact-us", "/contact"), ("/favicon.ico", "/favicon.svg")],
)  # fmt: skip
def test_301_redirects(old: str, new: str) -> None:
    _, c = make()
    r = c.get(old)
    assert r.status_code == 301 and r.headers["location"] == new


def test_trailing_slash_redirects_and_api_is_untouched() -> None:
    _, c = make()
    r = c.get("/about/?x=1")
    assert r.status_code == 301 and r.headers["location"] == "/about?x=1"
    assert c.get("/api/config").status_code == 200
    assert c.get("/health").status_code == 200


def test_custom_404_html_vs_api_json() -> None:
    _, c = make()
    r = c.get("/no-such-page")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Page not found" in r.text and "noindex" in r.text and "__SITE_URL__" not in r.text
    assert c.get("/api/no-such-route").status_code == 404
    j = c.get("/api/no-such-route")
    assert j.headers["content-type"].startswith("application/json") and "detail" in j.json()
    assert c.post("/no-such-page").status_code in (404, 405)


def test_security_headers_on_pages_and_api() -> None:
    _, c = make()
    for p in ("/", "/api/config", "/no-such-page"):
        h = c.get(p).headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] and "camera=()" in h["permissions-policy"]


def test_force_https_redirect() -> None:
    _, c = make(force_https=True)
    r = c.get("/about", headers={"x-forwarded-proto": "http"})
    assert r.status_code == 301 and r.headers["location"].startswith("https://")
    assert c.get("/health", headers={"x-forwarded-proto": "http"}).status_code == 200
    assert c.get("/about", headers={"x-forwarded-proto": "https"}).status_code == 200


def test_search_console_verification_meta_only_when_configured() -> None:
    _, c = make()
    assert "google-site-verification" not in c.get("/").text
    _, c2 = make(google_site_verification='abc"123')
    assert '<meta name="google-site-verification" content="abc&quot;123">' in c2.get("/").text


def test_analytics_id_only_exposed_when_configured() -> None:
    _, c = make()
    assert not c.get("/api/config").json().get("ga_id")
    _, c2 = make(ga_measurement_id="G-TEST1234")
    assert c2.get("/api/config").json()["ga_id"] == "G-TEST1234"
    assert "G-TEST1234" not in c2.get("/").text, (
        "never injected into HTML; the page loads it only after consent"
    )


# ------------------------------------------------------------------------------------------- contact form
def test_contact_json_success_is_stored() -> None:
    rt, c = make(admin_token="s3cret")
    r = c.post("/api/contact", json=GOOD)
    assert r.status_code == 200 and r.json()["ok"] is True
    rows = c.get("/api/contact/messages", headers={"Authorization": "Bearer s3cret"}).json()
    assert len(rows) == 1 and rows[0]["email"] == "ada@example.com" and rows[0]["emailed"] is False
    assert "ip" not in json.dumps(rows).lower() or "sender_hash" not in rows[0]
    assert rt.contact is not None


def test_contact_validation_errors() -> None:
    _, c = make()
    for bad, field in [({**GOOD, "email": "nope"}, "email"), ({**GOOD, "message": "short"}, "message"),
                       ({**GOOD, "name": ""}, "name"), ({**GOOD, "message": "x" * 3001}, "message")]:  # fmt: skip
        r = c.post("/api/contact", json=bad)
        assert r.status_code == 422 and r.json()["detail"][0]["loc"][-1] == field
    assert c.post("/api/contact", json=["not", "an", "object"]).status_code == 422
    assert (
        c.post("/api/contact", content=b"{oops", headers={"content-type": "application/json"}).status_code
        == 422
    )


def test_contact_form_post_redirects_back() -> None:
    _, c = make()
    ok = c.post("/api/contact", data=GOOD)
    assert ok.status_code == 303 and ok.headers["location"] == "/contact?sent=1"
    page = c.get("/contact?sent=1").text
    assert "Your message was received" in page and 'role="status"' in page
    bad = c.post("/api/contact", data={**GOOD, "email": "nope"})
    assert bad.status_code == 303 and bad.headers["location"].startswith("/contact?error=email")
    assert 'role="alert"' in c.get(bad.headers["location"]).text


def test_contact_error_message_is_escaped() -> None:
    _, c = make()
    page = c.get("/contact?error=%3Cscript%3Ealert(1)%3C/script%3E").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


def test_contact_honeypot_pretends_success_stores_nothing() -> None:
    _, c = make(admin_token="t")
    assert c.post("/api/contact", json={**GOOD, "website": "http://spam.example"}).json()["ok"] is True
    assert c.get("/api/contact/messages", headers={"Authorization": "Bearer t"}).json() == []


def test_contact_rate_limit() -> None:
    _, c = make()
    codes = [c.post("/api/contact", json=GOOD).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    r = c.post("/api/contact", json=GOOD)
    assert r.headers["retry-after"] == "3600"
    assert c.post("/api/contact", data=GOOD).headers["location"].startswith("/contact?error=too")


def test_contact_admin_endpoint_is_locked() -> None:
    _, c = make()  # no token configured: the route pretends not to exist
    assert c.get("/api/contact/messages").status_code == 404
    _, c2 = make(admin_token="right")
    assert c2.get("/api/contact/messages").status_code == 401
    assert c2.get("/api/contact/messages", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c2.get("/api/contact/messages", headers={"Authorization": "Bearer right"}).status_code == 200


def test_contact_email_notification_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    import mandate.contact as contact

    sent: list[dict] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int = 0) -> None:
            sent.append({"host": host, "port": port})

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def starttls(self) -> None:
            sent[-1]["tls"] = True

        def login(self, u: str, p: str) -> None:
            sent[-1]["login"] = u

        def send_message(self, m) -> None:  # type: ignore[no-untyped-def]
            sent[-1]["to"], sent[-1]["reply_to"], sent[-1]["subject"] = m["To"], m["Reply-To"], m["Subject"]

    monkeypatch.setattr(contact.smtplib, "SMTP", FakeSMTP)
    rt, _ = make(smtp_host="smtp.test", smtp_user="u", smtp_password="p", contact_to="me@example.com")
    svc = rt.contact
    assert svc is not None
    data = contact.ContactIn(**GOOD)
    assert svc._send("nonexistent", data) is True  # noqa: SLF001
    assert sent[0] == {"host": "smtp.test", "port": 587, "tls": True, "login": "u", "to": "me@example.com",
                       "reply_to": "ada@example.com", "subject": "[Mandate contact] Hello"}  # fmt: skip

    def boom(*a: object, **k: object) -> None:
        raise OSError("smtp down")

    monkeypatch.setattr(contact.smtplib, "SMTP", boom)
    assert svc._send("x", data) is False  # noqa: SLF001  failure is swallowed, message already stored


def test_contact_header_injection_is_neutralised() -> None:
    from mandate.contact import ContactIn

    c = ContactIn(
        name="A\r\nBcc: x@y.z",
        email="a@b.co",
        subject="Hi\r\nBcc: evil@x.co",
        message="a long enough message",
    )
    assert "\n" not in c.name and "\n" not in c.subject


def test_no_ui_fallback_when_not_built(monkeypatch: pytest.MonkeyPatch) -> None:
    import mandate.main as main

    monkeypatch.setattr(main, "WEB_DIST", Path("/nonexistent-dist"))
    c = TestClient(create_app(build()), follow_redirects=False)
    r = c.get("/")
    assert r.status_code == 200 and "Mandate API is running" in r.text
    assert c.get("/robots.txt").status_code == 200 and c.get("/api/config").status_code == 200
    assert c.get("/nope").status_code == 404
