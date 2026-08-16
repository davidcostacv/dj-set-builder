"""Deezer source: BPM only, joined on ISRC.

Measured before it was built — on 30 tracks GetSongBPM could not resolve,
Deezer matched 30/30 by ISRC and supplied a real tempo for 6 (~20% new
coverage over the gap). It carries no harmonic information, which is why it
sits below GetSongBPM in priority.
"""

from __future__ import annotations

import pytest

from djset.enrichment import default_sources
from djset.enrichment.base import Resolver
from djset.enrichment.deezer import DeezerSource
from djset.models import AudioFeatures, Track


def T(isrc: str | None = "USUG11902886", title="Sum 2 Prove", artist="Lil Baby"):
    return Track(
        spotify_id="t1", uri="spotify:track:t1", title=title, artist=artist,
        artist_names=[artist], isrc=isrc,
    )


class FakeDeezer(DeezerSource):
    def __init__(self, routes: dict):
        super().__init__(rate_per_hour=10**9)
        self.routes = routes
        self.calls: list[str] = []

    def _get(self, path, **params):
        self.calls.append(path)
        value = self.routes.get(path)
        return value(params) if callable(value) else value


# ---------------------------------------------------------------------------
# ISRC path
# ---------------------------------------------------------------------------


def test_isrc_lookup_is_exact_and_needs_one_request():
    src = FakeDeezer({"/track/isrc:USUG11902886": {"bpm": 123.45, "title": "Sum 2 Prove"}})
    f = src.lookup(T())

    assert f is not None
    assert f.bpm == pytest.approx(123.45)
    assert f.source == "deezer"
    assert src.calls == ["/track/isrc:USUG11902886"]


def test_deezer_never_claims_a_key():
    """It has no harmonic data; inventing one would poison harmonic mixing."""
    src = FakeDeezer({"/track/isrc:USUG11902886": {"bpm": 120}})
    f = src.lookup(T())
    assert f.key_camelot is None
    assert f.key_open is None
    assert not f.is_usable  # cannot take part in BPM+Key sequencing


def test_zero_bpm_means_unknown_not_zero():
    """Deezer uses 0 rather than null for missing tempo."""
    src = FakeDeezer({"/track/isrc:USUG11902886": {"bpm": 0}})
    assert src.lookup(T()) is None


@pytest.mark.parametrize("value", [None, "", "abc", -5])
def test_unusable_bpm_values_are_rejected(value):
    src = FakeDeezer({"/track/isrc:USUG11902886": {"bpm": value}})
    assert src.lookup(T()) is None


def test_isrc_confidence_beats_search_confidence():
    by_isrc = FakeDeezer({"/track/isrc:USUG11902886": {"bpm": 120}}).lookup(T())
    by_search = FakeDeezer(
        {
            "/track/isrc:NOPE": None,
            "/search": {"data": [{"id": 7, "title": "Sum 2 Prove",
                                  "artist": {"name": "Lil Baby"}}]},
            "/track/7": {"bpm": 120},
        }
    ).lookup(T(isrc="NOPE"))
    assert by_isrc.confidence > by_search.confidence


# ---------------------------------------------------------------------------
# search fallback
# ---------------------------------------------------------------------------


def test_falls_back_to_search_when_there_is_no_isrc():
    src = FakeDeezer(
        {
            "/search": {"data": [{"id": 42, "title": "Sum 2 Prove",
                                  "artist": {"name": "Lil Baby"}}]},
            "/track/42": {"bpm": 140.0},
        }
    )
    f = src.lookup(T(isrc=None))
    assert f is not None and f.bpm == 140.0
    assert "/search" in src.calls


def test_search_rejects_a_wrong_match():
    src = FakeDeezer(
        {
            "/search": {"data": [{"id": 1, "title": "Something Else Entirely",
                                  "artist": {"name": "Another Band"}}]},
        }
    )
    assert src.lookup(T(isrc=None)) is None


def test_an_api_error_is_a_clean_miss():
    assert FakeDeezer({"/track/isrc:USUG11902886": None}).lookup(T()) is None
    assert FakeDeezer({"/search": {"data": []}}).lookup(T(isrc=None)) is None


# ---------------------------------------------------------------------------
# priority within the chain
# ---------------------------------------------------------------------------


def test_deezer_sits_last_because_it_has_no_key():
    chain = default_sources("key")
    assert [s.name for s in Resolver(chain).sources][-1] == "deezer"


def test_getsongbpm_wins_when_both_have_data():
    """A GetSongBPM hit carries key as well, so it must be preferred."""

    class FakeGSB:
        name, priority = "getsongbpm", 20

        def lookup(self, track):
            return AudioFeatures(track.spotify_id, 128.0, "8A", source=self.name)

    resolver = Resolver([FakeDeezer({"/track/isrc:USUG11902886": {"bpm": 999}}), FakeGSB()])
    features, _ = resolver.resolve(T())
    assert features.source == "getsongbpm"
    assert features.key_camelot == "8A"


def test_deezer_fills_the_gap_when_getsongbpm_misses():
    class MissingGSB:
        name, priority = "getsongbpm", 20

        def lookup(self, track):
            return None

    resolver = Resolver(
        [MissingGSB(), FakeDeezer({"/track/isrc:USUG11902886": {"bpm": 116.1}})]
    )
    features, reason = resolver.resolve(T())
    assert reason is None
    assert features.source == "deezer"
    assert features.bpm == pytest.approx(116.1)


def test_chain_without_a_getsongbpm_key_still_has_deezer():
    assert "deezer" in [s.name for s in default_sources(None)]
