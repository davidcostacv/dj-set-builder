"""Spotify Web API client.

Endpoint surface follows the 2026 API exactly as specified in the build brief:
``/playlists/{id}/items`` (not ``/tracks``), single-entity fetches only (batch
``?ids=`` is gone), ``POST /me/playlists`` (not ``/users/{id}/playlists``), and
no ``audio-features`` / ``recommendations`` / ``related-artists`` anywhere.

If any endpoint here behaves differently from that, ``djset doctor`` will say
so rather than the client silently working around it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from ..models import Artist, PlaylistRef, Track
from ..net import HttpError, RateLimiter, request
from .auth import SpotifyAuth

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"

# Spotify's quota for a development-mode app is small, and the batch endpoints
# are gone — a genre pass is one request per artist across thousands of them.
# Unthrottled, that earns a 429 with a Retry-After measured in hours. Pace the
# calls instead of discovering the ceiling the hard way.
DEFAULT_RATE_PER_HOUR = 6000  # ~100/min, ~1 every 0.6s
_shared_limiter = RateLimiter(DEFAULT_RATE_PER_HOUR)


class SpotifyClient:
    def __init__(self, auth: SpotifyAuth, limiter: RateLimiter | None = None) -> None:
        self.auth = auth
        self.limiter = limiter or _shared_limiter

    # ------------------------------------------------------------------
    def _get(self, path_or_url: str, **params: Any) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{API}{path_or_url}"
        resp = request(
            "GET",
            url,
            headers=self.auth.auth_header(),
            params=params or None,
            on_unauthorized=self.auth.refreshed_auth_header,
            rate_limiter=self.limiter,
        )
        return resp.json()

    def _post(self, path: str, body: Any) -> dict[str, Any]:
        resp = request(
            "POST",
            f"{API}{path}",
            headers={**self.auth.auth_header(), "Content-Type": "application/json"},
            json_body=body,
            on_unauthorized=self.auth.refreshed_auth_header,
        )
        return resp.json() if resp.content else {}

    def _put(self, path: str, body: Any) -> dict[str, Any]:
        resp = request(
            "PUT",
            f"{API}{path}",
            headers={**self.auth.auth_header(), "Content-Type": "application/json"},
            json_body=body,
            on_unauthorized=self.auth.refreshed_auth_header,
        )
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def me(self) -> dict[str, Any]:
        return self._get("/me")

    def playlists(self) -> list[PlaylistRef]:
        out: list[PlaylistRef] = []
        url: str | None = f"{API}/me/playlists"
        params: dict[str, Any] | None = {"limit": 50}
        while url:
            page = self._get(url, **(params or {}))
            for p in page.get("items", []):
                if not p or not p.get("id"):
                    continue
                out.append(
                    PlaylistRef(
                        spotify_id=p["id"],
                        name=p.get("name") or "(untitled)",
                        snapshot_id=p.get("snapshot_id"),
                        track_count=playlist_count(p),
                    )
                )
            url = page.get("next")
            params = None  # the `next` URL already carries the paging params
        return out

    def playlist(self, playlist_id: str) -> dict[str, Any]:
        return self._get(f"/playlists/{playlist_id}")

    def playlist_items(self, playlist_id: str) -> Iterator[tuple[Track, str | None]]:
        """Yield ``(track, added_at)`` for every playable track in a playlist."""
        url: str | None = f"{API}/playlists/{playlist_id}/items"
        params: dict[str, Any] | None = {"limit": 100}
        while url:
            page = self._get(url, **(params or {}))
            for item in page.get("items", []):
                parsed = _parse_item(item)
                if parsed:
                    yield parsed
            url = page.get("next")
            params = None

    def liked_songs(self) -> Iterator[tuple[Track, str | None]]:
        url: str | None = f"{API}/me/tracks"
        params: dict[str, Any] | None = {"limit": 50}
        while url:
            page = self._get(url, **(params or {}))
            for item in page.get("items", []):
                parsed = _parse_item(item)
                if parsed:
                    yield parsed
            url = page.get("next")
            params = None

    def artist(self, artist_id: str) -> Artist | None:
        """Single-entity fetch — the batch ``/artists?ids=`` form was removed."""
        try:
            a = self._get(f"/artists/{artist_id}")
        except HttpError as exc:
            log.warning("artist %s failed: %s", artist_id, exc)
            return None
        return Artist(
            spotify_id=a.get("id") or artist_id,
            name=a.get("name") or "",
            genres=list(a.get("genres") or []),
        )

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def create_playlist(
        self, name: str, description: str = "", public: bool = False
    ) -> dict[str, Any]:
        return self._post(
            "/me/playlists",
            {"name": name, "description": description[:300], "public": public},
        )

    def add_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        return self._post(f"/playlists/{playlist_id}/items", {"uris": uris})

    def replace_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        return self._put(f"/playlists/{playlist_id}/items", {"uris": uris})

    def rename_playlist(
        self, playlist_id: str, name: str, description: str | None = None
    ) -> None:
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description[:300]
        self._put(f"/playlists/{playlist_id}", body)

    def unfollow_playlist(self, playlist_id: str) -> None:
        """Remove a playlist from the library — used to clean up a failed export."""
        request(
            "DELETE",
            f"{API}/me/library",
            headers={**self.auth.auth_header(), "Content-Type": "application/json"},
            json_body={"uris": [f"spotify:playlist:{playlist_id}"]},
            on_unauthorized=self.auth.refreshed_auth_header,
        )


def playlist_count(p: dict[str, Any]) -> int:
    """Track count off a playlist object.

    The count object was renamed ``tracks`` -> ``items`` alongside the
    ``/tracks`` -> ``/items`` endpoint rename; the build brief documents the
    endpoint but not the field. ``tracks`` is kept as a fallback so this keeps
    working if the rename is ever reverted.
    """
    for key in ("items", "tracks"):
        obj = p.get(key)
        if isinstance(obj, dict) and isinstance(obj.get("total"), int):
            return obj["total"]
    return 0


def _parse_item(item: dict[str, Any] | None) -> tuple[Track, str | None] | None:
    """Convert one playlist/library item into a Track.

    Skips podcast episodes, unavailable entries, and local files (no id, and
    their URIs cannot be added to a playlist by anyone but the owner's client).
    """
    if not item:
        return None
    # The wrapper key differs by endpoint: /me/tracks still nests under
    # "track", while /playlists/{id}/items nests under "item".
    t = item.get("track") or item.get("item")
    if not t or t.get("type") not in (None, "track"):
        return None
    tid = t.get("id")
    uri = t.get("uri")
    if not tid or not uri or t.get("is_local"):
        return None

    artists = t.get("artists") or []
    names = [a["name"] for a in artists if a.get("name")]
    return (
        Track(
            spotify_id=tid,
            uri=uri,
            title=t.get("name") or "",
            artist=", ".join(names),
            artist_ids=[a["id"] for a in artists if a.get("id")],
            artist_names=names,
            isrc=((t.get("external_ids") or {}).get("isrc")),
            album=(t.get("album") or {}).get("name"),
            duration_ms=t.get("duration_ms"),
            added_at=item.get("added_at"),
        ),
        item.get("added_at"),
    )
