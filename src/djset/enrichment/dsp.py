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
* **A weak estimate is discarded rather than reported** — but only when the
  disagreement would change a set. A clear winner is kept outright. When
  nothing wins by :data:`MIN_KEY_MARGIN`, the near-ties are checked against
  the Camelot wheel: if they all sit beside the winner, as the relative minor
  and the fifth do, then every candidate mixes with the same neighbours and
  the choice between them cannot produce a bad transition. Only a genuine
  disagreement — parallel major/minor, three steps apart — sends the tempo out
  alone. A missing key costs one track's eligibility; a wrong key corrupts
  every transition it takes part in; an *inconsequential* ambiguity costs
  nothing and used to cost 512 tracks.

Audio comes from Deezer, and from iTunes where Deezer has no preview — the two
catalogues have different holes. iTunes serves AAC, which libsndfile will not
read, so those are transcoded with ffmpeg when one is on PATH; without it the
MP3 path is unaffected.

The audio is streamed to a temp file, measured, and deleted. Nothing is kept:
the preview is a means of measurement, not a download.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

from ..camelot import key_distance, to_camelot
from ..models import MEASURED_SOURCE, AudioFeatures, Track
from ..net import HttpError, RateLimiter, request
from .normalize import normalize_artist, normalize_title, similarity

log = logging.getLogger(__name__)

DEEZER = "https://api.deezer.com"

# Second opinion on where to find 30 seconds of audio. Deezer's catalogue has
# holes — regional licensing, very new releases, and anything it simply never
# carried — and a track with no preview anywhere is the one case this source
# cannot answer at all. iTunes needs no key and covers a different catalogue,
# so the two together leave far less unmeasured than either alone.
ITUNES = "https://itunes.apple.com/search"
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
    name = MEASURED_SOURCE
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
        """Where to get 30 seconds of this track.

        Deezer first, by ISRC, since that is an exact join rather than a guess
        at a title. iTunes is asked only when Deezer has nothing — it is a
        fallback for the holes, not a second vote.
        """
        if track.isrc:
            payload = self._get(f"/track/isrc:{track.isrc}")
            if payload and payload.get("preview"):
                return str(payload["preview"])
        return self._search_preview(track) or self._itunes_preview(track)

    def _itunes_preview(self, track: Track) -> str | None:
        artist = normalize_artist(track.primary_artist)
        title = normalize_title(track.title)
        if not artist or not title:
            return None
        try:
            resp = request(
                "GET",
                ITUNES,
                params={"term": f"{artist} {title}", "entity": "song", "limit": 5},
                rate_limiter=self.limiter,
            )
            rows = resp.json().get("results")
        except (HttpError, ValueError, AttributeError):
            return None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict) or not row.get("previewUrl"):
                continue
            # Same similarity gate as Deezer. A search that returns *something*
            # for every query is worse than one that returns nothing: measuring
            # the wrong song writes a confident, wrong BPM.
            if (
                similarity(title, normalize_title(row.get("trackName") or ""))
                >= MIN_TITLE_SIM
                and similarity(artist, normalize_artist(row.get("artistName") or ""))
                >= MIN_ARTIST_SIM
            ):
                return str(row["previewUrl"])
        return None

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

    fd, path = tempfile.mkstemp(suffix=_suffix_for(audio), prefix="djset-")
    wav: str | None = None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)
        try:
            y, sr = librosa.load(path, sr=SAMPLE_RATE, mono=True)
        except Exception:
            # libsndfile reads MP3 but not AAC, and iTunes serves .m4a. Rather
            # than refuse a whole catalogue over a container format, hand it to
            # ffmpeg when there is one. Without ffmpeg this is exactly the old
            # behaviour, so Deezer's MP3s keep working either way.
            wav = _transcode(path)
            if wav is None:
                raise
            y, sr = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
    except Exception as exc:
        log.warning("could not decode preview: %s", exc)
        return None
    finally:
        for tmp in (path, wav):
            if tmp:
                try:
                    os.unlink(tmp)
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


def _suffix_for(audio: bytes) -> str:
    """The file extension libsndfile needs in order to recognise these bytes.

    soundfile dispatches on the *filename*, not the content, so a preview
    written to a neutral suffix fails to open even when it is an ordinary MP3.
    Getting this wrong is quiet and expensive: everything still worked, via
    ffmpeg, which turned an optional dependency into a required one and paid a
    subprocess and a temp WAV for every track.
    """
    if audio[:3] == b"ID3" or (
        len(audio) > 1 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0
    ):
        return ".mp3"
    if audio[4:8] == b"ftyp":      # iTunes serves AAC in an MP4 container
        return ".m4a"
    if audio[:4] == b"OggS":
        return ".ogg"
    if audio[:4] == b"fLaC":
        return ".flac"
    if audio[:4] == b"RIFF":
        return ".wav"
    return ".mp3"                  # the common case; ffmpeg covers a wrong guess


def _transcode(src: str) -> str | None:
    """``src`` as a mono WAV via ffmpeg, or None if that is not possible.

    Optional by design: it exists so one container format does not cost a whole
    catalogue, and its absence costs only the formats libsndfile cannot read.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.debug("no ffmpeg on PATH; cannot decode this container")
        return None
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="djset-")
    os.close(fd)
    try:
        subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", src,
             "-ac", "1", "-ar", str(SAMPLE_RATE), out],
            check=True, capture_output=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("ffmpeg could not decode the preview: %s", exc)
        try:
            os.unlink(out)
        except OSError:
            pass
        return None
    return out


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
    return _decide(scored)


def _decide(scored: list[tuple[float, str]]) -> tuple[str | None, float]:
    """Pick a key from ranked ``(score, "C major")`` candidates, or decline.

    Separate from the scoring because this is where the judgement lives, and
    the judgement is the part worth testing against named keys rather than
    against synthesised audio.
    """
    if not scored:
        return None, 0.0
    best, runner_up = scored[0], scored[1] if len(scored) > 1 else (0.0, "")
    if best[0] <= 0:
        return None, 0.0

    margin = (best[0] - runner_up[0]) / abs(best[0])
    top = to_camelot(best[1])
    if margin >= MIN_KEY_MARGIN:
        return top, margin

    # Several profiles fit about equally well. That is only a problem if they
    # disagree in a way that *matters*: the near-misses are usually the
    # relative minor or the fifth, which sit one step from the winner on the
    # wheel and mix with the same neighbours. Being unsure between 8A and 8B is
    # not the same as not knowing the key — a transition built on either works.
    #
    # So ask the wheel rather than the margin, and ask it about *every*
    # candidate close enough to have won, not just the runner-up: two
    # compatible front-runners mean nothing if a third one a tone away scored
    # the same. Parallel major/minor is the usual genuine tie — three steps
    # apart — and there the tempo goes out alone.
    cutoff = best[0] * (1.0 - MIN_KEY_MARGIN)
    contenders = [to_camelot(name) for score, name in scored if score >= cutoff]
    if top and all(c and key_distance(top, c) is not None for c in contenders):
        log.debug("thin margin %.3f but all %d contenders sit beside %s — keeping it",
                  margin, len(contenders), top)
        return top, margin
    log.debug("key discarded, margin %.3f (%s vs %s)", margin, best[1], runner_up[1])
    return None, margin
