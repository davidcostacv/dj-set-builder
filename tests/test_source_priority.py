"""Source priority resolution.

The build brief asks for this test now, before RekordboxXMLSource exists, so
that dropping a priority-10 source in later is provably a registration rather
than a refactor.
"""

from __future__ import annotations

from djset import db
from djset.enrichment.base import (
    MANUAL_SOURCE,
    Resolver,
    merged_with_key,
    set_manual_features,
)
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


# --------------------------------------------------------------------------
# a key from a source with less standing
# --------------------------------------------------------------------------
#
# Priority answers *whose tempo to believe*. It was being applied as *whose row
# wins*, which is a different question: Deezer outranks the DSP analyser and
# carries no harmonic data at all, so a Deezer row holding a tempo and no key
# discarded every DSP key — after paying ~3s of audio analysis to compute it.
# Measured on the real library: 670 tracks were unsequenceable for want of a
# value the app had already worked out and thrown away.


def _keyless(source: str, priority_note: str = "") -> AudioFeatures:
    return AudioFeatures(spotify_id="t1", bpm=124.0, key_camelot=None, source=source)


def test_a_key_fills_a_gap_a_more_trusted_source_left():
    existing = _keyless("deezer")
    candidate = AudioFeatures(spotify_id="t1", bpm=123.4, key_camelot="8A", source="dsp")

    merged = merged_with_key(existing, candidate)
    assert merged is not None
    assert merged.key_camelot == "8A"
    assert merged.key_source == "dsp"


def test_the_trusted_tempo_survives_the_merge():
    """The whole point of priority. Taking the key must not take the BPM."""
    existing = AudioFeatures(
        spotify_id="t1",
        bpm=124.0,
        key_camelot=None,
        energy=0.7,
        source="deezer",
        confidence=0.9,
    )
    merged = merged_with_key(
        existing, AudioFeatures(spotify_id="t1", bpm=61.9, key_camelot="8A", source="dsp")
    )
    assert merged.bpm == 124.0
    assert merged.source == "deezer"
    assert merged.energy == 0.7
    assert merged.confidence == 0.9


def test_an_existing_key_is_never_replaced():
    """This fills NULLs. A source with less standing does not get to argue."""
    existing = AudioFeatures(spotify_id="t1", bpm=124.0, key_camelot="5A", source="getsongbpm")
    candidate = AudioFeatures(spotify_id="t1", bpm=124.0, key_camelot="8A", source="dsp")
    assert merged_with_key(existing, candidate) is None


def test_a_candidate_with_no_key_contributes_nothing():
    assert merged_with_key(_keyless("deezer"), _keyless("dsp")) is None


def test_there_is_nothing_to_merge_into_when_the_row_is_absent():
    """A first result goes down the ordinary write path, not this one."""
    candidate = AudioFeatures(spotify_id="t1", bpm=124.0, key_camelot="8A", source="dsp")
    assert merged_with_key(None, candidate) is None


def test_a_hand_typed_row_is_not_amended():
    """`manual` means a person decided. Filling its gaps automatically would
    make that no longer true."""
    manual = AudioFeatures(spotify_id="t1", bpm=124.0, key_camelot=None, source=MANUAL_SOURCE)
    candidate = AudioFeatures(spotify_id="t1", bpm=124.0, key_camelot="8A", source="dsp")
    assert merged_with_key(manual, candidate) is None


def test_a_deezer_row_takes_the_dsp_key_end_to_end(conn, track_factory):
    """The 670-track case, through the runner and the database."""
    t = track_factory(1)
    db.upsert_track(conn, t)

    deezer = FakeSource("deezer", 30, {"t1": (124.0, None)})
    enrich_tracks(conn, Resolver([deezer]), [t])
    on_file = db.get_features(conn, "t1")
    assert (on_file.bpm, on_file.key_camelot) == (124.0, None)
    assert on_file.key_source is None      # no key, nowhere it came from

    dsp = FakeSource("dsp", 40, {"t1": (123.4, "8A")})
    stats = enrich_tracks(conn, Resolver([deezer, dsp]), [t], refresh=True)

    on_file = db.get_features(conn, "t1")
    assert on_file.key_camelot == "8A"     # the key crossed over
    assert on_file.bpm == 124.0            # the trusted tempo did not
    assert on_file.source == "deezer"
    assert on_file.key_source == "dsp"
    assert (stats.keys_merged, stats.written, stats.discarded) == (1, 0, 0)


def test_a_half_answer_does_not_end_the_search(conn, track_factory):
    """Deezer answers for a great many tracks and never has a key. Stopping
    there meant the analyser that did have one was never asked."""
    t = track_factory(1)
    db.upsert_track(conn, t)

    deezer = FakeSource("deezer", 30, {"t1": (124.0, None)})
    dsp = FakeSource("dsp", 40, {"t1": (123.4, "8A")})
    enrich_tracks(conn, Resolver([deezer, dsp]), [t])

    assert dsp.calls == ["t1"]             # it was asked at all
    on_file = db.get_features(conn, "t1")
    assert (on_file.bpm, on_file.source) == (124.0, "deezer")
    assert (on_file.key_camelot, on_file.key_source) == ("8A", "dsp")


def test_a_complete_answer_still_stops_at_the_first_source(conn, track_factory):
    """The saving that makes a full pass affordable: no needless lookups."""
    t = track_factory(1)
    db.upsert_track(conn, t)

    good = FakeSource("getsongbpm", 20, {"t1": (128.0, "8A")})
    dsp = FakeSource("dsp", 40, {"t1": (127.9, "9A")})
    enrich_tracks(conn, Resolver([good, dsp]), [t])

    assert dsp.calls == []                 # never reached
    assert db.get_features(conn, "t1").key_source == "getsongbpm"


def test_a_pass_that_stores_nothing_says_so(conn, track_factory):
    """`resolved` counted answers that were written nowhere, which is how a
    whole --refresh pass could compute thousands of results and keep none
    without anything in the output looking wrong."""
    t = track_factory(1)
    db.upsert_track(conn, t)
    good = FakeSource("getsongbpm", 20, {"t1": (128.0, "8A")})
    enrich_tracks(conn, Resolver([good]), [t])

    weaker = FakeSource("dsp", 40, {"t1": (128.0, "9A")})
    stats = enrich_tracks(conn, Resolver([good, weaker]), [t], refresh=True)
    assert stats.resolved == 1
    assert (stats.written, stats.keys_merged, stats.discarded) == (0, 0, 1)
    assert db.get_features(conn, "t1").key_camelot == "8A"


def test_key_provenance_is_backfilled_for_rows_written_before_the_column(
    conn, track_factory
):
    """Leaving these NULL would read as "unknown" when it is known: one source
    wrote the whole row."""
    db.upsert_track(conn, track_factory(1))
    conn.execute(
        "INSERT INTO audio_features (spotify_id, bpm, key_camelot, source) "
        "VALUES ('t1', 128.0, '8A', 'getsongbpm')"
    )
    conn.execute("UPDATE audio_features SET key_source = NULL")
    db._backfill_key_source(conn)
    assert db.get_features(conn, "t1").key_source == "getsongbpm"


def test_a_key_borrowed_from_the_analyser_is_still_an_estimate():
    """The row came from Deezer; the key did not. Whether it was *measured*
    follows the key, not the row, or a merged row would present an estimate as
    if a catalogue had supplied it."""
    merged = AudioFeatures(
        spotify_id="t1", bpm=124.0, key_camelot="8A",
        source="deezer", key_source="dsp",
    )
    assert merged.key_is_estimated is True

    looked_up = AudioFeatures(
        spotify_id="t2", bpm=128.0, key_camelot="8A",
        source="getsongbpm", key_source="getsongbpm",
    )
    assert looked_up.key_is_estimated is False


def test_the_analyser_and_the_flag_cannot_drift_apart():
    """`DSPSource.name` *is* the constant the flag tests against."""
    from djset.enrichment.dsp import DSPSource
    from djset.models import MEASURED_SOURCE

    assert DSPSource.name == MEASURED_SOURCE
