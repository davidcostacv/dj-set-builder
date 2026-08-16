"""Genre filtering.

The hard requirement under test: no genres selected means NO FILTER — every
track eligible — and that must never collapse into "no tracks eligible" or
raise a validation error. Skipping step 2 is a normal path.
"""

from __future__ import annotations

import pytest

from djset.filtering import (
    UNKNOWN,
    bucket_of,
    canonical_genre,
    dedupe_by_isrc,
    filter_tracks,
    genre_index,
    sequenceable,
    summarize,
    track_genres,
)
from djset.models import AudioFeatures, Track

ALIASES = {
    "tech house": "house",
    "deep house": "house",
    "trap latino": "reggaeton",
    "urbano latino": "reggaeton",
}

ARTIST_GENRES = {
    "a_house": ["tech house", "deep house"],
    "a_reg": ["trap latino", "urbano latino"],
    "a_both": ["deep house", "urbano latino"],
    "a_rock": ["rock"],
    "a_bare": [],  # cached artist with no tags
}


def T(tid: str, *artist_ids: str, isrc: str | None = None) -> Track:
    return Track(
        spotify_id=tid,
        uri=f"spotify:track:{tid}",
        title=f"Title {tid}",
        artist="Artist",
        artist_ids=list(artist_ids),
        isrc=isrc,
    )


HOUSE = T("h1", "a_house")
REG = T("r1", "a_reg")
BOTH = T("b1", "a_both")
ROCK = T("k1", "a_rock")
BARE = T("u1", "a_bare")
NOARTIST = T("u2")

ALL = [HOUSE, REG, BOTH, ROCK, BARE, NOARTIST]

FEATURES = {
    "h1": AudioFeatures("h1", 128.0, "8A", source="getsongbpm"),
    "b1": AudioFeatures("b1", 124.0, "5A", source="getsongbpm"),
    "r1": AudioFeatures("r1", 96.0, None, source="getsongbpm"),  # no key
    "k1": AudioFeatures("k1", None, None, source="getsongbpm"),
}


# --------------------------------------------------------------------------
# the optional-filter contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize("selected", [None, set(), frozenset()])
def test_no_selection_means_no_filter(selected):
    got = filter_tracks(ALL, selected, ARTIST_GENRES, ALIASES)
    assert len(got) == len(ALL)


def test_no_selection_is_distinct_from_selecting_everything():
    """Unselected != all-selected. The counter must be able to tell them apart."""
    unfiltered = summarize(ALL, None, ARTIST_GENRES, ALIASES, FEATURES)
    every = {s.name for s in genre_index(ALL, ARTIST_GENRES, ALIASES, FEATURES)}
    all_selected = summarize(ALL, every, ARTIST_GENRES, ALIASES, FEATURES)

    assert unfiltered.total == all_selected.total  # same tracks…
    assert unfiltered.filtered is False  # …but not the same state
    assert all_selected.filtered is True
    assert "no genre filter" in unfiltered.label
    assert "no genre filter" not in all_selected.label


def test_empty_library_with_no_filter_is_not_an_error():
    assert filter_tracks([], None, ARTIST_GENRES, ALIASES) == []
    assert summarize([], None, ARTIST_GENRES, ALIASES).total == 0


# --------------------------------------------------------------------------
# genre resolution
# --------------------------------------------------------------------------


def test_aliases_collapse_to_canonical_names():
    assert canonical_genre("Tech House", ALIASES) == "house"
    assert canonical_genre("TRAP LATINO", ALIASES) == "reggaeton"
    # Unmapped tags pass through, lowercased.
    assert canonical_genre("Ambient", ALIASES) == "ambient"


def test_track_inherits_the_union_of_its_artists_tags():
    multi = T("m1", "a_house", "a_reg")
    assert track_genres(multi, ARTIST_GENRES, ALIASES) == {"house", "reggaeton"}


def test_duplicate_tags_collapse_to_one_genre():
    # tech house and deep house both alias to house.
    assert track_genres(HOUSE, ARTIST_GENRES, ALIASES) == {"house"}


def test_untagged_tracks_go_to_the_unknown_bucket():
    assert track_genres(BARE, ARTIST_GENRES, ALIASES) == set()
    assert bucket_of(BARE, ARTIST_GENRES, ALIASES) == {UNKNOWN}
    # A track whose artists were never fetched behaves the same way.
    assert bucket_of(NOARTIST, ARTIST_GENRES, ALIASES) == {UNKNOWN}


# --------------------------------------------------------------------------
# selection semantics
# --------------------------------------------------------------------------


def test_multiple_genres_are_or_not_and():
    got = filter_tracks(ALL, {"house", "rock"}, ARTIST_GENRES, ALIASES)
    assert {t.spotify_id for t in got} == {"h1", "b1", "k1"}


def test_a_multi_genre_track_matches_any_of_its_genres():
    assert BOTH in filter_tracks(ALL, {"house"}, ARTIST_GENRES, ALIASES)
    assert BOTH in filter_tracks(ALL, {"reggaeton"}, ARTIST_GENRES, ALIASES)


def test_unknown_can_be_included_or_excluded():
    included = filter_tracks(ALL, {"house", UNKNOWN}, ARTIST_GENRES, ALIASES)
    assert {t.spotify_id for t in included} == {"h1", "b1", "u1", "u2"}

    excluded = filter_tracks(ALL, {"house"}, ARTIST_GENRES, ALIASES)
    assert "u1" not in {t.spotify_id for t in excluded}


def test_selecting_a_genre_with_no_tracks_yields_nothing_without_erroring():
    assert filter_tracks(ALL, {"polka"}, ARTIST_GENRES, ALIASES) == []


# --------------------------------------------------------------------------
# the index shown in the checkbox list
# --------------------------------------------------------------------------


def test_index_is_built_from_the_current_selection_only():
    stats = genre_index([HOUSE], ARTIST_GENRES, ALIASES, FEATURES)
    assert [s.name for s in stats] == ["house"]  # not a global genre list


def test_index_sorted_by_track_count_descending():
    stats = genre_index(ALL, ARTIST_GENRES, ALIASES, FEATURES)
    counts = [s.track_count for s in stats]
    assert counts == sorted(counts, reverse=True)
    assert stats[0].name == "house"  # h1 + b1


def test_index_reports_enriched_counts_per_genre():
    stats = {s.name: s for s in genre_index(ALL, ARTIST_GENRES, ALIASES, FEATURES)}
    # house = h1 (bpm+key) and b1 (bpm+key)
    assert (stats["house"].track_count, stats["house"].enriched_count) == (2, 2)
    # reggaeton = r1 (bpm only, unusable) and b1 (usable)
    assert (stats["reggaeton"].track_count, stats["reggaeton"].enriched_count) == (2, 1)
    # rock = k1, no features at all
    assert (stats["rock"].track_count, stats["rock"].enriched_count) == (1, 0)


def test_unknown_appears_in_the_index():
    stats = {s.name: s for s in genre_index(ALL, ARTIST_GENRES, ALIASES, FEATURES)}
    assert stats[UNKNOWN].track_count == 2
    assert stats[UNKNOWN].is_unknown


def test_index_without_features_reports_zero_enriched():
    stats = genre_index(ALL, ARTIST_GENRES, ALIASES)
    assert all(s.enriched_count == 0 for s in stats)


# --------------------------------------------------------------------------
# the live counter
# --------------------------------------------------------------------------


def test_summary_counts_eligible_and_sequenceable_separately():
    s = summarize(ALL, {"house"}, ARTIST_GENRES, ALIASES, FEATURES)
    assert (s.total, s.enriched) == (2, 2)

    s2 = summarize(ALL, {"rock"}, ARTIST_GENRES, ALIASES, FEATURES)
    # The case the brief calls out: tracks present, none of them usable.
    assert (s2.total, s2.enriched) == (1, 0)
    assert "1 tracks eligible" in s2.label and "0 have BPM+key" in s2.label


def test_sequenceable_requires_both_bpm_and_key():
    got = {t.spotify_id for t in sequenceable(ALL, FEATURES)}
    assert got == {"h1", "b1"}  # r1 has bpm only, k1 has neither


# --------------------------------------------------------------------------
# dedupe
# --------------------------------------------------------------------------


def test_dedupe_by_isrc_keeps_first_occurrence():
    a = T("a", isrc="ISRC1")
    b = T("b", isrc="ISRC1")  # same recording, different release
    c = T("c", isrc="ISRC2")
    assert [t.spotify_id for t in dedupe_by_isrc([a, b, c])] == ["a", "c"]


def test_tracks_without_isrc_are_never_treated_as_duplicates():
    a, b = T("a"), T("b")
    assert len(dedupe_by_isrc([a, b])) == 2
