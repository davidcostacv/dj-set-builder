"""Web authorization — step W5.

The desktop flow spins up a throwaway server on 127.0.0.1:8888 and does both
halves of PKCE in one call. That works only while the app and the browser are
on the same machine. Here the app is the server and the callback is one of its
own routes, which is the shape that keeps working once the redirect URI is not
loopback.

Two things must survive between the redirect out and the callback back: the
PKCE verifier, which proves this app started the flow and must never reach the
browser, and the state, which proves the callback belongs to a flow this app
started.
"""

from __future__ import annotations

import time
import urllib.parse as urlparse

import pytest

pytest.importorskip("fastapi")

from djset.web.oauth import (  # noqa: E402
    MAX_PENDING,
    PENDING_TTL_S,
    PendingFlows,
    callback_uri,
    registration_hint,
)


# ---------------------------------------------------------------------------
# the pending store
# ---------------------------------------------------------------------------


def test_a_started_flow_can_be_claimed_once():
    flows = PendingFlows()
    flows.start("st", "verifier-1", "http://x/cb")

    first = flows.claim("st")
    assert first is not None
    assert first.verifier == "verifier-1"
    assert flows.claim("st") is None      # single use


def test_an_unknown_state_is_refused():
    assert PendingFlows().claim("never-issued") is None


def test_expiry_and_a_wrong_state_are_indistinguishable():
    """Saying which would help someone guessing at state values."""
    flows = PendingFlows()
    flows.start("st", "v", "http://x/cb")
    flows._flows["st"] = flows._flows["st"].__class__(
        "v", "http://x/cb", time.time() - PENDING_TTL_S - 1
    )
    assert flows.claim("st") is None
    assert flows.claim("some-other-state") is None


def test_the_store_is_capped_so_a_caller_cannot_grow_it():
    """Every /auth/login mints an entry, so without a cap anyone could grow
    this without limit simply by hitting that route."""
    flows = PendingFlows()
    for i in range(MAX_PENDING + 20):
        flows.start(f"s{i}", f"v{i}", "http://x/cb")

    assert len(flows) == MAX_PENDING


def test_the_cap_evicts_the_oldest_not_the_newest():
    """A stale flow nobody completed must not lock out someone trying now."""
    flows = PendingFlows()
    for i in range(MAX_PENDING + 5):
        flows.start(f"s{i}", f"v{i}", "http://x/cb")

    assert flows.claim("s0") is None                       # oldest gone
    assert flows.claim(f"s{MAX_PENDING + 4}") is not None   # newest kept


def test_expired_flows_are_pruned_rather_than_accumulating():
    flows = PendingFlows()
    flows.start("old", "v", "http://x/cb")
    flows._flows["old"] = flows._flows["old"].__class__(
        "v", "http://x/cb", time.time() - PENDING_TTL_S - 1
    )
    flows.start("new", "v2", "http://x/cb")
    assert len(flows) == 1


# ---------------------------------------------------------------------------
# the redirect URI
# ---------------------------------------------------------------------------


def test_the_callback_uri_is_derived_from_the_server_it_runs_on():
    """Configured rather than derived, a server started on a different port
    would silently send Spotify a URI it will reject."""
    assert callback_uri("http://127.0.0.1:8000/") == "http://127.0.0.1:8000/auth/callback"
    assert callback_uri("http://127.0.0.1:8000") == "http://127.0.0.1:8000/auth/callback"
    assert callback_uri("https://djset.example/") == "https://djset.example/auth/callback"


def test_plain_http_off_loopback_is_flagged_before_spotify_refuses_it():
    """Spotify accepts plain http only for loopback. Finding that out from its
    error message is much worse than being told here."""
    warned = registration_hint("http://djset.example/auth/callback")
    assert "WARNING" in warned and "https" in warned

    for fine in (
        "http://127.0.0.1:8000/auth/callback",
        "https://djset.example/auth/callback",
    ):
        assert "WARNING" not in registration_hint(fine)


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DJSET_DB_PATH", str(tmp_path / "oauth.sqlite3"))
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GETSONGBPM_API_KEY", "test-key")
    from djset import config

    monkeypatch.setattr(config, "_resolved", None)

    from fastapi.testclient import TestClient

    from djset.web import app as web_app
    from djset.web import oauth

    oauth.pending = oauth.PendingFlows()      # a clean store per test
    monkeypatch.setattr(web_app, "pending", oauth.pending)
    web_app.library.loaded = False
    return TestClient(web_app.app, follow_redirects=False)


def test_login_redirects_to_spotify_with_pkce(client):
    r = client.get("/auth/login")
    assert r.status_code == 307

    q = urlparse.parse_qs(urlparse.urlparse(r.headers["location"]).query)
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] and q["state"]
    assert q["redirect_uri"][0].endswith("/auth/callback")


def test_the_verifier_never_reaches_the_browser(client):
    """It is the whole point of PKCE: only the server that started the flow can
    finish it."""
    r = client.get("/auth/login")
    location = r.headers["location"]
    q = urlparse.parse_qs(urlparse.urlparse(location).query)

    assert "code_verifier" not in q
    from djset.web import oauth

    verifier = next(iter(oauth.pending._flows.values())).verifier
    assert verifier not in location


def test_each_login_mints_a_fresh_state(client):
    states = set()
    for _ in range(3):
        r = client.get("/auth/login")
        q = urlparse.parse_qs(urlparse.urlparse(r.headers["location"]).query)
        states.add(q["state"][0])
    assert len(states) == 3


@pytest.mark.parametrize(
    "query,expected",
    [
        ("", "missing_code"),
        ("?code=abc", "missing_code"),
        ("?state=abc", "missing_code"),
        ("?error=access_denied", "access_denied"),
        ("?code=abc&state=never-issued", "stale_or_unknown_state"),
    ],
)
def test_every_callback_failure_redirects_with_a_reason(client, query, expected):
    """Never render an error body here: this URL is in the address bar, and
    leaving someone on a dead end containing an authorization code is worse
    than sending them back to something they can retry from."""
    r = client.get(f"/auth/callback{query}")
    assert r.status_code == 303
    assert expected in r.headers["location"]


def test_a_used_state_cannot_be_replayed(client, monkeypatch):
    from djset.web import app as web_app

    r = client.get("/auth/login")
    state = urlparse.parse_qs(urlparse.urlparse(r.headers["location"]).query)["state"][0]

    exchanged = []

    class FakeAuth:
        def exchange_code(self, code, verifier, redirect_uri):
            exchanged.append((code, verifier))
            return "token"

    monkeypatch.setattr(web_app, "_auth", lambda: FakeAuth())

    first = client.get(f"/auth/callback?code=real-code&state={state}")
    assert "authorized=1" in first.headers["location"]
    assert exchanged == [("real-code", exchanged[0][1])]

    replay = client.get(f"/auth/callback?code=real-code&state={state}")
    assert "stale_or_unknown_state" in replay.headers["location"]
    assert len(exchanged) == 1          # the second never reached Spotify


def test_a_failed_exchange_still_consumes_the_state(client, monkeypatch):
    """A state is spent when it is presented, not when it succeeds — otherwise
    a failed attempt leaves a live state behind to be replayed."""
    from djset.web import app as web_app

    r = client.get("/auth/login")
    state = urlparse.parse_qs(urlparse.urlparse(r.headers["location"]).query)["state"][0]

    class Exploding:
        def exchange_code(self, *a, **kw):
            raise RuntimeError("spotify said no")

    monkeypatch.setattr(web_app, "_auth", lambda: Exploding())

    first = client.get(f"/auth/callback?code=x&state={state}")
    assert "exchange_failed" in first.headers["location"]

    again = client.get(f"/auth/callback?code=x&state={state}")
    assert "stale_or_unknown_state" in again.headers["location"]


def test_status_reports_not_authorized_without_a_saved_login(client, monkeypatch):
    from djset.web import app as web_app

    class NoLogin:
        def has_saved_login(self):
            return False

    monkeypatch.setattr(web_app, "_auth", lambda: NoLogin())
    body = client.get("/api/auth/status").json()
    assert body == {"configured": True, "authorized": False, "user": None}


def test_a_saved_but_broken_login_is_not_reported_as_signed_in(client, monkeypatch):
    """Otherwise the page shows a logged-in state that fails on the first real
    request."""
    from djset.web import app as web_app

    class Stale:
        def has_saved_login(self):
            return True

    monkeypatch.setattr(web_app, "_auth", lambda: Stale())
    monkeypatch.setattr(
        web_app, "SpotifyClient",
        lambda auth: (_ for _ in ()).throw(RuntimeError("token rejected")),
    )
    body = client.get("/api/auth/status").json()
    assert body["authorized"] is False
    assert "not usable" in body["detail"]


# ---------------------------------------------------------------------------
# hosted: the redirect URI a proxy leaves behind — step W6
# ---------------------------------------------------------------------------
#
# Uvicorn's proxy handling rewrites the scheme from X-Forwarded-Proto but not
# the host. Driven at a running server with --proxy-headers, a request carrying
# `X-Forwarded-Proto: https, X-Forwarded-Host: djset.example.com` produced
# `https://127.0.0.1:8780/auth/callback` — right scheme, wrong host, and
# Spotify refuses it without explaining why.


def test_an_explicit_public_url_wins_over_what_the_request_says(monkeypatch):
    from djset.web import oauth

    monkeypatch.setenv("DJSET_PUBLIC_URL", "https://djset.example.com")
    assert oauth.callback_uri("http://0.0.0.0:8000/") == (
        "https://djset.example.com/auth/callback"
    )


def test_a_trailing_slash_on_the_public_url_does_not_double_up(monkeypatch):
    from djset.web import oauth

    monkeypatch.setenv("DJSET_PUBLIC_URL", "https://djset.example.com/")
    assert oauth.callback_uri("http://x/") == "https://djset.example.com/auth/callback"


def test_without_an_override_it_still_derives_from_the_request(monkeypatch):
    from djset.web import oauth

    monkeypatch.delenv("DJSET_PUBLIC_URL", raising=False)
    assert oauth.callback_uri("http://127.0.0.1:9000/") == (
        "http://127.0.0.1:9000/auth/callback"
    )


def test_a_bind_address_is_flagged(monkeypatch):
    """0.0.0.0 is somewhere to listen, not somewhere to reach."""
    from djset.web import oauth

    monkeypatch.delenv("DJSET_PUBLIC_URL", raising=False)
    assert "internal address" in oauth.registration_hint(
        "https://0.0.0.0:8000/auth/callback"
    )


def test_loopback_over_https_is_flagged_as_a_proxy_that_kept_our_host(monkeypatch):
    from djset.web import oauth

    monkeypatch.delenv("DJSET_PUBLIC_URL", raising=False)
    assert "internal address" in oauth.registration_hint(
        "https://127.0.0.1:8000/auth/callback"
    )


def test_ordinary_local_use_is_not_nagged(monkeypatch):
    """Plain http on loopback is how this is run every day."""
    from djset.web import oauth

    monkeypatch.delenv("DJSET_PUBLIC_URL", raising=False)
    assert "WARNING" not in oauth.registration_hint(
        "http://127.0.0.1:8000/auth/callback"
    )


def test_a_configured_deployment_is_not_nagged(monkeypatch):
    from djset.web import oauth

    monkeypatch.setenv("DJSET_PUBLIC_URL", "https://djset.example.com")
    assert "WARNING" not in oauth.registration_hint(
        oauth.callback_uri("http://0.0.0.0:8000/")
    )
