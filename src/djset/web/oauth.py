"""Spotify authorization for the web app — step W5.

The desktop flow spins up a throwaway HTTP server on 127.0.0.1:8888, opens a
browser, and waits for Spotify to call back. That works for a program running
on the same machine as the browser, and stops working the moment the app is
somewhere else — which is where this project is going.

Here the app *is* the server, so the callback is one of its own routes. The
same shape works at ``http://127.0.0.1:8000/auth/callback`` today and at
``https://somewhere/auth/callback`` once deployed; only the registered URI
changes.

Two things have to survive the seconds between the redirect out and the
callback back:

* the **PKCE verifier**, which proves this app started the flow. It must never
  leave the server — sending it to the browser would defeat the point of PKCE.
* the **state**, which proves the callback belongs to a flow this app started
  rather than one an attacker induced.

They live in memory here, keyed by state, with an expiry and a cap. In memory
is right for a single-user local app and stays right for one hosted process;
a multi-process deployment would need them in shared storage, which is a W6
problem and is called out there rather than pretended away.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# A person has this long to finish authorizing before the flow is abandoned.
PENDING_TTL_S = 600.0

# Bound on concurrent flows. Each /auth/login mints an entry, so without a cap
# a caller could grow this without limit simply by hitting that route.
MAX_PENDING = 32


@dataclass(frozen=True)
class Pending:
    verifier: str
    redirect_uri: str
    created_at: float

    def expired(self, now: float) -> bool:
        return now - self.created_at > PENDING_TTL_S


class PendingFlows:
    """In-flight authorizations, keyed by the state parameter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flows: dict[str, Pending] = {}

    def start(self, state: str, verifier: str, redirect_uri: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._flows) >= MAX_PENDING:
                # Drop the oldest rather than refuse: a stale flow nobody
                # completed should not lock out someone trying again now.
                oldest = min(self._flows, key=lambda k: self._flows[k].created_at)
                del self._flows[oldest]
            self._flows[state] = Pending(verifier, redirect_uri, now)

    def claim(self, state: str) -> Pending | None:
        """Take the flow for this state, if there is a live one.

        Single-use: a state that has been redeemed is removed, so a replayed
        callback finds nothing. Returns None for unknown, expired, or already
        used — the caller cannot tell those apart, and should not, because
        saying which would help someone guessing.
        """
        now = time.time()
        with self._lock:
            self._prune(now)
            flow = self._flows.pop(state, None)
        if flow is None or flow.expired(now):
            return None
        return flow

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._flows.items() if v.expired(now)]:
            del self._flows[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._flows)


pending = PendingFlows()


# The public URL of this deployment, when the app cannot work it out itself.
# Uvicorn's proxy handling rewrites the *scheme* from X-Forwarded-Proto but not
# the host, so behind a load balancer the app derives
# `https://127.0.0.1:8000/auth/callback` — right scheme, wrong host, and
# Spotify refuses it without explaining why. Stating the public URL removes the
# guesswork rather than trusting one more forgeable header.
_ENV_PUBLIC_URL = "DJSET_PUBLIC_URL"


def public_base_url(request_base_url: str) -> str:
    """The address a browser reaches this server on."""
    configured = os.environ.get(_ENV_PUBLIC_URL, "").strip()
    return configured.rstrip("/") if configured else request_base_url.rstrip("/")


def callback_uri(base_url: str) -> str:
    """The redirect URI for a server reachable at ``base_url``.

    Derived from the request unless :data:`_ENV_PUBLIC_URL` says otherwise, so
    a server started on a different port does not silently send Spotify a URI
    it will reject. The value still has to be registered in the Spotify
    dashboard verbatim — :func:`registration_hint` tells the operator which.
    """
    return public_base_url(base_url) + "/auth/callback"


def registration_hint(redirect_uri: str) -> str:
    """What to paste into the Spotify dashboard, and why it might be refused."""
    lines = [f"Redirect URI for this server:  {redirect_uri}"]
    if redirect_uri.startswith("http://") and not redirect_uri.startswith(
        ("http://127.0.0.1", "http://[::1]")
    ):
        lines.append(
            "  WARNING: Spotify only accepts plain http for loopback addresses. "
            "Anything else must be https, so this URI will be rejected."
        )
    if _is_internal(redirect_uri) and not os.environ.get(_ENV_PUBLIC_URL):
        lines.append(
            f"  WARNING: that is an internal address. Set {_ENV_PUBLIC_URL} to the "
            "URL browsers actually use — a proxy rewrites the scheme but not the "
            "host, so the app cannot work the public one out on its own."
        )
    return "\n".join(lines)


def _is_internal(uri: str) -> bool:
    """Whether this address could not be the one a browser actually used.

    Loopback over plain http is the normal local case and must not nag — that
    is how the app is run every day. What is suspicious is loopback over
    *https*, which means a proxy terminated TLS and the app kept its own host,
    and 0.0.0.0, which is a bind address rather than somewhere reachable.
    """
    parsed = urllib.parse.urlparse(uri)
    host = parsed.hostname or ""
    if host in {"0.0.0.0", ""}:
        return True
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    return loopback and parsed.scheme == "https"
