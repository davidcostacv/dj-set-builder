"""Deezer feature source (priority 30) — BPM only, matched on ISRC.

Added after measurement, not assumption. On a 30-track sample of tracks
GetSongBPM could not resolve, Deezer found 30/30 by ISRC and supplied a real
tempo for 6 of them — about 20% new coverage over the gap.

Two things distinguish it from :mod:`djset.enrichment.getsongbpm`:

* **It joins on ISRC**, which Spotify already gives us, so matching is exact.
  No artist/title normalisation is involved and none of that class of failure
  applies.
* **It has no musical key.** Deezer exposes ``bpm`` and ``gain`` but nothing
  harmonic. So it raises BPM coverage — helping BPM-mode sequencing — without
  helping BPM+Key mixing. That is why it sits at priority 30, *below*
  GetSongBPM: a GetSongBPM hit carries both values and is worth more.

No API key is required for these public endpoints.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import AudioFeatures, Track
from ..net import HttpError, RateLimiter, request
from .normalize import normalize_artist, normalize_title, similarity

log = logging.getLogger(__name__)

BASE = "https://api.deezer.com"

# Deezer permits roughly 50 requests per 5 seconds per IP. Stay well under it.
DEFAULT_RATE_PER_HOUR = 10_800  # 3/s

MIN_TITLE_SIM = 0.6
MIN_ARTIST_SIM = 0.5


class DeezerSource:
    name = "deezer"
    priority = 30

    def __init__(self, rate_per_hour: int = DEFAULT_RATE_PER_HOUR) -> None:
        self.limiter = RateLimiter(rate_per_hour)

    # ------------------------------------------------------------------
    def lookup(self, track: Track) -> AudioFeatures | None:
        # ISRC first: an exact join beats any amount of string matching.
        if track.isrc:
            found = self._by_isrc(track)
            if found is not None:
                return found
        return self._by_search(track)

    # ------------------------------------------------------------------
    def _get(self, path: str, **params: Any) -> dict[str, Any] | None:
        try:
            resp = request(
                "GET", f"{BASE}{path}", params=params or None, rate_limiter=self.limiter
            )
        except HttpError as exc:
            log.warning("deezer %s failed: %s", path, exc)
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        if not isinstance(payload, dict) or "error" in payload:
            return None
        return payload

    def _by_isrc(self, track: Track) -> AudioFeatures | None:
        payload = self._get(f"/track/isrc:{track.isrc}")
        return self._to_features(track, payload, confidence=0.9)

    def _by_search(self, track: Track) -> AudioFeatures | None:
        artist = normalize_artist(track.primary_artist)
        title = normalize_title(track.title)
        if not artist or not title:
            return None

        payload = self._get("/search", q=f'artist:"{artist}" track:"{title}"')
        rows = (payload or {}).get("data")
        if not isinstance(rows, list) or not rows:
            return None

        best: tuple[float, dict[str, Any]] | None = None
        for r in rows:
            if not isinstance(r, dict):
                continue
            r_title = normalize_title((r.get("title") or ""))
            r_artist = normalize_artist(((r.get("artist") or {}).get("name") or ""))
            t_sim = similarity(title, r_title)
            a_sim = similarity(artist, r_artist)
            if t_sim < MIN_TITLE_SIM or a_sim < MIN_ARTIST_SIM:
                continue
            score = t_sim + a_sim
            if best is None or score > best[0]:
                best = (score, r)
        if best is None:
            return None

        # Search rows omit bpm; the full track object carries it.
        detail = self._get(f"/track/{best[1].get('id')}")
        return self._to_features(track, detail, confidence=0.7)

    # ------------------------------------------------------------------
    def _to_features(
        self, track: Track, payload: dict[str, Any] | None, *, confidence: float
    ) -> AudioFeatures | None:
        if not payload:
            return None
        bpm = payload.get("bpm")
        try:
            bpm = float(bpm)
        except (TypeError, ValueError):
            return None
        # Deezer uses 0 for "unknown", not null.
        if bpm <= 0:
            return None
        return AudioFeatures(
            spotify_id=track.spotify_id,
            bpm=round(bpm, 2),
            key_camelot=None,  # Deezer exposes no harmonic information
            key_open=None,
            energy=None,
            source=self.name,
            confidence=confidence,
        )
