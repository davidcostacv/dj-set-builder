"""AcousticBrainz feature source (priority 25) — BPM *and* key, joined on ISRC.

AcousticBrainz stopped accepting submissions in 2022, but the accumulated
dataset is still served, and it is the only free source found so far that
supplies harmonic information without fuzzy string matching.

Measured before it was built: on 25 tracks with no key at all, MusicBrainz
resolved 19 ISRCs to recording MBIDs and AcousticBrainz had a full BPM+key
analysis for 9 of them.

Two hops are unavoidable, because AcousticBrainz is keyed by MusicBrainz
recording MBID and knows nothing about ISRCs:

1. ``musicbrainz.org/ws/2/isrc/{isrc}``  -> recording MBIDs
2. ``acousticbrainz.org/api/v1/{mbid}/low-level`` -> Essentia analysis

Where it sits in the chain, and why:

* **Below GetSongBPM (20).** GetSongBPM's values are human-curated; these are
  machine estimates. When both have data, prefer the human.
* **Above Deezer (30).** Deezer carries no key at all.

The values are estimates, so the key is only trusted when Essentia's own
``key_strength`` clears :data:`MIN_KEY_STRENGTH`. Below that the tempo is still
returned and the key is dropped — a missing key costs one track's eligibility,
whereas a wrong key silently corrupts every transition it takes part in.

The pass is slow by construction: MusicBrainz enforces one request per second
per client and blocks clients that ignore it. That ceiling is not something to
engineer around, so a full library run is measured in hours. Enrichment
checkpoints after every track, so it resumes rather than restarts.
"""

from __future__ import annotations

import logging
from typing import Any

from ..camelot import to_camelot
from ..models import AudioFeatures, Track
from ..net import HttpError, RateLimiter, request

log = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
AB_BASE = "https://acousticbrainz.org/api/v1"

# MusicBrainz: 1 req/s, enforced. Sit just under it.
MB_RATE_PER_HOUR = 3_400
AB_RATE_PER_HOUR = 10_800  # 3/s; AcousticBrainz publishes no hard limit

# MusicBrainz requires a descriptive User-Agent with a contact address, and
# rejects generic ones. The repository URL is the contact.
USER_AGENT = "dj-set-builder/0.1 ( https://github.com/davidcostacv/dj-set-builder )"

# Essentia's self-reported confidence in the key estimate, 0..1.
MIN_KEY_STRENGTH = 0.5

# Upper bound of Essentia's danceability, used only to fit the 0..1 column.
DANCEABILITY_MAX = 3.0

# One ISRC can map to several recording MBIDs (the same recording appearing on
# multiple releases). Only a few are worth trying before giving up.
MAX_MBIDS = 3


class AcousticBrainzSource:
    name = "acousticbrainz"
    priority = 25

    def __init__(
        self,
        mb_rate_per_hour: int = MB_RATE_PER_HOUR,
        ab_rate_per_hour: int = AB_RATE_PER_HOUR,
        min_key_strength: float = MIN_KEY_STRENGTH,
    ) -> None:
        self.mb_limiter = RateLimiter(mb_rate_per_hour)
        self.ab_limiter = RateLimiter(ab_rate_per_hour)
        self.min_key_strength = min_key_strength

    # ------------------------------------------------------------------
    def lookup(self, track: Track) -> AudioFeatures | None:
        # ISRC only. Without one there is no exact join, and this source is not
        # worth two rate-limited round trips on a guess.
        if not track.isrc:
            return None

        for mbid in self._mbids(track.isrc):
            features = self._analysis(track, mbid)
            if features is not None:
                return features
        return None

    # ------------------------------------------------------------------
    def _mbids(self, isrc: str) -> list[str]:
        payload = self._get(
            f"{MB_BASE}/isrc/{isrc}",
            self.mb_limiter,
            params={"fmt": "json"},
            headers={"User-Agent": USER_AGENT},
        )
        recordings = (payload or {}).get("recordings")
        if not isinstance(recordings, list):
            return []
        out = []
        for r in recordings:
            if isinstance(r, dict) and isinstance(r.get("id"), str):
                out.append(r["id"])
            if len(out) >= MAX_MBIDS:
                break
        return out

    def _analysis(self, track: Track, mbid: str) -> AudioFeatures | None:
        payload = self._get(f"{AB_BASE}/{mbid}/low-level", self.ab_limiter)
        if not payload:
            return None

        rhythm = payload.get("rhythm")
        tonal = payload.get("tonal")
        if not isinstance(rhythm, dict):
            rhythm = {}
        if not isinstance(tonal, dict):
            tonal = {}

        bpm = _as_float(rhythm.get("bpm"))
        if bpm is None or bpm <= 0:
            # Essentia always emits a tempo; its absence means the row is not a
            # usable analysis, so do not trust the key from it either.
            return None

        key_camelot = None
        strength = _as_float(tonal.get("key_strength"))
        if strength is not None and strength >= self.min_key_strength:
            key_camelot = to_camelot(_key_name(tonal))
        elif strength is not None:
            log.debug(
                "acousticbrainz: dropping low-confidence key for %s (strength %.2f)",
                track.title,
                strength,
            )

        return AudioFeatures(
            spotify_id=track.spotify_id,
            bpm=round(bpm, 2),
            key_camelot=key_camelot,
            key_open=None,
            energy=_danceability(rhythm.get("danceability")),
            source=self.name,
            # Exact ISRC join, estimated values. Below GetSongBPM's curated
            # numbers, above a fuzzy-matched result.
            confidence=0.8 if key_camelot else 0.6,
        )

    # ------------------------------------------------------------------
    def _get(
        self,
        url: str,
        limiter: RateLimiter,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """A miss and a failure are both ``None`` here; neither is worth raising.

        A 404 is the normal answer for "no such ISRC" and "no analysis for this
        recording", which between them are most of the calls this source makes.
        """
        try:
            resp = request(
                "GET", url, params=params, headers=headers, rate_limiter=limiter
            )
        except HttpError as exc:
            if exc.status != 404:
                log.warning("%s failed: %s", url, exc)
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None


def _key_name(tonal: dict[str, Any]) -> str | None:
    """Join Essentia's split key fields into something ``to_camelot`` parses.

    The field pair was renamed at some point; older rows use ``key_key`` /
    ``key_scale``, newer ones ``key_edma``-prefixed variants. Accept both.
    """
    for note_field, scale_field in (
        ("key_key", "key_scale"),
        ("key_edma_key", "key_edma_scale"),
    ):
        note = tonal.get(note_field)
        scale = tonal.get(scale_field)
        if isinstance(note, str) and isinstance(scale, str) and note and scale:
            return f"{note} {scale}"
    return None


def _danceability(value: Any) -> float | None:
    """Essentia's danceability, squeezed into the 0..1 the schema expects.

    Its detrended-fluctuation figure runs to roughly 3, and observed values on
    this library sit around 1.15-1.40 — so the scale genuinely differs from
    GetSongBPM's danceability/100, which lands nearer 0.60-0.80 for the same
    kind of music. Dividing by :data:`DANCEABILITY_MAX` keeps the column
    honest but does *not* make the two comparable; only the per-source ranking
    in ``sequencing.comparable_energy`` does that. Writing this value without
    that ranking in place would make every cross-source transition look like a
    collapse and prune it out of the graph.
    """
    f = _as_float(value)
    if f is None or f < 0:
        return None
    return min(1.0, f / DANCEABILITY_MAX)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
