"""Source priority resolution.

The build brief asks for this test now, before RekordboxXMLSource exists, so
that dropping a priority-10 source in later is provably a registration rather
than a refactor.
"""

from __future__ import annotations

from djset import db
from djset.enrichment.base import MANUAL_SOURCE, Resolver, set_manual_features
from djset.enrichment.runner import enrich_tracks
from djset.models import AudioFeatures, Track


class FakeSource:
    """Answers only for the track ids it was given."""

    def __init__(self, name: str, priority: int, data: dict[str, tuple[float, str]]):
        self.name = name
        self.priority = priority
        self.data = data
        self.calls: list[str] = []

    def lookup(self, track: Track) -> AudioFeatures | None:
        self.calls.append(track.spotify_id)
        hit = self.data.get(track.spotify_id)
        if hit is None:
            return None
        bpm, key = hit
        return AudioFeatures(
            spotify_id=track.spotify_id,
            bpm=bpm,
            key_camelot=key,
            source=self.name,
        )


class ExplodingSource:
    name = "exploding"
    priority = 5

    def lookup(self, track: Track) -> AudioFeatures | None:
        raise RuntimeError("upstream is down")


def test_sources_sort_by_priority_on_construction():
    a = FakeSource("slow", 30, {})
    b = FakeSource("fast", 10, {})
    r = Resolver([a, b])
    assert [s.name for s in r.sources] == ["fast", "slow"]


def test_registering_later_keeps_the_order(track_factory):
    r = Resolver([FakeSource("getsongbpm", 20, {"t1": (128.0, "8A")})])
    r.register(FakeSource("rekordbox", 10, {"t1": (127.5, "9A")}))
    assert [s.name for s in r.sources] == ["rekordbox", "getsongbpm"]

    features, reason = r.resolve(track_factory(1))
    assert reason is None
    # Lower priority number wins, without any other code changing.
    assert features.source == "rekordbox"
    assert features.bpm == 127.5


def test_resolution_falls_through_to_the_next_source(track_factory):
    high = FakeSource("rekordbox", 10, {})           # no data for t1
    low = FakeSource("getsongbpm", 20, {"t1": (120.0, "5A")})
    r = Resolver([low, high])

    features, reason = r.resolve(track_factory(1))
    assert reason is None
    assert features.source == "getsongbpm"
    assert high.calls == ["t1"]  # the higher-priority source was tried first


def test_all_sources_miss_reports_every_reason(track_factory):
    r = Resolver([FakeSource("a", 10, {}), FakeSource("b", 20, {})])
    features, reason = r.resolve(track_factory(1))
    assert features is None
    assert "a: no match" in reason and "b: no match" in reason


def test_a_broken_source_does_not_kill_the_pass(track_factory):
    r = Resolver([ExplodingSource(), FakeSource("getsongbpm", 20, {"t1": (100.0, "1A")})])
    features, reason = r.resolve(track_factory(1))
    assert reason is None
    assert features.source == "getsongbpm"


def test_no_sources_registered_is_a_clean_miss(track_factory):
    features, reason = Resolver([]).resolve(track_factory(1))
    assert features is None
    assert "no sources" in reason


# --------------------------------------------------------------------------
# overwrite rules
# --------------------------------------------------------------------------


def test_manual_is_never_overwritten_by_an_automated_source():
    r = Resolver([FakeSource("rekordbox", 10, {}), FakeSource("getsongbpm", 20, {})])
    manual = AudioFeatures(spotify_id="t1", bpm=128, key_camelot="8A", source=MANUAL_SOURCE)
    assert r.should_overwrite(manual, candidate_priority=10) is False
    assert r.should_overwrite(manual, candidate_priority=0) is False


def test_higher_trust_source_overwrites_lower():
    r = Resolver([FakeSource("rekordbox", 10, {}), FakeSource("getsongbpm", 20, {})])
    cached = AudioFeatures(spotify_id="t1", bpm=128, key_camelot="8A", source="getsongbpm")
    assert r.should_overwrite(cached, candidate_priority=10) is True
    assert r.should_overwrite(cached, candidate_priority=20) is False
    assert r.should_overwrite(None, candidate_priority=20) is True


def test_manual_entry_wins_end_to_end(conn, track_factory):
    t = track_factory(1)
    db.upsert_track(conn, t)
    r = Resolver([FakeSource("getsongbpm", 20, {"t1": (128.0, "8A")})])

    enrich_tracks(conn, r, [t])
    assert db.get_features(conn, "t1").source == "getsongbpm"

    set_manual_features(conn, "t1", bpm=126.0, key_camelot="9A")

    # A second pass must not clobber the hand-typed value.
    enrich_tracks(conn, r, [t])
    got = db.get_features(conn, "t1")
    assert got.source == MANUAL_SOURCE
    assert got.bpm == 126.0
