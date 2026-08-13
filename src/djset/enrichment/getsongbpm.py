"""GetSongBPM feature source (priority 20).

GetSongBPM has no Spotify or ISRC index, so every lookup is a fuzzy
artist+title search. Matching quality is entirely down to
:mod:`djset.enrichment.normalize`.

Attribution: GetSongBPM's terms require a visible backlink to
https://getsongbpm.com in the app. See :data:`ATTRIBUTION_URL`; it is rendered
in the About pane and printed by ``djset about``. Do not remove it — they
suspend accounts without notice.
"""

from __future__ import annotations

import logging
from typing import Any

from ..camelot import open_key_to_camelot, to_camelot
from ..models import AudioFeatures, Track
from ..net import HttpError, RateLimiter, request
from .normalize import (
    artist_variants,
    normalize_artist,
    normalize_title,
    similarity,
    title_variants,
)

log = logging.getLogger(__name__)

BASE = "https://api.getsong.co"
ATTRIBUTION_URL = "https://getsongbpm.com"
ATTRIBUTION_TEXT = "BPM and key data by GetSongBPM - https://getsongbpm.com"

# Reject a search hit whose artist/title do not really match ours.
MIN_TITLE_SIM = 0.6
MIN_ARTIST_SIM = 0.5


class GetSongBPMSource:
    name = "getsongbpm"
    priority = 20

    def __init__(self, api_key: str, rate_per_hour: int = 2400) -> None:
        self.api_key = api_key
        self.limiter = RateLimiter(rate_per_hour)

    # ------------------------------------------------------------------
    def lookup(self, track: Track) -> AudioFeatures | None:
        for artist in artist_variants(track.artist):
            for title in title_variants(track.title):
                hit = self._search_one(artist, title, track)
                if hit is None:
                    continue
                features = self._to_features(track, hit)
                if features is not None:
                    return features
        return None

    # ------------------------------------------------------------------
    def _get(self, path: str, **params: Any) -> dict[str, Any] | None:
        try:
            resp = request(
                "GET",
                f"{BASE}{path}",
                params={"api_key": self.api_key, **params},
                rate_limiter=self.limiter,
            )
        except HttpError as exc:
            log.warning("getsongbpm %s failed: %s", path, exc)
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("getsongbpm %s returned non-JSON", path)
            return None

    def _search_one(
        self, artist: str, title: str, track: Track
    ) -> dict[str, Any] | None:
        payload = self._get(
            "/search/", type="both", lookup=f"song:{title} artist:{artist}"
        )
        if not payload:
            return None

        results = _extract_results(payload)
        if not results:
            return None

        want_title = normalize_title(track.title)
        want_artist = normalize_artist(track.artist)

        best: tuple[float, dict[str, Any]] | None = None
        for r in results:
            if not isinstance(r, dict):
                continue
            r_title = normalize_title(r.get("title") or "")
            r_artist = normalize_artist((r.get("artist") or {}).get("name") or "")
            t_sim = similarity(want_title, r_title)
            a_sim = similarity(want_artist, r_artist)
            if t_sim < MIN_TITLE_SIM or a_sim < MIN_ARTIST_SIM:
                continue
            score = t_sim + a_sim
            if best is None or score > best[0]:
                best = (score, r)

        return best[1] if best else None

    # ------------------------------------------------------------------
    def _to_features(
        self, track: Track, hit: dict[str, Any]
    ) -> AudioFeatures | None:
        detail = hit
        if not _has_usable_fields(hit) and hit.get("id"):
            payload = self._get("/song/", id=hit["id"])
            song = (payload or {}).get("song")
            if isinstance(song, dict):
                detail = {**hit, **song}

        bpm = _as_float(detail.get("tempo"))
        camelot = to_camelot(detail.get("key_of")) or open_key_to_camelot(
            detail.get("open_key")
        )
        if bpm is None and camelot is None:
            return None

        return AudioFeatures(
            spotify_id=track.spotify_id,
            bpm=bpm,
            key_camelot=camelot,
            key_open=str(detail.get("open_key")) if detail.get("open_key") else None,
            energy=_scale_0_1(detail.get("danceability")),
            source=self.name,
            confidence=0.7,
        )

    # ------------------------------------------------------------------
    def artist_genres(self, hit: dict[str, Any]) -> list[str]:
        """GetSongBPM also carries artist genres; kept for a possible future
        fallback when Spotify's artist tags are empty. Not wired in yet."""
        artist = hit.get("artist") or {}
        genres = artist.get("genres")
        if isinstance(genres, list):
            return [str(g).strip().lower() for g in genres if g]
        return []


_warned_shapes: set[str] = set()


def _extract_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the result rows out of a ``/search/`` response.

    The documented ``type=artist`` form returns ``{"search": [ ... ]}`` and a
    miss returns ``{"search": {"error": "no result"}}``. The shape of
    ``type=both`` is not documented, so a dict keyed by ``song``/``artist`` is
    also accepted. Any other shape is logged once so it surfaces instead of
    silently reading as "no match" for every track in the library.
    """
    results = payload.get("search")

    if isinstance(results, list):
        return [r for r in results if isinstance(r, dict)]

    if isinstance(results, dict):
        if "error" in results:
            return []
        for key in ("song", "songs", "both"):
            nested = results.get(key)
            if isinstance(nested, list):
                return [r for r in nested if isinstance(r, dict)]
        # A single result object rather than a list of one.
        if "id" in results or "title" in results:
            return [results]
        shape = f"dict:{sorted(results)[:6]}"
        if shape not in _warned_shapes:
            _warned_shapes.add(shape)
            log.warning("Unrecognised getsongbpm search shape %s", shape)
        return []

    if results is not None:
        shape = type(results).__name__
        if shape not in _warned_shapes:
            _warned_shapes.add(shape)
            log.warning("Unexpected getsongbpm search payload type: %s", shape)
    return []


def _has_usable_fields(d: dict[str, Any]) -> bool:
    return bool(d.get("tempo")) and bool(d.get("key_of") or d.get("open_key"))


def _as_float(v: Any) -> float | None:
    if v in (None, "", "-"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _scale_0_1(v: Any) -> float | None:
    """GetSongBPM returns danceability/acousticness as 0-100; the schema wants 0-1."""
    f = _as_float(v)
    if f is None:
        return None
    return max(0.0, min(1.0, f / 100.0 if f > 1.0 else f))
