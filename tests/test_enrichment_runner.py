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


# ---------------------------------------------------------------------------
# retry_misses: the flag to pull after registering a new source
# ---------------------------------------------------------------------------


class NewSource:
    """Stands in for a source registered after the library was first enriched."""

    name, priority = "new-source", 25

    def __init__(self):
        self.asked: list[str] = []

    def lookup(self, track: Track) -> AudioFeatures:
        self.asked.append(track.spotify_id)
        return AudioFeatures(track.spotify_id, 128.0, "9A", source=self.name)


def _exhaust(conn, spotify_id: str) -> None:
    for _ in range(db.MAX_ATTEMPTS):
        db.record_miss(conn, spotify_id, "no match")


def test_retry_misses_keeps_hits_but_reattempts_failures(conn, track_factory):
    """A new source exists to serve the tracks the old ones missed. Re-asking
    about thousands of already-answered tracks would cost hours for nothing."""
    hit, missed = track_factory(1), track_factory(2)
    db.upsert_track(conn, hit)
    db.upsert_track(conn, missed)
    db.upsert_features(
        conn, AudioFeatures(hit.spotify_id, 120.0, "8A", source="getsongbpm")
    )
    _exhaust(conn, missed.spotify_id)
    conn.commit()

    source = NewSource()
    stats = enrich_tracks(conn, Resolver([source]), retry_misses=True)

    assert source.asked == [missed.spotify_id]  # the exhausted one, and only it
    assert stats.already_cached == 1            # the hit was left alone
    assert stats.skipped_exhausted == 0         # the ceiling was ignored
    assert stats.resolved == 1

    features = db.all_features(conn)
    assert features[hit.spotify_id].source == "getsongbpm"
    assert features[missed.spotify_id].source == "new-source"


def test_without_the_flag_an_exhausted_track_stays_skipped(conn, track_factory):
    missed = track_factory(1)
    db.upsert_track(conn, missed)
    _exhaust(conn, missed.spotify_id)
    conn.commit()

    source = NewSource()
    stats = enrich_tracks(conn, Resolver([source]))

    assert source.asked == []
    assert stats.skipped_exhausted == 1


def test_refresh_reattempts_even_cached_hits(conn, track_factory):
    """The broader hammer: only for when existing data is itself suspect."""
    hit = track_factory(1)
    db.upsert_track(conn, hit)
    db.upsert_features(
        conn, AudioFeatures(hit.spotify_id, 120.0, "8A", source="getsongbpm")
    )
    conn.commit()

    source = NewSource()
    stats = enrich_tracks(conn, Resolver([source]), refresh=True)

    assert source.asked == [hit.spotify_id]
    assert stats.already_cached == 0
    # The row still stands: an unregistered source's priority is unknowable,
    # and unknown is not evidence that the candidate is better.
    assert db.all_features(conn)[hit.spotify_id].source == "getsongbpm"


class KeylessSource:
    """A source that outranks another but has nothing harmonic to say."""

    name, priority = "keyless", 10

    def lookup(self, track: Track) -> AudioFeatures:
        return AudioFeatures(track.spotify_id, 999.0, None, source=self.name)


def test_a_higher_trust_source_may_not_erase_a_known_key(conn, track_factory):
    """Priority settles whose tempo to believe, not who has more to say.
    Trading a sequenceable track for a marginally better BPM is a bad deal."""
    t = track_factory(1)
    db.upsert_track(conn, t)
    db.upsert_features(
        conn, AudioFeatures(t.spotify_id, 120.0, "8A", source="getsongbpm")
    )
    conn.commit()

    enrich_tracks(conn, Resolver([KeylessSource(), NewSource()]), refresh=True)

    kept = db.all_features(conn)[t.spotify_id]
    assert kept.key_camelot == "8A"
    assert kept.bpm == 120.0
    assert kept.source == "getsongbpm"


def test_a_keyless_source_still_writes_when_there_is_nothing_to_lose(
    conn, track_factory
):
    t = track_factory(1)
    db.upsert_track(conn, t)
    conn.commit()

    enrich_tracks(conn, Resolver([KeylessSource()]))

    written = db.all_features(conn)[t.spotify_id]
    assert written.source == "keyless"
    assert written.key_camelot is None


# --------------------------------------------------------------------------
# --retry-incomplete
# --------------------------------------------------------------------------
#
# A row with a tempo and no key is not a miss — it was answered, just not
# fully. `--refresh` reaches it only by re-attempting the whole library, and
# `--retry-misses` does not reach it at all, so a source added later to supply
# keys had no affordable way to be pointed at the tracks it exists to fix.


class _Source:
    """Answers with whatever it was handed, and remembers being asked."""

    def __init__(self, name: str, priority: int, data: dict[str, AudioFeatures]):
        self.name = name
        self.priority = priority
        self.data = data
        self.calls: list[str] = []

    def lookup(self, track: Track) -> AudioFeatures | None:
        self.calls.append(track.spotify_id)
        return self.data.get(track.spotify_id)


def test_retry_incomplete_reaches_a_row_with_no_key(conn, track_factory):
    t = track_factory(1)
    db.upsert_track(conn, t)
    db.upsert_features(
        conn,
        AudioFeatures(spotify_id="t1", bpm=124.0, key_camelot=None, source="deezer"),
    )

    dsp = _Source("dsp", 40, {"t1": AudioFeatures("t1", 123.4, "8A", source="dsp")})
    stats = enrich_tracks(conn, Resolver([dsp]), [t], retry_incomplete=True)

    assert stats.already_cached == 0
    assert db.get_features(conn, "t1").key_camelot == "8A"


def test_retry_incomplete_leaves_a_sequenceable_row_alone(conn, track_factory):
    """The point of the flag is to be affordable: a complete row is not work."""
    t = track_factory(1)
    db.upsert_track(conn, t)
    db.upsert_features(
        conn,
        AudioFeatures(spotify_id="t1", bpm=128.0, key_camelot="8A", source="getsongbpm"),
    )

    dsp = _Source("dsp", 40, {"t1": AudioFeatures("t1", 127.9, "9A", source="dsp")})
    stats = enrich_tracks(conn, Resolver([dsp]), [t], retry_incomplete=True)

    assert (stats.already_cached, dsp.calls) == (1, [])
    assert db.get_features(conn, "t1").key_camelot == "8A"
