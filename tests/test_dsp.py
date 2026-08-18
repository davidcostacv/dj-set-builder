"""Measuring the audio instead of looking it up (priority 40).

The reason this source exists, measured across the library by release year:
89% of pre-2015 tracks resolve from catalogues, 17% of 2022, 1% of 2023
onward. That cliff is February 2022, when AcousticBrainz stopped accepting
submissions. On 14 post-2023 tracks that no catalogue could resolve, this
answered 14.

Measured against GetSongBPM as curated truth on 22 tracks: BPM usable 77%
(41% exact, 18% within the sequencer's tolerance, 18% half/double), key
mixable 74% of those reported. Those numbers are why it sits below every
catalogue and why a weak key is dropped rather than guessed.
"""

from __future__ import annotations

import numpy as np
import pytest

from djset.enrichment import default_sources
from djset.enrichment.base import Resolver
from djset.enrichment.dsp import MIN_KEY_MARGIN, DSPSource, _estimate_key, analyse
from djset.models import AudioFeatures, Track

librosa = pytest.importorskip("librosa")

SR = 22_050


def T(tid="t1", isrc="USUG11902886", title="Sun Rising", artist="Someone"):
    return Track(tid, f"spotify:track:{tid}", title, artist,
                 artist_names=[artist], isrc=isrc)


def tone_series(notes, seconds=6.0, sr=SR):
    """A synthetic clip in a known key, so the estimator has a right answer."""
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    y = np.zeros_like(t)
    for i, midi in enumerate(notes):
        freq = 440.0 * 2 ** ((midi - 69) / 12)
        seg = (t >= i * seconds / len(notes)) & (t < (i + 1) * seconds / len(notes))
        y[seg] += np.sin(2 * np.pi * freq * t[seg])
    return y


# ---------------------------------------------------------------------------
# key estimation
# ---------------------------------------------------------------------------


def test_a_c_major_triad_reads_as_c_major():
    # C4 E4 G4 repeated: unambiguously C major, Camelot 8B.
    y = tone_series([60, 64, 67] * 4)
    key, margin = _estimate_key(y, SR)
    assert key == "8B"
    assert margin >= MIN_KEY_MARGIN


def test_an_a_minor_triad_reads_as_a_minor():
    y = tone_series([57, 60, 64] * 4)   # A3 C4 E4
    key, _ = _estimate_key(y, SR)
    assert key == "8A"


def test_a_weak_estimate_is_dropped_rather_than_guessed():
    """A missing key costs one track's eligibility; a wrong key corrupts every
    transition it takes part in, so the trade is not symmetric."""
    rng = np.random.default_rng(0)
    key, margin = _estimate_key(rng.standard_normal(SR * 4) * 0.1, SR)
    assert key is None or margin >= MIN_KEY_MARGIN


def test_silence_yields_no_key():
    key, margin = _estimate_key(np.zeros(SR * 2), SR)
    assert key is None


# ---------------------------------------------------------------------------
# analyse()
# ---------------------------------------------------------------------------


def test_undecodable_bytes_are_a_clean_miss_not_a_crash():
    assert analyse(b"this is not audio at all") is None


def test_empty_input_is_a_clean_miss():
    assert analyse(b"") is None


# ---------------------------------------------------------------------------
# the source
# ---------------------------------------------------------------------------


class Fake(DSPSource):
    """Stubs the two boundaries — the preview URL and the bytes — so the rest
    of the source runs for real."""

    def __init__(self, url=None, audio=None, analysis=("skip",)):
        super().__init__(rate_per_hour=10**9)
        self._url, self._audio, self._analysis = url, audio, analysis
        self.downloaded = []

    def preview_url(self, track):
        return self._url

    def _download(self, url):
        self.downloaded.append(url)
        return self._audio


def test_no_preview_means_no_request_and_no_result():
    src = Fake(url=None)
    assert src.lookup(T()) is None
    assert src.downloaded == []


def test_a_failed_download_is_a_clean_miss():
    src = Fake(url="http://x/p.mp3", audio=None)
    assert src.lookup(T()) is None


def test_undecodable_audio_is_a_clean_miss():
    src = Fake(url="http://x/p.mp3", audio=b"junk" * 4000)
    assert src.lookup(T()) is None


def test_a_result_is_labelled_as_measured_not_verified(monkeypatch):
    src = Fake(url="http://x/p.mp3", audio=b"x" * 20_000)
    monkeypatch.setattr("djset.enrichment.dsp.analyse", lambda b: (128.0, "8A", 0.2))

    f = src.lookup(T())
    assert f.source == "dsp"
    assert (f.bpm, f.key_camelot) == (128.0, "8A")
    # Below a curated value: this was computed, not confirmed by anyone.
    assert f.confidence < 0.7


def test_a_dropped_key_still_returns_the_tempo(monkeypatch):
    src = Fake(url="http://x/p.mp3", audio=b"x" * 20_000)
    monkeypatch.setattr("djset.enrichment.dsp.analyse", lambda b: (128.0, None, 0.01))

    f = src.lookup(T())
    assert f.bpm == 128.0
    assert f.key_camelot is None
    assert not f.is_usable          # cannot take part in BPM+Key sequencing


@pytest.mark.parametrize("bpm,kept", [(10.0, False), (500.0, False), (128.0, True)])
def test_an_implausible_tempo_is_refused(bpm, kept, monkeypatch):
    """Outside 40-220 is a detector artefact, not a song. Driven through
    analyse() with real audio so the guard is actually exercised."""
    monkeypatch.setattr(
        librosa.beat, "beat_track", lambda **kw: (np.array([bpm]), np.array([]))
    )
    import io

    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, tone_series([60, 64, 67], seconds=3.0), SR, format="WAV")
    result = analyse(buf.getvalue())

    assert (result is not None) is kept
    if kept:
        assert result[0] == pytest.approx(bpm)


# ---------------------------------------------------------------------------
# where it sits
# ---------------------------------------------------------------------------


def test_it_is_off_by_default():
    """It downloads audio and costs seconds of CPU per track, so enabling it
    changes what a run costs rather than only what it finds."""
    assert "dsp" not in [s.name for s in default_sources("key")]
    assert "dsp" in [s.name for s in default_sources("key", dsp=True)]


def test_it_sits_below_every_catalogue():
    chain = Resolver(default_sources("key", dsp=True)).sources
    assert [s.name for s in chain] == [
        "getsongbpm", "acousticbrainz", "deezer", "dsp",
    ]


def test_a_catalogue_answer_wins_over_a_measurement():
    class Curated:
        name, priority = "getsongbpm", 20

        def lookup(self, track):
            return AudioFeatures(track.spotify_id, 128.0, "8A", source=self.name)

    resolver = Resolver([Curated(), Fake(url=None)])
    features, _ = resolver.resolve(T())
    assert features.source == "getsongbpm"


def test_it_answers_where_the_catalogues_are_silent(monkeypatch):
    """The whole point: on 14 post-2023 tracks no catalogue could resolve,
    this returned 14."""
    class Silent:
        def __init__(self, name, priority):
            self.name, self.priority = name, priority

        def lookup(self, track):
            return None

    src = Fake(url="http://x/p.mp3", audio=b"x" * 20_000)
    monkeypatch.setattr("djset.enrichment.dsp.analyse", lambda b: (117.5, "4A", 0.2))

    resolver = Resolver(
        [Silent("getsongbpm", 20), Silent("acousticbrainz", 25), Silent("deezer", 30), src]
    )
    features, reason = resolver.resolve(T())
    assert reason is None
    assert features.source == "dsp"
    assert features.is_usable
