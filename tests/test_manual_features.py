"""Hand-typed BPM and key: the only route past the coverage ceiling.

No source resolves everything -- GetSongBPM's catalogue is thin on recent
releases, AcousticBrainz stopped taking submissions in 2022, and Deezer has no
harmonic data at all. For what falls through, the person who owns the library
usually knows the answer.
"""

from __future__ import annotations

import pytest

from djset import db
from djset.enrichment.base import MANUAL_SOURCE, Resolver, set_manual_features
from djset.models import AudioFeatures

pytest.importorskip("PySide6")

from djset.ui.manual_features import (  # noqa: E402
    MAX_BPM,
    MIN_BPM,
    parse_manual_input,
)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_reads_both_fields():
    p = parse_manual_input("128", "8A")
    assert (p.bpm, p.key_camelot) == (128.0, "8A")
    assert p.ok


@pytest.mark.parametrize(
    "text,expected",
    [("Am", "8A"), ("F#m", "11A"), ("Db", "3B"), ("C minor", "5A"), ("9m", "4A")],
)
def test_any_notation_the_converter_knows_is_accepted(text, expected):
    """Typing "Am" should not require knowing it is 8A."""
    assert parse_manual_input("", text).key_camelot == expected


def test_a_comma_decimal_is_accepted():
    """A Spanish-locale keyboard produces "128,5" as readily as "128.5"."""
    assert parse_manual_input("128,5", "").bpm == pytest.approx(128.5)


def test_blank_means_leave_alone_not_zero():
    p = parse_manual_input("", "8A")
    assert p.bpm is None and p.key_camelot == "8A" and p.ok

    p = parse_manual_input("128", "")
    assert p.key_camelot is None and p.bpm == 128.0 and p.ok


def test_both_blank_is_not_yet_a_change():
    """Not an error worth shouting about -- just nothing to save."""
    p = parse_manual_input("  ", "")
    assert not p.ok
    assert p.bpm_error is None and p.key_error is None


def test_a_bad_number_is_reported_without_losing_the_good_field():
    p = parse_manual_input("fast", "8A")
    assert p.bpm_error is not None
    assert p.key_camelot == "8A"  # the other field still parsed
    assert not p.ok


@pytest.mark.parametrize("bpm", [MIN_BPM - 1, MAX_BPM + 1, 0, 2026])
def test_out_of_range_tempos_are_refused(bpm):
    """Catches a typed year or a mistyped Camelot code."""
    p = parse_manual_input(str(bpm), "")
    assert p.bpm_error is not None
    assert p.bpm is None


@pytest.mark.parametrize("bpm", [MIN_BPM, MAX_BPM, 128])
def test_the_range_is_inclusive(bpm):
    assert parse_manual_input(str(bpm), "").bpm == pytest.approx(float(bpm))


def test_an_unparseable_key_is_refused_rather_than_guessed():
    p = parse_manual_input("", "H sharp")
    assert p.key_error is not None
    assert p.key_camelot is None
    assert not p.ok


# ---------------------------------------------------------------------------
# persistence: omitted fields must not erase what is known
# ---------------------------------------------------------------------------


def test_supplying_only_a_key_keeps_a_bpm_another_source_found(conn, track_factory):
    """The commonest reason to reach for this at all: Deezer supplies tempo but
    never a key. Replacing the row would leave the track as unusable as before."""
    t = track_factory(1)
    db.upsert_track(conn, t)
    db.upsert_features(conn, AudioFeatures(t.spotify_id, 122.5, None, source="deezer"))
    conn.commit()

    saved = set_manual_features(conn, t.spotify_id, None, "8A")

    assert saved.bpm == pytest.approx(122.5)
    assert saved.key_camelot == "8A"
    assert saved.is_usable
    assert db.all_features(conn)[t.spotify_id].bpm == pytest.approx(122.5)


def test_supplying_only_a_bpm_keeps_a_known_key(conn, track_factory):
    t = track_factory(1)
    db.upsert_track(conn, t)
    db.upsert_features(
        conn, AudioFeatures(t.spotify_id, 100.0, "5A", source="getsongbpm")
    )
    conn.commit()

    saved = set_manual_features(conn, t.spotify_id, 128.0, None)

    assert saved.bpm == 128.0  # the typed value wins
    assert saved.key_camelot == "5A"  # the untouched one survives


def test_typed_values_override_what_was_there(conn, track_factory):
    t = track_factory(1)
    db.upsert_track(conn, t)
    db.upsert_features(
        conn, AudioFeatures(t.spotify_id, 100.0, "5A", source="getsongbpm")
    )
    conn.commit()

    saved = set_manual_features(conn, t.spotify_id, 128.0, "8A")
    assert (saved.bpm, saved.key_camelot) == (128.0, "8A")


def test_writing_to_a_track_with_no_row_at_all(conn, track_factory):
    t = track_factory(1)
    db.upsert_track(conn, t)
    conn.commit()

    saved = set_manual_features(conn, t.spotify_id, 128.0, "8A")
    assert saved.source == MANUAL_SOURCE
    assert db.all_features(conn)[t.spotify_id].key_camelot == "8A"


def test_a_manual_row_clears_the_miss_so_it_stops_being_retried(conn, track_factory):
    t = track_factory(1)
    db.upsert_track(conn, t)
    db.record_miss(conn, t.spotify_id, "getsongbpm: no match")
    conn.commit()

    set_manual_features(conn, t.spotify_id, 128.0, "8A")
    assert t.spotify_id not in db.miss_reasons(conn)


def test_no_automated_source_can_overwrite_a_manual_row(conn, track_factory):
    """Priority 0 is the whole point -- a typed value is the last word."""
    t = track_factory(1)
    db.upsert_track(conn, t)
    conn.commit()
    manual = set_manual_features(conn, t.spotify_id, 128.0, "8A")

    class Best:
        name, priority = "rekordbox", 10

        def lookup(self, track):
            return AudioFeatures(track.spotify_id, 999.0, "1A", source=self.name)

    assert Resolver([Best()]).should_overwrite(manual, candidate_priority=10) is False
