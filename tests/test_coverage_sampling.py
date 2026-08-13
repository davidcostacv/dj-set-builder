"""Coverage must be measured over the tracks actually attempted.

Regression: with a 9,759-track library, enriching a 300-track sample and
dividing by the library size reported ~2% coverage and a bogus STOP verdict.
The percentage that decides the project has to have the right denominator.
"""

from __future__ import annotations

from djset import db
from djset.models import AudioFeatures
from djset.report import build_coverage


def _library(conn, track_factory, n: int) -> None:
    for i in range(1, n + 1):
        db.upsert_track(conn, track_factory(i))


def test_percentages_use_the_measured_set_not_the_library(conn, track_factory):
    _library(conn, track_factory, 1000)

    # Measure 10 tracks: 8 fully resolved, 2 missed.
    for i in range(1, 9):
        db.upsert_features(conn, AudioFeatures(f"t{i}", 128.0, "8A", source="getsongbpm"))
    for i in (9, 10):
        db.record_miss(conn, f"t{i}", "getsongbpm: no match")

    cov = build_coverage(conn)
    assert cov.total_tracks == 1000
    assert cov.measured == 10
    assert cov.with_both == 8
    assert cov.pct(cov.with_both) == 80.0
    assert cov.verdict.startswith("PROCEED")


def test_untouched_tracks_do_not_drag_the_number_down(conn, track_factory):
    _library(conn, track_factory, 500)
    db.upsert_features(conn, AudioFeatures("t1", 128.0, "8A", source="getsongbpm"))

    cov = build_coverage(conn)
    assert cov.measured == 1
    assert cov.pct(cov.with_both) == 100.0


def test_nothing_enriched_reports_no_data_rather_than_stop(conn, track_factory):
    _library(conn, track_factory, 100)
    cov = build_coverage(conn)
    assert cov.measured == 0
    assert cov.pct(cov.with_both) == 0.0
    assert cov.verdict.startswith("NO DATA")


def test_verdict_thresholds(conn, track_factory):
    _library(conn, track_factory, 10)
    # 7 of 10 resolved -> exactly the PROCEED boundary.
    for i in range(1, 8):
        db.upsert_features(conn, AudioFeatures(f"t{i}", 128.0, "8A", source="getsongbpm"))
    for i in (8, 9, 10):
        db.record_miss(conn, f"t{i}", "no match")
    assert build_coverage(conn).verdict.startswith("PROCEED")


def test_tune_band(conn, track_factory):
    _library(conn, track_factory, 10)
    for i in range(1, 6):
        db.upsert_features(conn, AudioFeatures(f"t{i}", 128.0, "8A", source="getsongbpm"))
    for i in range(6, 11):
        db.record_miss(conn, f"t{i}", "no match")
    assert build_coverage(conn).verdict.startswith("TUNE")


def test_partial_features_count_separately(conn, track_factory):
    _library(conn, track_factory, 4)
    db.upsert_features(conn, AudioFeatures("t1", 128.0, "8A", source="getsongbpm"))
    db.upsert_features(conn, AudioFeatures("t2", 124.0, None, source="getsongbpm"))
    db.upsert_features(conn, AudioFeatures("t3", None, "5A", source="getsongbpm"))
    db.record_miss(conn, "t4", "no match")

    cov = build_coverage(conn)
    assert (cov.measured, cov.with_bpm, cov.with_key, cov.with_both) == (4, 2, 2, 1)


# --------------------------------------------------------------------------
# sampling
# --------------------------------------------------------------------------


def test_sample_is_deterministic(conn, track_factory):
    _library(conn, track_factory, 200)
    a = [t.spotify_id for t in db.sample_tracks(conn, 30)]
    b = [t.spotify_id for t in db.sample_tracks(conn, 30)]
    assert a == b
    assert len(set(a)) == 30


def test_sample_is_not_just_the_first_n(conn, track_factory):
    _library(conn, track_factory, 200)
    sample = {t.spotify_id for t in db.sample_tracks(conn, 30)}
    first_30 = {t.spotify_id for t in db.all_tracks(conn)[:30]}
    assert sample != first_30


def test_sample_larger_than_library_returns_everything(conn, track_factory):
    _library(conn, track_factory, 5)
    assert len(db.sample_tracks(conn, 500)) == 5


def test_different_seeds_give_different_samples(conn, track_factory):
    _library(conn, track_factory, 200)
    a = {t.spotify_id for t in db.sample_tracks(conn, 30, seed=1)}
    b = {t.spotify_id for t in db.sample_tracks(conn, 30, seed=2)}
    assert a != b
