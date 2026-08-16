"""AcousticBrainz source: BPM + key over a two-hop ISRC -> MBID -> analysis join.

Measured before it was built — on 25 tracks with no key, MusicBrainz resolved
19 ISRCs and AcousticBrainz had a full analysis for 9 of them. It is the only
free source found that supplies harmonic data without fuzzy string matching.
"""

from __future__ import annotations

import pytest

from djset.enrichment import default_sources
from djset.enrichment.acousticbrainz import AcousticBrainzSource
from djset.enrichment.base import Resolver
from djset.models import AudioFeatures, Track

MBID = "f0f4c1e2-0000-4000-8000-000000000001"
ISRC = "USUG11902886"


def T(isrc: str | None = ISRC):
    return Track(
        spotify_id="t1", uri="spotify:track:t1", title="Hold My Liquor",
        artist="Kanye West", artist_names=["Kanye West"], isrc=isrc,
    )


def analysis(bpm=107.0, key="F", scale="minor", strength=0.75):
    tonal = {"key_strength": strength}
    if key is not None:
        tonal |= {"key_key": key, "key_scale": scale}
    return {"rhythm": {"bpm": bpm}, "tonal": tonal}


class Fake(AcousticBrainzSource):
    def __init__(self, routes: dict, **kw):
        super().__init__(mb_rate_per_hour=10**9, ab_rate_per_hour=10**9, **kw)
        self.routes = routes
        self.calls: list[str] = []

    def _get(self, url, limiter, *, params=None, headers=None):
        key = url.split("/api/v1/")[-1] if "/api/v1/" in url else url.split("/ws/2/")[-1]
        self.calls.append(key)
        return self.routes.get(key)


def isrc_route(*mbids):
    return {f"isrc/{ISRC}": {"recordings": [{"id": m} for m in mbids]}}


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_resolves_bpm_and_key_through_both_hops():
    src = Fake(isrc_route(MBID) | {f"{MBID}/low-level": analysis()})
    f = src.lookup(T())

    assert f is not None
    assert f.bpm == pytest.approx(107.0)
    assert f.key_camelot == "4A"  # F minor
    assert f.source == "acousticbrainz"
    assert src.calls == [f"isrc/{ISRC}", f"{MBID}/low-level"]


def test_major_and_minor_both_convert():
    minor = Fake(isrc_route(MBID) | {f"{MBID}/low-level": analysis(key="A#")})
    major = Fake(
        isrc_route(MBID) | {f"{MBID}/low-level": analysis(key="G", scale="major")}
    )
    assert minor.lookup(T()).key_camelot == "3A"  # A# minor
    assert major.lookup(T()).key_camelot == "9B"  # G major


# ---------------------------------------------------------------------------
# never assert a key it is not confident about
# ---------------------------------------------------------------------------


def test_low_confidence_key_is_dropped_but_tempo_is_kept():
    """A missing key costs one track. A wrong key corrupts every transition
    it takes part in, so the trade is not symmetric."""
    src = Fake(isrc_route(MBID) | {f"{MBID}/low-level": analysis(strength=0.2)})
    f = src.lookup(T())

    assert f is not None
    assert f.bpm == pytest.approx(107.0)
    assert f.key_camelot is None
    assert not f.is_usable


def test_threshold_is_tunable():
    routes = isrc_route(MBID) | {f"{MBID}/low-level": analysis(strength=0.4)}
    assert Fake(routes).lookup(T()).key_camelot is None
    assert Fake(routes, min_key_strength=0.3).lookup(T()).key_camelot == "4A"


def test_an_unparseable_key_is_dropped_not_guessed():
    src = Fake(isrc_route(MBID) | {f"{MBID}/low-level": analysis(key="H")})
    f = src.lookup(T())
    assert f.bpm == pytest.approx(107.0)
    assert f.key_camelot is None


def test_a_missing_tempo_discards_the_whole_row():
    """Essentia always emits a tempo; its absence means the row is not a usable
    analysis, so the key from it is not trustworthy either."""
    for bad in (None, 0, -1, "abc"):
        src = Fake(isrc_route(MBID) | {f"{MBID}/low-level": analysis(bpm=bad)})
        assert src.lookup(T()) is None


def test_confidence_reflects_whether_a_key_survived():
    with_key = Fake(isrc_route(MBID) | {f"{MBID}/low-level": analysis()}).lookup(T())
    without = Fake(
        isrc_route(MBID) | {f"{MBID}/low-level": analysis(strength=0.1)}
    ).lookup(T())
    assert with_key.confidence > without.confidence


# ---------------------------------------------------------------------------
# misses
# ---------------------------------------------------------------------------


def test_no_isrc_means_no_requests_at_all():
    """Without an exact join this source is not worth two rate-limited hops."""
    src = Fake({})
    assert src.lookup(T(isrc=None)) is None
    assert src.calls == []


def test_unknown_isrc_is_a_clean_miss():
    src = Fake({f"isrc/{ISRC}": None})
    assert src.lookup(T()) is None


def test_recording_without_an_analysis_is_a_clean_miss():
    src = Fake(isrc_route(MBID) | {f"{MBID}/low-level": None})
    assert src.lookup(T()) is None


def test_tries_the_next_mbid_when_the_first_has_no_analysis():
    """One ISRC maps to several recordings; coverage is per recording."""
    other = "aaaaaaaa-0000-4000-8000-000000000002"
    src = Fake(
        isrc_route(MBID, other)
        | {f"{MBID}/low-level": None, f"{other}/low-level": analysis()}
    )
    f = src.lookup(T())
    assert f is not None and f.key_camelot == "4A"


def test_mbid_attempts_are_capped():
    many = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(9)]
    src = Fake(isrc_route(*many))
    assert src.lookup(T()) is None
    assert len(src.calls) == 1 + 3  # the isrc hop, then MAX_MBIDS analyses


@pytest.mark.parametrize("payload", [{}, {"recordings": "nope"}, {"recordings": []}])
def test_malformed_musicbrainz_payloads_do_not_raise(payload):
    assert Fake({f"isrc/{ISRC}": payload}).lookup(T()) is None


@pytest.mark.parametrize("payload", [{}, {"rhythm": "nope", "tonal": 3}])
def test_malformed_analysis_payloads_do_not_raise(payload):
    src = Fake(isrc_route(MBID) | {f"{MBID}/low-level": payload})
    assert src.lookup(T()) is None


def test_accepts_the_renamed_essentia_key_fields():
    src = Fake(
        isrc_route(MBID)
        | {
            f"{MBID}/low-level": {
                "rhythm": {"bpm": 128.0},
                "tonal": {"key_edma_key": "C", "key_edma_scale": "major",
                          "key_strength": 0.8},
            }
        }
    )
    assert src.lookup(T()).key_camelot == "8B"


# ---------------------------------------------------------------------------
# priority within the chain
# ---------------------------------------------------------------------------


def test_sits_between_getsongbpm_and_deezer():
    chain = default_sources("key")
    assert [s.name for s in Resolver(chain).sources] == [
        "getsongbpm",
        "acousticbrainz",
        "deezer",
    ]


def test_curated_getsongbpm_wins_over_an_estimate():
    class FakeGSB:
        name, priority = "getsongbpm", 20

        def lookup(self, track):
            return AudioFeatures(track.spotify_id, 128.0, "8A", source=self.name)

    resolver = Resolver(
        [Fake(isrc_route(MBID) | {f"{MBID}/low-level": analysis()}), FakeGSB()]
    )
    features, _ = resolver.resolve(T())
    assert features.source == "getsongbpm"


def test_beats_deezer_because_deezer_has_no_key():
    class FakeDz:
        name, priority = "deezer", 30

        def lookup(self, track):
            return AudioFeatures(track.spotify_id, 107.0, None, source=self.name)

    resolver = Resolver(
        [FakeDz(), Fake(isrc_route(MBID) | {f"{MBID}/low-level": analysis()})]
    )
    features, _ = resolver.resolve(T())
    assert features.source == "acousticbrainz"
    assert features.key_camelot == "4A"


def test_each_source_can_be_disabled_independently():
    assert [s.name for s in default_sources("k", acousticbrainz=False)] == [
        "getsongbpm",
        "deezer",
    ]
    assert [s.name for s in default_sources(None, deezer=False)] == ["acousticbrainz"]
    assert default_sources(None, acousticbrainz=False, deezer=False) == []
