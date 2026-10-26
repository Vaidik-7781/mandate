"""PayPal webhook signature verification on the raw body.

Algorithm (PayPal docs): message = transmissionId|timeStamp|webhookId|crc32(raw body), signed with
SHA256-RSA by the certificate at `paypal-cert-url`. Rules enforced here:

* verify against the *raw* bytes, never a re-serialised JSON body,
* validate the certificate URL before fetching it (SSRF): https, host under paypal.com, path under the certs path,
* the certificate must be inside its validity window,
* the transmission time must be recent (PayPal retries for up to 3 days).
"""

from __future__ import annotations

import base64
import datetime as dt
import zlib
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ... import clock

CERT_PATH_PREFIX = "/v1/notifications/certs/"
ALLOWED_HOST_SUFFIXES = (".paypal.com",)
MAX_AGE_SECONDS = 3 * 86400 + 3600
CertFetcher = Callable[[str], bytes]


class WebhookError(Exception):
    """Signature or header validation failed; the delivery must not be trusted."""


def validate_cert_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme != "https":
        raise WebhookError("cert url must be https")
    host = (p.hostname or "").lower()
    if not any(host.endswith(sfx) for sfx in ALLOWED_HOST_SUFFIXES) or p.username or p.password:
        raise WebhookError(f"cert url host not allowed: {host!r}")
    if p.port not in (None, 443):
        raise WebhookError("cert url port not allowed")
    if not p.path.startswith(CERT_PATH_PREFIX) or ".." in p.path:
        raise WebhookError("cert url path not allowed")


def crc32_decimal(body: bytes) -> int:
    return zlib.crc32(body) & 0xFFFFFFFF


def expected_message(transmission_id: str, transmission_time: str, webhook_id: str, body: bytes) -> bytes:
    return f"{transmission_id}|{transmission_time}|{webhook_id}|{crc32_decimal(body)}".encode()


def _parse_time(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def verify_signature(
    headers: dict[str, str],
    raw_body: bytes,
    webhook_id: str,
    cert_fetcher: CertFetcher,
    now: float | None = None,
) -> None:
    """Raise WebhookError unless the delivery is authentic. Header names are matched case-insensitively."""
    h = {k.lower(): v for k, v in headers.items()}
    try:
        tid, ttime = h["paypal-transmission-id"], h["paypal-transmission-time"]
        sig, cert_url = h["paypal-transmission-sig"], h["paypal-cert-url"]
    except KeyError as exc:
        raise WebhookError(f"missing header {exc.args[0]}") from exc
    if h.get("paypal-auth-algo", "SHA256withRSA").upper() != "SHA256WITHRSA":
        raise WebhookError("unsupported auth algorithm")
    validate_cert_url(cert_url)
    try:
        age = (clock.now_f() if now is None else now) - _parse_time(ttime)
    except ValueError as exc:
        raise WebhookError("bad transmission time") from exc
    if age > MAX_AGE_SECONDS or age < -600:
        raise WebhookError("transmission time outside the accepted window")
    try:
        cert = x509.load_pem_x509_certificate(cert_fetcher(cert_url))
    except Exception as exc:
        raise WebhookError(f"could not load certificate: {exc}") from exc
    t = dt.datetime.fromtimestamp(clock.now_f() if now is None else now, dt.UTC)
    if not (cert.not_valid_before_utc <= t <= cert.not_valid_after_utc):
        raise WebhookError("certificate outside its validity window")
    pub = cert.public_key()
    if not isinstance(pub, rsa.RSAPublicKey):
        raise WebhookError("certificate key is not RSA")
    try:
        pub.verify(
            base64.b64decode(sig),
            expected_message(tid, ttime, webhook_id, raw_body),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise WebhookError("signature mismatch") from exc


class HttpCertFetcher:
    """Fetches and caches PayPal certificates. Only validated URLs are ever requested."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=10.0)
        self._cache: dict[str, bytes] = {}

    def __call__(self, url: str) -> bytes:
        validate_cert_url(url)
        if url not in self._cache:
            r = self.http.get(url)
            r.raise_for_status()
            if len(r.content) > 64 * 1024:
                raise WebhookError("certificate response too large")
            self._cache[url] = r.content
        return self._cache[url]
