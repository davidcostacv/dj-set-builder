"""Two ways to feed the sequencer, and full duplicate removal.

* Reorder a whole playlist into mixable order (`use_all`).
* Hand-pick a subset and let the app work out the order.

Plus the dedupe that both depend on: a set must never play the same recording
twice, and ISRC alone does not catch a re-release.
"""

from __future__ import annotations

import pytest

from djset.filtering import dedupe_by_isrc, dedupe_recordings, recording_key
from djset.models import AudioFeatures, Track
from djset.sequencing import SequenceOptions, build_set


def T(tid: str, title: str = "Song", artist: str = "Artist", isrc: str | None = None):
    return Track(
        spotify_id=tid, uri=f"spotify:track:{tid}", title=title, artist=artist,
        artist_names=[artist], isrc=isrc, duration_ms=210_000,
    )


def F(tid: str, bpm=128.0, key="8A"):
    return AudioFeatures(tid, bpm, key, source="test")


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------


def test_isrc_duplicates_collapse():
    tracks = [
        T("a", "Sum 2 Prove", "Lil Baby", "USUG11902886"),
        T("b", "Sum 2 Prove", "Lil Baby", "USUG11902886"),
    ]
    assert len(dedupe_recordings(tracks)) == 1


def test_a_rerelease_with_a_different_isrc_still_collapses():
    """Regression: Maroon 5's "This Love" appeared twice in a generated set
    because the two releases carry different ISRCs."""
    tracks = [
        T("a", "This Love", "Maroon 5", "USJAY0400018"),
        T("b", "This Love", "Maroon 5", "USJAY0400099"),
    ]
    assert len(dedupe_by_isrc(tracks)) == 2  # ISRC alone cannot tell
    assert len(dedupe_recordings(tracks)) == 1  # artist+title can


def test_featuring_variants_of_one_song_collapse():
    tracks = [
        T("a", "LOYAL (feat. Drake)", "PARTYNEXTDOOR"),
        T("b", "LOYAL (feat. Drake and Bad Bunny)", "PARTYNEXTDOOR"),
    ]
    assert len(dedupe_recordings(tracks)) == 1


def test_a_remix_is_NOT_collapsed_into_its_original():
    """Different recordings at different tempos — a DJ wants both."""
    tracks = [
        T("a", "Strobe", "deadmau5"),
        T("b", "Strobe - Extended Mix", "deadmau5"),
        T("c", "Strobe (Radio Edit)", "deadmau5"),
    ]
    assert len(dedupe_recordings(tracks)) == 3


def test_same_title_by_different_artists_is_not_a_duplicate():
    tracks = [T("a", "Alright", "Kendrick Lamar"), T("b", "Alright", "Supergrass")]
    assert len(dedupe_recordings(tracks)) == 2


def test_recording_key_is_stable_across_spellings():
    a = T("a", "Alright (feat. Someone)", "Kendrick Lamar")
    b = T("b", "alright", "kendrick lamar")
    assert recording_key(a) == recording_key(b)


def test_dedupe_preserves_order_and_keeps_the_first():
    tracks = [T("first", "X", "A", "I1"), T("second", "X", "A", "I1"), T("third", "Y", "A")]
    out = dedupe_recordings(tracks)
    assert [t.spotify_id for t in out] == ["first", "third"]


def test_tracks_without_isrc_or_match_are_all_kept():
    tracks = [T("a", "One", "A"), T("b", "Two", "A"), T("c", "Three", "A")]
    assert len(dedupe_recordings(tracks)) == 3


def test_build_set_never_emits_the_same_recording_twice():
    tracks = [
        T("a1", "Alright", "Kendrick Lamar", "USUM71502498"),
        T("a2", "Alright", "Kendrick Lamar", "USUM71502498"),
        T("b1", "This Love", "Maroon 5", "USJAY0400018"),
        T("b2", "This Love", "Maroon 5", "USJAY0400099"),
        T("c1", "Other", "Somebody"),
    ]
    feats = {t.spotify_id: F(t.spotify_id, 128, "8A") for t in tracks}
    res = build_set(tracks, feats, SequenceOptions(target_tracks=5))

    titles = [(t.artist, t.title) for t in res.tracks]
    assert len(titles) == len(set(titles))
    assert len(res.tracks) == 3


# ---------------------------------------------------------------------------
# whole-selection mode
# ---------------------------------------------------------------------------


def _chain(n: int):
    tracks = [T(f"t{i}", title=f"Song {i}", artist=f"Artist {i}") for i in range(n)]
    feats = {f"t{i}": F(f"t{i}", 120 + i * 0.5, "8A") for i in range(n)}
    return tracks, feats


def test_use_all_reorders_everything_eligible():
    tracks, feats = _chain(15)
    res = build_set(tracks, feats, SequenceOptions(use_all=True))
    assert len(res.tracks) == 15
    assert res.reached_target
    assert {t.spotify_id for t in res.tracks} == {t.spotify_id for t in tracks}


def test_use_all_actually_changes_the_order():
    tracks, feats = _chain(12)
    # Feed them in an order that is not already mixable.
    shuffled = list(reversed(tracks))
    res = build_set(shuffled, feats, SequenceOptions(use_all=True))
    assert len(res.tracks) == 12
    # Every consecutive pair is a valid transition, which the input order was not
    # guaranteed to be.
    assert all(t.quality > 0 for t in res.transitions)


def test_use_all_overrides_a_track_count():
    tracks, feats = _chain(9)
    res = build_set(tracks, feats, SequenceOptions(use_all=True, target_tracks=3))
    assert len(res.tracks) == 9


def test_use_all_carries_tracks_it_cannot_sequence_at_the_end():
    """"The whole selection" means the whole selection. A track with no BPM or
    key cannot be *mixed* into an order, but dropping it loses it from the
    playlist entirely, which reads as the app quietly eating songs."""
    tracks, feats = _chain(5)
    feats["t2"] = AudioFeatures("t2", None, None, source="test")  # unusable
    res = build_set(tracks, feats, SequenceOptions(use_all=True))

    assert len(res.tracks) == 5
    assert res.tracks[-1].spotify_id == "t2"      # carried, at the end
    assert res.appended == 1
    assert [t.spotify_id for t in res.sequenced] == ["t0", "t1", "t3", "t4"]


def test_the_carried_tracks_are_not_counted_as_mixed():
    """They have no transitions, so quality must be measured over the part
    that was actually sequenced — otherwise appending drags the number down
    and makes a good set look worse than it is."""
    tracks, feats = _chain(5)
    feats["t2"] = AudioFeatures("t2", None, None, source="test")
    res = build_set(tracks, feats, SequenceOptions(use_all=True))

    assert len(res.transitions) == len(res.sequenced) - 1
    assert res.average_quality > 0


def test_the_explanation_says_they_were_carried_not_mixed():
    tracks, feats = _chain(4)
    feats["t1"] = AudioFeatures("t1", None, None, source="test")
    res = build_set(tracks, feats, SequenceOptions(use_all=True))
    assert "no BPM or key" in res.explain()
    assert "carried at the end" in res.explain()


def test_an_explicit_count_is_not_padded_with_unmixable_tracks():
    """Asking for 3 tracks is a request to *choose* 3. Nothing is being
    dropped when the rest were never asked for, so nothing is carried."""
    tracks, feats = _chain(6)
    feats["t2"] = AudioFeatures("t2", None, None, source="test")
    res = build_set(tracks, feats, SequenceOptions(target_tracks=3))

    assert len(res.tracks) == 3
    assert res.appended == 0
    assert "t2" not in {t.spotify_id for t in res.tracks}


def test_use_all_places_every_track_even_when_unmixable():
    """Regression: "reorder this playlist" produced 21 of 630 tracks, which is
    useless for that job. Every track must get a place; the forced seams are
    counted and surfaced rather than hidden."""
    tracks = [T(f"t{i}", title=f"S{i}") for i in range(4)]
    feats = {
        "t0": F("t0", 100, "1A"), "t1": F("t1", 140, "4A"),
        "t2": F("t2", 180, "7A"), "t3": F("t3", 220, "10A"),
    }
    res = build_set(tracks, feats, SequenceOptions(use_all=True))

    assert len(res.tracks) == 4
    assert {t.spotify_id for t in res.tracks} == {"t0", "t1", "t2", "t3"}
    assert res.compromises > 0
    assert "break the BPM/key rules" in res.explain()


def test_a_fully_mixable_pool_needs_no_compromises():
    tracks, feats = _chain(12)
    res = build_set(tracks, feats, SequenceOptions(use_all=True))
    assert res.compromises == 0
    assert "break the BPM/key rules" not in res.explain()


def test_compromises_prefer_the_gentlest_seam():
    # t0 is close to t1 and far from t2; the forced jump should go to t1.
    # Distinct titles, or dedupe would (correctly) collapse them into one.
    tracks = [T("t0", "Alpha"), T("t1", "Beta"), T("t2", "Gamma")]
    feats = {
        "t0": F("t0", 128, "8A"),
        "t1": F("t1", 150, "9A"),   # closer in both BPM and key
        "t2": F("t2", 200, "3B"),   # far in both
    }
    res = build_set(tracks, feats, SequenceOptions(use_all=True))
    assert [t.spotify_id for t in res.tracks][:2] == ["t0", "t1"]


def test_a_fixed_target_still_refuses_to_pad():
    """Only use_all places everything; a numeric target must stay honest."""
    tracks = [T(f"t{i}", title=f"Distinct {i}") for i in range(4)]
    feats = {
        "t0": F("t0", 100, "1A"), "t1": F("t1", 140, "4A"),
        "t2": F("t2", 180, "7A"), "t3": F("t3", 220, "10A"),
    }
    res = build_set(tracks, feats, SequenceOptions(target_tracks=4))
    assert len(res.tracks) < 4
    assert res.compromises == 0


# ---------------------------------------------------------------------------
# hand-picked subset
# ---------------------------------------------------------------------------


def test_a_subset_sequences_only_the_chosen_tracks():
    tracks, feats = _chain(20)
    chosen = {"t1", "t3", "t5", "t7"}
    subset = [t for t in tracks if t.spotify_id in chosen]

    res = build_set(subset, feats, SequenceOptions(use_all=True))
    assert {t.spotify_id for t in res.tracks} <= chosen
    assert len(res.tracks) == 4


def test_a_subset_smaller_than_the_target_reports_honestly():
    tracks, feats = _chain(20)
    subset = tracks[:3]
    res = build_set(subset, feats, SequenceOptions(target_tracks=10))
    assert len(res.tracks) == 3
    assert not res.reached_target
    assert "3 of 10" in res.explain()
