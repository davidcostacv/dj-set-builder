"""Sequencing: compatibility rules, graph construction, and path finding."""

from __future__ import annotations

import pytest

from djset.models import AudioFeatures, Track
from djset.sequencing import (
    LimitingFactor,
    SequenceMode,
    SequenceOptions,
    TrackGraph,
    bpm_compatible,
    bpm_distance,
    bpm_ratio,
    build_set,
    key_compatible,
    key_distance,
    transition,
)

# ---------------------------------------------------------------------------
# key compatibility
# ---------------------------------------------------------------------------


def test_same_key_is_distance_zero():
    assert key_distance("8A", "8A") == 0


@pytest.mark.parametrize("a,b", [("8A", "9A"), ("8A", "7A"), ("12A", "1A"), ("1A", "12A")])
def test_adjacent_on_the_wheel_wraps_around(a, b):
    assert key_distance(a, b) == 1


def test_relative_major_minor():
    assert key_distance("8A", "8B") == 1
    assert key_distance("8B", "8A") == 1


@pytest.mark.parametrize("a,b", [("8A", "10A"), ("8A", "9B"), ("8A", "2A")])
def test_incompatible_moves_return_none(a, b):
    assert key_distance(a, b) is None
    assert not key_compatible(a, b)


def test_energy_boost_is_opt_in():
    # +7 on the number, same letter: 8A -> 3A
    assert key_distance("8A", "3A") is None
    assert key_distance("8A", "3A", energy_boost=True) == 2
    assert key_compatible("8A", "3A", energy_boost=True)


def test_energy_boost_wraps():
    assert key_distance("10A", "5A", energy_boost=True) == 2


def test_malformed_codes_are_incompatible_not_crashes():
    assert key_distance("banana", "8A") is None
    assert key_distance("8A", "13A") is None


# ---------------------------------------------------------------------------
# BPM compatibility
# ---------------------------------------------------------------------------


def test_bpm_ratio_uses_the_smaller_value_as_denominator():
    assert bpm_ratio(100, 106) == pytest.approx(0.06)
    assert bpm_ratio(106, 100) == pytest.approx(0.06)


def test_within_and_outside_default_tolerance():
    assert bpm_compatible(128, 130)
    assert bpm_compatible(100, 106)  # exactly 6%
    assert not bpm_compatible(100, 110)


def test_tolerance_is_adjustable():
    assert not bpm_compatible(100, 110, tolerance=0.06)
    assert bpm_compatible(100, 110, tolerance=0.12)
    assert not bpm_compatible(128, 130, tolerance=0.005)


def test_half_and_double_time():
    # The case the brief names: 70 should match 140.
    assert bpm_compatible(70, 140)
    assert bpm_compatible(140, 70)
    assert bpm_compatible(75, 145, tolerance=0.06)


def test_half_double_can_be_disabled():
    assert not bpm_compatible(70, 140, half_double=False)
    assert bpm_compatible(70, 140, half_double=True)


def test_bpm_distance_returns_the_best_available_match():
    # 140 vs 70: direct is way off, double-time is exact.
    assert bpm_distance(140, 70) == pytest.approx(0.0)
    assert bpm_distance(100, 110) is None


def test_zero_and_negative_bpm_do_not_divide_by_zero():
    assert bpm_ratio(0, 120) == float("inf")
    assert not bpm_compatible(0, 120)


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------


def F(tid, bpm=128.0, key="8A", energy=0.5):
    return AudioFeatures(tid, bpm, key, energy=energy, source="test")


def test_mode_selects_which_predicates_apply():
    a, b = F("a", 128, "8A"), F("b", 128, "2A")  # same bpm, incompatible key
    assert transition(a, b, SequenceOptions(mode=SequenceMode.BPM)) is not None
    assert transition(a, b, SequenceOptions(mode=SequenceMode.KEY)) is None
    assert transition(a, b, SequenceOptions(mode=SequenceMode.BPM_KEY)) is None


def test_key_mode_ignores_bpm_entirely():
    a, b = F("a", 90, "8A"), F("b", 175, "8A")
    assert transition(a, b, SequenceOptions(mode=SequenceMode.KEY)) is not None


def test_missing_data_blocks_only_the_relevant_mode():
    a, b = F("a", 128, None), F("b", 130, None)
    assert transition(a, b, SequenceOptions(mode=SequenceMode.BPM)) is not None
    assert transition(a, b, SequenceOptions(mode=SequenceMode.KEY)) is None


def test_closer_matches_score_higher():
    opts = SequenceOptions(mode=SequenceMode.BPM_KEY)
    tight = transition(F("a", 128, "8A"), F("b", 128, "8A"), opts)
    loose = transition(F("a", 128, "8A"), F("b", 134, "9A"), opts)
    assert tight.quality > loose.quality
    assert tight.label == "excellent"


def test_transition_reports_energy_delta():
    opts = SequenceOptions(mode=SequenceMode.BPM)
    tr = transition(F("a", 128, "8A", energy=0.4), F("b", 129, "8A", energy=0.7), opts)
    assert tr.energy_delta == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# graph + search
# ---------------------------------------------------------------------------


def _track(tid: str, dur: int = 210_000, isrc: str | None = None) -> Track:
    return Track(
        spotify_id=tid, uri=f"spotify:track:{tid}", title=tid, artist="A",
        duration_ms=dur, isrc=isrc,
    )


def _chain(n: int, bpm0: float = 120.0, key: str = "8A"):
    """n tracks each 1 BPM apart in a single key — a guaranteed long path."""
    tracks = [_track(f"t{i}") for i in range(n)]
    feats = {f"t{i}": F(f"t{i}", bpm0 + i * 1.0, key, energy=0.5) for i in range(n)}
    return tracks, feats


def test_graph_excludes_tracks_missing_required_data():
    tracks = [_track("a"), _track("b"), _track("c")]
    feats = {
        "a": F("a", 128, "8A"),
        "b": F("b", None, "8A"),   # no bpm
        "c": F("c", 128, None),    # no key
    }
    g = TrackGraph(tracks, feats, SequenceOptions(mode=SequenceMode.BPM_KEY))
    assert [t.spotify_id for t in g.tracks] == ["a"]

    g2 = TrackGraph(tracks, feats, SequenceOptions(mode=SequenceMode.KEY))
    assert {t.spotify_id for t in g2.tracks} == {"a", "b"}


def test_builds_a_set_of_the_requested_length():
    tracks, feats = _chain(30)
    res = build_set(tracks, feats, SequenceOptions(target_tracks=10))
    assert len(res.tracks) == 10
    assert res.reached_target
    assert len(res.transitions) == 9
    assert res.average_quality > 0


def test_the_same_recording_never_appears_twice():
    """Regression, straight from a real generated set: Kendrick Lamar's
    "Alright" and Lil Baby's "Sum 2 Prove" each appeared twice. Different
    Spotify ids (album cut vs single) but identical ISRCs, so per-id
    uniqueness was not enough."""
    tracks = [
        Track(spotify_id="a1", uri="spotify:track:a1", title="Alright",
              artist="Kendrick Lamar", isrc="USUM71502498", duration_ms=210_000),
        Track(spotify_id="a2", uri="spotify:track:a2", title="Alright",
              artist="Kendrick Lamar", isrc="USUM71502498", duration_ms=210_000),
        Track(spotify_id="b1", uri="spotify:track:b1", title="Sum 2 Prove",
              artist="Lil Baby", isrc="USUG11902886", duration_ms=210_000),
        Track(spotify_id="b2", uri="spotify:track:b2", title="Sum 2 Prove",
              artist="Lil Baby", isrc="USUG11902886", duration_ms=210_000),
        _track("c1"),
    ]
    feats = {
        "a1": F("a1", 112, "9B"), "a2": F("a2", 112, "9B"),
        "b1": F("b1", 123, "9A"), "b2": F("b2", 123, "9A"),
        "c1": F("c1", 120, "9A"),
    }
    res = build_set(tracks, feats, SequenceOptions(target_tracks=5))

    isrcs = [t.isrc for t in res.tracks if t.isrc]
    assert len(isrcs) == len(set(isrcs))
    assert len(res.tracks) <= 3  # five inputs, but only three distinct recordings


def test_tracks_without_isrc_are_still_usable():
    tracks, feats = _chain(10)  # no ISRCs at all
    res = build_set(tracks, feats, SequenceOptions(target_tracks=6))
    assert len(res.tracks) == 6


def test_no_track_repeats_within_a_set():
    tracks, feats = _chain(30)
    res = build_set(tracks, feats, SequenceOptions(target_tracks=15))
    ids = [t.spotify_id for t in res.tracks]
    assert len(ids) == len(set(ids))


def test_every_consecutive_pair_actually_satisfies_the_predicates():
    tracks, feats = _chain(40)
    opts = SequenceOptions(mode=SequenceMode.BPM_KEY, target_tracks=12)
    res = build_set(tracks, feats, opts)
    for a, b in zip(res.tracks, res.tracks[1:]):
        assert transition(feats[a.spotify_id], feats[b.spotify_id], opts) is not None


def test_a_short_path_reports_the_limiting_factor_and_does_not_pad():
    # Four mutually incompatible tracks: no path longer than 1.
    tracks = [_track(f"t{i}") for i in range(4)]
    feats = {
        "t0": F("t0", 100, "1A"),
        "t1": F("t1", 140, "4A"),
        "t2": F("t2", 180, "7A"),
        "t3": F("t3", 220, "10A"),
    }
    res = build_set(tracks, feats, SequenceOptions(target_tracks=10))
    assert not res.reached_target
    assert len(res.tracks) < 10
    assert res.limiting_factor is not LimitingFactor.NONE
    assert "limiting factor" in res.explain()
    assert str(res.requested) in res.explain()


def test_an_empty_pool_blames_coverage():
    res = build_set([], {}, SequenceOptions(target_tracks=5))
    assert res.tracks == []
    assert res.limiting_factor is LimitingFactor.COVERAGE


def test_a_pool_emptied_by_the_genre_filter_blames_the_filter():
    res = build_set([], {}, SequenceOptions(target_tracks=5), eligible_before_filter=500)
    assert res.limiting_factor is LimitingFactor.GENRE_FILTER


def test_sparse_coverage_is_not_blamed_on_the_genre_filter():
    """Regression: a big unfiltered library with few enriched tracks was
    reported as 'the genre filter' even when no filter had been applied."""
    tracks, feats = _chain(20)
    # 20 eligible tracks, but the pool they came from was 5,000 — i.e. no
    # genre filter was applied, the library is simply mostly un-enriched.
    res = build_set(
        tracks, feats, SequenceOptions(target_tracks=50), eligible_before_filter=20
    )
    assert not res.reached_target
    assert res.limiting_factor is LimitingFactor.COVERAGE
    assert "genre filter" not in res.explain()


def test_a_genuine_genre_filter_is_still_detected():
    tracks, feats = _chain(20)
    # 20 eligible out of a 5,000-track pool: the filter really did the cutting.
    res = build_set(
        tracks, feats, SequenceOptions(target_tracks=50), eligible_before_filter=5000
    )
    assert res.limiting_factor is LimitingFactor.GENRE_FILTER


def test_start_track_is_honoured():
    tracks, feats = _chain(30)
    res = build_set(
        tracks, feats, SequenceOptions(target_tracks=8, start_track_id="t5")
    )
    assert res.tracks[0].spotify_id == "t5"


def test_target_by_minutes_converts_to_a_track_count():
    tracks, feats = _chain(40, )  # 3.5 min each
    res = build_set(tracks, feats, SequenceOptions(target_minutes=35.0))
    assert len(res.tracks) == 10
    assert res.total_duration_ms == 10 * 210_000


def test_half_double_widens_the_graph():
    tracks = [_track("a"), _track("b")]
    feats = {"a": F("a", 70, "8A"), "b": F("b", 140, "8A")}

    joined = build_set(tracks, feats, SequenceOptions(target_tracks=2, half_double=True))
    assert len(joined.tracks) == 2

    split = build_set(tracks, feats, SequenceOptions(target_tracks=2, half_double=False))
    assert len(split.tracks) == 1


def test_energy_boost_unlocks_otherwise_dead_transitions():
    tracks = [_track("a"), _track("b")]
    feats = {"a": F("a", 128, "8A"), "b": F("b", 128, "3A")}  # +7 move

    without = build_set(tracks, feats, SequenceOptions(target_tracks=2))
    assert len(without.tracks) == 1

    with_boost = build_set(
        tracks, feats, SequenceOptions(target_tracks=2, energy_boost=True)
    )
    assert len(with_boost.tracks) == 2


def test_key_only_mode_ignores_wild_bpm_swings():
    tracks = [_track("a"), _track("b")]
    feats = {"a": F("a", 90, "8A"), "b": F("b", 174, "9A")}
    res = build_set(tracks, feats, SequenceOptions(mode=SequenceMode.KEY, target_tracks=2))
    assert len(res.tracks) == 2


def test_result_reports_duration_and_quality():
    tracks, feats = _chain(20)
    res = build_set(tracks, feats, SequenceOptions(target_tracks=5))
    assert res.total_duration_ms == 5 * 210_000
    assert 0.0 <= res.average_quality <= 1.0
    assert "average transition quality" in res.explain()


def test_scales_to_a_realistic_pool():
    """2,000 tracks is the shape of the real library; must not blow up."""
    import random

    rng = random.Random(7)
    keys = [f"{n}{l}" for n in range(1, 13) for l in "AB"]
    tracks = [_track(f"t{i}") for i in range(2000)]
    feats = {
        f"t{i}": F(f"t{i}", rng.uniform(90, 150), rng.choice(keys), rng.random())
        for i in range(2000)
    }
    res = build_set(tracks, feats, SequenceOptions(target_tracks=25))
    assert len(res.tracks) == 25
    ids = [t.spotify_id for t in res.tracks]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# an original and its own remix must not sit next to each other
# ---------------------------------------------------------------------------


def _versions():
    """Two versions of one song plus a filler, all mutually compatible."""
    tracks = [
        Track("a", "spotify:track:a", "We Are The People", "Empire Of The Sun",
              artist_names=["Empire Of The Sun"], duration_ms=200_000),
        Track("b", "spotify:track:b", "We Are The People - ARTBAT Remix",
              "Empire Of The Sun, ARTBAT",
              artist_names=["Empire Of The Sun", "ARTBAT"], duration_ms=200_000),
        Track("c", "spotify:track:c", "Something Else", "Another Band",
              artist_names=["Another Band"], duration_ms=200_000),
    ]
    feats = {
        t.spotify_id: AudioFeatures(t.spotify_id, 123.0, "9A", source="test")
        for t in tracks
    }
    return tracks, feats


def test_the_two_versions_are_recognised_as_one_song():
    from djset.filtering import song_family

    tracks, _ = _versions()
    assert song_family(tracks[0]) == song_family(tracks[1])
    assert song_family(tracks[0]) != song_family(tracks[2])


def test_a_remix_never_follows_its_own_original():
    """Observed in a real 18-track set: rows 1 and 2 were "We Are The People"
    and its ARTBAT remix, back to back. Dedupe keeps both on purpose — a remix
    is a different recording — but consecutively it is the same song twice."""
    tracks, feats = _versions()
    result = build_set(
        tracks, feats, SequenceOptions(mode=SequenceMode.BPM_KEY, target_tracks=3)
    )

    ids = [t.spotify_id for t in result.tracks]
    for first, second in zip(ids, ids[1:]):
        assert {first, second} != {"a", "b"}, f"versions adjacent in {ids}"


def test_both_versions_are_still_eligible():
    """The rule separates them; it must not drop either."""
    tracks, feats = _versions()
    result = build_set(
        tracks, feats,
        SequenceOptions(mode=SequenceMode.BPM_KEY, use_all=True),
    )
    assert len(result.tracks) == 3


def test_reorder_everything_also_keeps_them_apart():
    tracks, feats = _versions()
    result = build_set(
        tracks, feats, SequenceOptions(mode=SequenceMode.BPM_KEY, use_all=True)
    )
    ids = [t.spotify_id for t in result.tracks]
    for first, second in zip(ids, ids[1:]):
        assert {first, second} != {"a", "b"}, f"versions adjacent in {ids}"


def test_two_versions_alone_are_still_both_placed():
    """With nothing to separate them the set must not silently lose one."""
    tracks, feats = _versions()
    pair = tracks[:2]
    result = build_set(
        pair, {k: v for k, v in feats.items() if k in ("a", "b")},
        SequenceOptions(mode=SequenceMode.BPM_KEY, use_all=True),
    )
    assert len(result.tracks) == 2
