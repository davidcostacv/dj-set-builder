"""Shared HTTP client, central retry wrapper, and a rate limiter.

Every outbound request in the app goes through :func:`request`. It honours
``Retry-After`` on 429 everywhere (not just export), backs off exponentially on
5xx and transport errors, and logs failures with timestamps.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_ATTEMPTS = 5
RETRY_STATUS = {429, 500, 502, 503, 504}

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _ssl_context() -> "ssl.SSLContext | bool":
    """Verify against the OS trust store rather than certifi's bundle.

    Consumer antivirus and corporate proxies intercept HTTPS with a locally
    generated root CA. That root is installed in the OS store, but not in
    certifi's, so certifi-based verification fails outright — Avast's root, for
    one, also has a non-critical Basic Constraints extension that OpenSSL 3.x
    rejects. Deferring to the OS store keeps verification fully on while
    respecting roots the user has actually installed.

    Falls back to httpx's default (certifi) if truststore is unavailable.
    Verification is never disabled.
    """
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception as exc:  # pragma: no cover - depends on the host
        log.debug("truststore unavailable (%s); using certifi", exc)
        return True


def client() -> httpx.Client:
    """Process-wide shared client (connection pooling, keep-alive)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=DEFAULT_TIMEOUT,
                headers={"User-Agent": "djset/0.1 (personal, local)"},
                follow_redirects=True,
                verify=_ssl_context(),
            )
        return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


class RateLimiter:
    """Simple thread-safe spacing limiter: at most ``per_hour`` calls per hour."""

    def __init__(self, per_hour: int) -> None:
        self.min_interval = 3600.0 / max(1, per_hour)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                delay = self._next_at - now
            else:
                delay = 0.0
            self._next_at = max(now, self._next_at) + self.min_interval
        if delay > 0:
            time.sleep(delay)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status = status
        self.url = url
        self.body = body


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    data: Mapping[str, Any] | None = None,
    on_unauthorized: Callable[[], Mapping[str, str]] | None = None,
    rate_limiter: RateLimiter | None = None,
) -> httpx.Response:
    """Perform a request with retry, backoff, and 429 handling.

    ``on_unauthorized`` is called once on a 401 and must return fresh headers
    (that is where the token refresh happens); the request is then retried.
    """
    hdrs = dict(headers or {})
    refreshed = False

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if rate_limiter is not None:
            rate_limiter.wait()
        try:
            resp = client().request(
                method, url, headers=hdrs, params=params, json=json_body, data=data
            )
        except httpx.TransportError as exc:
            if attempt == MAX_ATTEMPTS:
                log.error("transport error (final) %s %s: %s", method, url, exc)
                raise
            delay = _backoff(attempt)
            log.warning(
                "transport error %s %s: %s — retry %d/%d in %.1fs",
                method, url, exc, attempt, MAX_ATTEMPTS, delay,
            )
            time.sleep(delay)
            continue

        if resp.status_code == 401 and on_unauthorized is not None and not refreshed:
            refreshed = True
            log.info("401 on %s — refreshing token", url)
            hdrs.update(on_unauthorized())
            continue

        if resp.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
            wait = _retry_after_seconds(resp)
            delay = (wait + 1.0) if wait is not None else _backoff(attempt)
            log.warning(
                "HTTP %d %s — retry %d/%d in %.1fs",
                resp.status_code, url, attempt, MAX_ATTEMPTS, delay,
            )
            time.sleep(delay)
            continue

        if resp.status_code >= 400:
            log.error("HTTP %d %s: %s", resp.status_code, url, resp.text[:400])
            raise HttpError(resp.status_code, url, resp.text)

        return resp

    raise HttpError(0, url, "exhausted retries")


def _backoff(attempt: int) -> float:
    return min(30.0, (2 ** (attempt - 1))) + random.uniform(0, 0.5)
