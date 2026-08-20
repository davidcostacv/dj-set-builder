"""Authorization Code + PKCE against a loopback redirect.

The refresh token is stored in the OS keyring (Windows Credential Manager /
macOS Keychain / Secret Service), which is encrypted at rest by the OS. No
client secret exists — PKCE does not use one — and nothing secret is written to
SQLite or the log.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import keyring
from keyring.errors import KeyringError

from ..config import APP_NAME, Config
from ..net import HttpError, request

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

_KEYRING_SERVICE = f"{APP_NAME}-spotify"
_KEYRING_USER = "refresh_token"

# Refresh this many seconds before the token actually expires.
_EXPIRY_SKEW = 60


class AuthError(RuntimeError):
    pass


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        type(self).result = {k: v[0] for k, v in params.items()}

        ok = "code" in type(self).result
        body = (
            "<h2>Authorized.</h2><p>You can close this tab and return to the app.</p>"
            if ok
            else f"<h2>Authorization failed.</h2><pre>{type(self).result}</pre>"
        )
        payload = f"<html><body style='font-family:sans-serif'>{body}</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def log_message(self, *args: object) -> None:
        pass  # keep the loopback server out of stdout


def _run_callback_server(host: str, port: int, timeout: float) -> dict[str, str]:
    _CallbackHandler.result = {}
    server = HTTPServer((host, port), _CallbackHandler)
    server.timeout = 1.0
    thread = threading.Thread(target=_serve_until_result, args=(server, timeout))
    thread.start()
    thread.join(timeout + 5)
    server.server_close()
    return _CallbackHandler.result


def _serve_until_result(server: HTTPServer, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _CallbackHandler.result:
        server.handle_request()


class TokenStore:
    """Refresh token in the OS keyring; access token in memory only."""

    def __init__(self) -> None:
        self._access: str | None = None
        self._expires_at: float = 0.0

    # -- refresh token (persisted) --
    def load_refresh(self) -> str | None:
        try:
            return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        except KeyringError as exc:
            raise AuthError(
                f"No usable OS keyring backend: {exc}\n"
                "The refresh token must be stored encrypted at rest, so the app "
                "will not fall back to a plaintext file."
            ) from exc

    def save_refresh(self, token: str) -> None:
        try:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, token)
        except KeyringError as exc:
            raise AuthError(f"Could not write to the OS keyring: {exc}") from exc

    def clear(self) -> None:
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except Exception:
            pass
        self._access = None
        self._expires_at = 0.0

    # -- access token (memory) --
    def set_access(self, token: str, expires_in: int) -> None:
        self._access = token
        self._expires_at = time.monotonic() + expires_in - _EXPIRY_SKEW

    @property
    def access(self) -> str | None:
        if self._access and time.monotonic() < self._expires_at:
            return self._access
        return None


class SpotifyAuth:
    def __init__(self, config: Config, store: TokenStore | None = None) -> None:
        self.config = config
        self.store = store or TokenStore()

    # ------------------------------------------------------------------
    def has_saved_login(self) -> bool:
        return bool(self.store.load_refresh())

    def token(self) -> str:
        """Current access token, refreshing or prompting for login as needed."""
        cached = self.store.access
        if cached:
            return cached
        refresh = self.store.load_refresh()
        if refresh:
            return self._refresh(refresh)
        return self.login()

    def force_refresh(self) -> str:
        refresh = self.store.load_refresh()
        if not refresh:
            return self.login()
        return self._refresh(refresh)

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def refreshed_auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.force_refresh()}"}

    # ------------------------------------------------------------------
    def authorize_url(self, redirect_uri: str | None = None) -> tuple[str, str, str]:
        """``(url, verifier, state)`` — the first half of the PKCE flow.

        Split out of :meth:`login` so a web server can serve the two halves as
        separate requests. The desktop flow runs a throwaway HTTP server and
        does both in one call; a web app redirects the browser and gets the
        callback on its own route, which is the only shape that works once the
        redirect URI is not loopback.

        The verifier is returned rather than stored: the caller decides where
        it lives for the seconds between the two halves, and it must never
        leave the server that will exchange it.
        """
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)
        params = {
            "client_id": self.config.spotify_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri or self.config.spotify_redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
            "scope": self.config.scopes,
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", verifier, state

    def exchange_code(
        self, code: str, verifier: str, redirect_uri: str | None = None
    ) -> str:
        """The second half: swap the authorization code for tokens.

        ``redirect_uri`` is sent again because Spotify checks it matches the
        one the code was issued against; it is not used to redirect anything.
        """
        try:
            resp = request(
                "POST",
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri or self.config.spotify_redirect_uri,
                    "client_id": self.config.spotify_client_id,
                    "code_verifier": verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except HttpError as exc:
            # Never echo the code or the verifier — this lands in logs.
            raise AuthError(
                f"Spotify rejected the authorization code (HTTP {exc.status}). "
                "The most common cause is a redirect URI that does not exactly "
                "match the one registered in the Spotify dashboard."
            ) from exc
        payload = resp.json()
        self.store.set_access(
            payload["access_token"], int(payload.get("expires_in", 3600))
        )
        if payload.get("refresh_token"):
            self.store.save_refresh(payload["refresh_token"])
        return payload["access_token"]

    def login(self, *, timeout: float = 180.0) -> str:
        """Run the interactive PKCE flow. Opens a browser, waits for callback."""
        parsed = urllib.parse.urlparse(self.config.spotify_redirect_uri)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8888

        url, verifier, state = self.authorize_url()

        log.info("Opening browser for Spotify authorization…")
        log.info("If it does not open, paste this into your browser:\n%s", url)
        webbrowser.open(url)

        result = _run_callback_server(host, port, timeout)
        if not result:
            raise AuthError("Timed out waiting for the Spotify callback.")
        if "error" in result:
            raise AuthError(f"Spotify returned an error: {result['error']}")
        if result.get("state") != state:
            raise AuthError("State mismatch on the callback — aborting.")
        code = result.get("code")
        if not code:
            raise AuthError(f"No authorization code in callback: {result}")

        token = self.exchange_code(code, verifier)
        log.info("Authorized. Refresh token saved to the OS keyring.")
        return token

    # ------------------------------------------------------------------
    def _refresh(self, refresh_token: str) -> str:
        """Exchange the saved refresh token for an access token.

        A rejected refresh token means re-authorizing. A network failure does
        NOT — falling back to an interactive login there would open a browser
        and block forever in a background or headless run, which is exactly
        what happened when a TLS error hit mid-sync.
        """
        try:
            resp = request(
                "POST",
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.config.spotify_client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except HttpError as exc:
            if exc.status in (400, 401):
                log.warning("Saved refresh token was rejected — re-authorizing.")
                return self.login()
            raise AuthError(
                f"Token refresh failed with HTTP {exc.status}. "
                "The saved login is still intact; retry when the API is reachable."
            ) from exc
        except Exception as exc:
            raise AuthError(
                f"Could not reach {TOKEN_URL}: {exc}\n"
                "This is a network/TLS problem, not an authorization problem — "
                "the saved login is still intact. Retry once connectivity is back."
            ) from exc

        payload = resp.json()
        self.store.set_access(payload["access_token"], int(payload.get("expires_in", 3600)))
        # Spotify rotates refresh tokens; persist the new one when present.
        if payload.get("refresh_token"):
            self.store.save_refresh(payload["refresh_token"])
        return payload["access_token"]
