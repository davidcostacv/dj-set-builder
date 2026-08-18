"""Analyse the audio instead of looking it up (priority 40).

Every other source asks "does someone already know this song's tempo?", and
for recent music nobody does. Measured across this library by release year:
89% of pre-2015 tracks resolve, 17% of 2022, and 1% of 2023 onward. That cliff
is February 2022, when AcousticBrainz stopped accepting submissions — it is a
frozen archive, so the gap widens with every release.

This source asks a different question. It fetches the 30-second preview and
measures the audio, so it always produces an answer and its coverage does not
decay. That is why it sits at priority 40, below every curated source: a value
somebody verified beats one this computed, and this is the floor that catches
what nothing else knows.

What it is honest about:

* **Tempo is reliable.** Beat tracking over 30 seconds is the well-solved half.
* **Key is an estimate**, right roughly 70-85% of the time. The usual failures
  are the relative major/minor and the fifth, which land on *adjacent* Camelot
  codes — `8B` against `8A` or `9B` — and those still mix, so the errors
  degrade gracefully for this particular use. The exception is parallel
  major/minor, which does not.
* **A weak estimate is discarded rather than reported.** The key is kept only
  when the winning profile beats the runner-up by :data:`MIN_KEY_MARGIN`;
  otherwise the tempo is returned alone. A missing key costs one track's
  eligibility, a wrong key corrupts every transition it takes part in.

The audio is streamed to a temp file, measured, and deleted. Nothing is kept:
the preview is a means of measurement, not a download.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from ..camelot import to_camelot
from ..models import AudioFeatures, Track
from ..net import HttpError, RateLimiter, request
from .normalize import normalize_artist, normalize_title, similarity

log = logging.getLogger(__name__)

DEEZER = "https://api.deezer.com"
DEFAULT_RATE_PER_HOUR = 3_600  # one a second; each also costs seconds of CPU

# Analysis rate. 22.05 kHz is plenty for tempo and chroma and halves the work.
SAMPLE_RATE = 22_050

# Tempos outside this are a detector artefact rather than a song.
MIN_BPM, MAX_BPM = 40.0, 220.0

# How far the winning key must beat the runner-up, as a fraction of the best
# correlation. Below this the estimate is a coin toss and the key is dropped.
MIN_KEY_MARGIN = 0.03

MIN_TITLE_SIM = 0.6
MIN_ARTIST_SIM = 0.5

_PITCHES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Krumhansl-Schmuckler key profiles: how strongly each scale degree is expected
# to sound in a major and a minor key. Correlating the track's average chroma
# against all 24 rotations is the standard way to name a key.
_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)


class DSPSource:
    name = "dsp"
    priority = 40

    def __init__(self, rate_per_hour: int = DEFAULT_RATE_PER_HOUR) -> None:
        self.limiter = RateLimiter(rate_per_hour)

    # ------------------------------------------------------------------
    def lookup(self, track: Track) -> AudioFeatures | None:
        url = self.preview_url(track)
        if not url:
            return None
        audio = self._download(url)
        if not audio:
            return None

        analysis = analyse(audio)
        if analysis is None:
            return None
        bpm, key, margin = analysis
        return AudioFeatures(
            spotify_id=track.spotify_id,
            bpm=round(bpm, 2),
            key_camelot=key,
            key_open=None,
            energy=None,
            source=self.name,
            # Measured, not verified: below every catalogue that had an answer.
            confidence=0.55 if key else 0.4,
        )

    # ------------------------------------------------------------------
    def preview_url(self, track: Track) -> str | None:
        """Deezer serves the clip. ISRC first, since it is an exact join."""
        if track.isrc:
            payload = self._get(f"/track/isrc:{track.isrc}")
            if payload and payload.get("preview"):
                return str(payload["preview"])
        return self._search_preview(track)

    def _search_preview(self, track: Track) -> str | None:
        artist = normalize_artist(track.primary_artist)
        title = normalize_title(track.title)
        if not artist or not title:
            return None
        payload = self._get("/search", q=f'artist:"{artist}" track:"{title}"')
        rows = (payload or {}).get("data")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict) or not row.get("preview"):
                continue
            r_title = normalize_title(row.get("title") or "")
            r_artist = normalize_artist((row.get("artist") or {}).get("name") or "")
            if (
                similarity(title, r_title) >= MIN_TITLE_SIM
                and similarity(artist, r_artist) >= MIN_ARTIST_SIM
            ):
                return str(row["preview"])
        return None

    def _get(self, path: str, **params: Any) -> dict[str, Any] | None:
        try:
            resp = request(
                "GET", f"{DEEZER}{path}", params=params or None, rate_limiter=self.limiter
            )
            payload = resp.json()
        except (HttpError, ValueError):
            return None
        return payload if isinstance(payload, dict) and "error" not in payload else None

    def _download(self, url: str) -> bytes | None:
        try:
            resp = request("GET", url, rate_limiter=self.limiter)
        except HttpError as exc:
            log.warning("preview download failed: %s", exc)
            return None
        return resp.content if len(resp.content) > 10_000 else None


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------


def analyse(audio: bytes) -> tuple[float, str | None, float] | None:
    """``(bpm, camelot_or_None, key_margin)`` from encoded audio.

    Via a temp file rather than a buffer: libsndfile refuses an in-memory MP3
    with "Format not recognised", and these previews are MP3 with an ID3 tag.
    The file is removed before returning, on every path.
    """
    try:
        import librosa
        import numpy as np
    except ImportError:
        log.error("the dsp source needs librosa: pip install librosa")
        return None

    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="djset-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)
        y, sr = librosa.load(path, sr=SAMPLE_RATE, mono=True)
    except Exception as exc:
        log.warning("could not decode preview: %s", exc)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if y.size < sr:  # under a second of audio is not worth measuring
        return None

    try:
        tempo = float(np.atleast_1d(librosa.beat.beat_track(y=y, sr=sr)[0])[0])
    except Exception as exc:
        log.warning("beat tracking failed: %s", exc)
        return None
    if not (MIN_BPM <= tempo <= MAX_BPM):
        return None

    key, margin = _estimate_key(y, sr)
    return tempo, key, margin


def _estimate_key(y, sr) -> tuple[str | None, float]:
    """Average chroma against all 24 Krumhansl-Schmuckler profiles."""
    import librosa
    import numpy as np

    try:
        # Percussion smears the chroma; the harmonic part is what carries key.
        harmonic = librosa.effects.harmonic(y)
        chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr)
    except Exception as exc:
        log.warning("chroma failed: %s", exc)
        return None, 0.0

    profile = chroma.mean(axis=1)
    if not np.any(profile):
        return None, 0.0
    profile = (profile - profile.mean()) / (profile.std() or 1.0)

    scored: list[tuple[float, str]] = []
    for mode, weights in (("major", _MAJOR), ("minor", _MINOR)):
        w = np.asarray(weights, dtype=float)
        w = (w - w.mean()) / (w.std() or 1.0)
        for tonic in range(12):
            score = float(np.dot(profile, np.roll(w, tonic)) / 12.0)
            scored.append((score, f"{_PITCHES[tonic]} {mode}"))

    scored.sort(reverse=True)
    best, runner_up = scored[0], scored[1]
    if best[0] <= 0:
        return None, 0.0

    margin = (best[0] - runner_up[0]) / abs(best[0])
    if margin < MIN_KEY_MARGIN:
        # Two keys fit about equally well, which is a coin toss. Report the
        # tempo alone rather than a key that would silently break every
        # transition it takes part in.
        log.debug("key discarded, margin %.3f (%s vs %s)", margin, best[1], runner_up[1])
        return None, margin
    return to_camelot(best[1]), margin
