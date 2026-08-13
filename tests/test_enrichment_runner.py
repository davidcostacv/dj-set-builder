from djset import db
from djset.enrichment.base import Resolver
from djset.enrichment.runner import enrich_tracks
from djset.models import AudioFeatures, Track

import threading


class CountingSource:
    name = "counting"
    priority = 20

    def __init__(self, hits: set[str]):
        self.hits = hits
        self.calls: list[str] = []

    def lookup(self, track: Track) -> AudioFeatures | None:
        self.calls.append(track.spotify_id)
        if track.spotify_id in self.hits:
            return AudioFeatures(
                spotify_id=track.spotify_id, bpm=128.0, key_camelot="8A", source=self.name
            )
        return None


def test_cached_tracks_are_never_refetched(conn, track_factory):
    tracks = [track_factory(i) for i in (1, 2)]
    for t in tracks:
        db.upsert_track(conn, t)

    src = CountingSource({"t1", "t2"})
    r = Resolver([src])

    first = enrich_tracks(conn, r, tracks)
    assert first.resolved == 2

    src.calls.clear()
    second = enrich_tracks(conn, r, tracks)
    assert src.calls == []
    assert second.already_cached == 2
    assert second.resolved == 0


def test_misses_are_recorded_and_retried_at_most_three_times(conn, track_factory):
    t = track_factory(1)
    db.upsert_track(conn, t)
    src = CountingSource(set())
    r = Resolver([src])

    for _ in range(5):
        enrich_tracks(conn, r, [t])

    assert len(src.calls) == db.MAX_ATTEMPTS
    assert "t1" in db.exhausted_ids(conn)

    stats = enrich_tracks(conn, r, [t])
    assert stats.skipped_exhausted == 1


def test_a_later_hit_clears_the_miss_record(conn, track_factory):
    t = track_factory(1)
    db.upsert_track(conn, t)

    enrich_tracks(conn, Resolver([CountingSource(set())]), [t])
    assert db.miss_reasons(conn)

    enrich_tracks(conn, Resolver([CountingSource({"t1"})]), [t])
    assert db.miss_reasons(conn) == {}


def test_cancel_stops_the_pass_and_keeps_what_landed(conn, track_factory):
    tracks = [track_factory(i) for i in range(1, 6)]
    for t in tracks:
        db.upsert_track(conn, t)

    cancel = threading.Event()

    class CancelAfterTwo(CountingSource):
        def lookup(self, track):
            if len(self.calls) >= 2:
                cancel.set()
            return super().lookup(track)

    stats = enrich_tracks(
        conn, Resolver([CancelAfterTwo({f"t{i}" for i in range(1, 6)})]), tracks, cancel=cancel
    )

    assert stats.cancelled
    assert 0 < stats.resolved < 5
    # Whatever resolved before the cancel is durably committed, so a re-run resumes.
    assert len(db.all_features(conn)) == stats.resolved
