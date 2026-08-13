"""Token refresh behaviour.

Regression: a TLS error during a background sync made `_refresh` fall back to
an interactive browser login, which then blocked until the callback timed out
and killed the run. A network failure and a rejected token are different
things and must be handled differently.
"""

from __future__ import annotations

import httpx
import pytest

from djset.config import Config
from djset.net import HttpError
from djset.spotify import auth as auth_mod
from djset.spotify.auth import AuthError, SpotifyAuth, TokenStore


class MemoryStore(TokenStore):
    """TokenStore that never touches the real OS keyring."""

    def __init__(self, refresh: str | None = "saved-refresh") -> None:
        super().__init__()
        self._refresh = refresh

    def load_refresh(self) -> str | None:
        return self._refresh

    def save_refresh(self, token: str) -> None:
        self._refresh = token

    def clear(self) -> None:
        self._refresh = None


@pytest.fixture()
def cfg() -> Config:
    return Config(
        spotify_client_id="cid",
        spotify_redirect_uri="http://127.0.0.1:8888/callback",
        getsongbpm_api_key=None,
        getsongbpm_rate_per_hour=2400,
    )


@pytest.fixture()
def no_login(monkeypatch):
    """Make any interactive login attempt an loud failure instead of a browser."""
    called: list[bool] = []

    def boom(self, **kwargs):
        called.append(True)
        raise AssertionError("interactive login must not be triggered here")

    monkeypatch.setattr(SpotifyAuth, "login", boom)
    return called


def test_transport_error_raises_and_never_opens_a_browser(cfg, no_login, monkeypatch):
    def explode(*a, **k):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] cert verify failed")

    monkeypatch.setattr(auth_mod, "request", explode)
    a = SpotifyAuth(cfg, MemoryStore())

    with pytest.raises(AuthError) as exc:
        a.token()

    assert "network/TLS problem" in str(exc.value)
    # The saved login must survive a network blip.
    assert a.store.load_refresh() == "saved-refresh"


def test_server_error_raises_rather_than_re_authorizing(cfg, no_login, monkeypatch):
    def explode(*a, **k):
        raise HttpError(503, auth_mod.TOKEN_URL, "Service Unavailable")

    monkeypatch.setattr(auth_mod, "request", explode)

    with pytest.raises(AuthError) as exc:
        SpotifyAuth(cfg, MemoryStore()).token()
    assert "503" in str(exc.value)


@pytest.mark.parametrize("status", [400, 401])
def test_a_rejected_refresh_token_does_re_authorize(cfg, monkeypatch, status):
    """This is the one case where opening a browser is correct."""

    def explode(*a, **k):
        raise HttpError(status, auth_mod.TOKEN_URL, '{"error":"invalid_grant"}')

    monkeypatch.setattr(auth_mod, "request", explode)
    monkeypatch.setattr(SpotifyAuth, "login", lambda self, **kw: "fresh-access-token")

    assert SpotifyAuth(cfg, MemoryStore()).token() == "fresh-access-token"


def test_successful_refresh_caches_the_access_token(cfg, monkeypatch):
    calls: list[int] = []

    def ok(*a, **k):
        calls.append(1)
        return httpx.Response(
            200, json={"access_token": "at-1", "expires_in": 3600}
        )

    monkeypatch.setattr(auth_mod, "request", ok)
    a = SpotifyAuth(cfg, MemoryStore())

    assert a.token() == "at-1"
    assert a.token() == "at-1"
    assert len(calls) == 1  # second call served from memory, no network


def test_a_rotated_refresh_token_is_persisted(cfg, monkeypatch):
    monkeypatch.setattr(
        auth_mod,
        "request",
        lambda *a, **k: httpx.Response(
            200,
            json={"access_token": "at", "expires_in": 3600, "refresh_token": "rotated"},
        ),
    )
    store = MemoryStore()
    SpotifyAuth(cfg, store).token()
    assert store.load_refresh() == "rotated"


def test_no_saved_token_goes_straight_to_login(cfg, monkeypatch):
    monkeypatch.setattr(SpotifyAuth, "login", lambda self, **kw: "first-token")
    assert SpotifyAuth(cfg, MemoryStore(refresh=None)).token() == "first-token"
