"""Energy is the one field with no universal unit, so it must be ranked.

BPM is beats per minute everywhere and key normalises to Camelot, but every
source measures energy its own way: GetSongBPM reports danceability on 0-100,
Essentia a detrended-fluctuation figure on roughly 0-3. Measured on this
library, GetSongBPM lands near 0.60-0.80 and Essentia near 0.38-0.47 for the
same kind of music.

That is not a cosmetic difference. A fall of more than ENERGY_DIP prunes the
edge out of the beam search, so mixing raw values would make every
cross-source transition look like a collapse and delete it -- the graph would
shrink as coverage improved.
"""

from __future__ import annotations

import pytest

from djset.models import AudioFeatures, Track
from djset.sequencing import (
    ENERGY_DIP,
    SequenceMode,
    SequenceOptions,
    build_set,
    comparable_energy,
)


def F(sid, energy, source="getsongbpm", bpm=128.0, key="8A"):
    return AudioFeatures(sid, bpm, key, source=source, energy=energy)


def ranks(features):
    return {sid: f.energy for sid, f in comparable_energy(features).items()}


# ---------------------------------------------------------------------------
# the scale problem
# ---------------------------------------------------------------------------


def test_sources_on_different_scales_become_comparable():
    """The real numbers from the two sources, which barely overlap."""
    features = {
        "g1": F("g1", 0.60), "g2": F("g2", 0.70), "g3": F("g3", 0.80),
        "a1": F("a1", 0.38, "acousticbrainz"),
        "a2": F("a2", 0.42, "acousticbrainz"),
        "a3": F("a3", 0.47, "acousticbrainz"),
    }
    r = ranks(features)

    # Each source spans the full 0..1 range on its own terms.
    assert (r["g1"], r["g2"], r["g3"]) == (0.0, 0.5, 1.0)
    assert (r["a1"], r["a2"], r["a3"]) == (0.0, 0.5, 1.0)

    # The quiet AcousticBrainz track is no longer below the loud GetSongBPM one.
    assert r["a3"] > r["g1"]


def test_raw_values_would_have_looked_like_a_collapse():
    """Guards the reason this exists: untranslated, the gap is 15x the dip
    threshold, so every cross-source edge would be pruned."""
    assert (0.38 - 0.80) < -ENERGY_DIP * 15


def test_a_cross_source_transition_survives_the_beam_search():
    tracks = [
        Track(f"t{i}", f"spotify:track:t{i}", f"Title {i}", f"Artist {i}",
              artist_names=[f"Artist {i}"])
        for i in range(1, 5)
    ]
    # Ascending within each source, but every AcousticBrainz value sits far
    # below every GetSongBPM value on the raw scale.
    features = {
        "t1": F("t1", 0.38, "acousticbrainz", bpm=128.0, key="8A"),
        "t2": F("t2", 0.47, "acousticbrainz", bpm=128.0, key="8A"),
        "t3": F("t3", 0.60, "getsongbpm", bpm=128.0, key="8A"),
        "t4": F("t4", 0.80, "getsongbpm", bpm=128.0, key="8A"),
    }
    result = build_set(
        tracks, features, SequenceOptions(mode=SequenceMode.BPM_KEY, target_tracks=4)
    )
    assert len(result.tracks) == 4


# ---------------------------------------------------------------------------
# ranking behaviour
# ---------------------------------------------------------------------------


def test_tied_values_share_a_rank():
    """GetSongBPM's danceability is integer derived, so ties are common.
    Splitting them would invent dips between tracks the source called equal."""
    features = {s: F(s, 0.7) for s in ("a", "b", "c")} | {"d": F("d", 0.9)}
    r = ranks(features)

    assert r["a"] == r["b"] == r["c"]
    assert r["d"] > r["a"]


def test_a_tied_block_takes_the_midrank():
    features = {"a": F("a", 0.1), "b": F("b", 0.5), "c": F("c", 0.5), "d": F("d", 0.9)}
    r = ranks(features)
    assert r["a"] == 0.0
    assert r["b"] == r["c"] == pytest.approx(0.5)  # midrank of positions 1 and 2
    assert r["d"] == 1.0


def test_ordering_within_a_source_is_preserved():
    features = {s: F(s, e) for s, e in [("a", 0.1), ("b", 0.9), ("c", 0.5)]}
    r = ranks(features)
    assert r["a"] < r["c"] < r["b"]


def test_a_lone_track_from_a_source_sits_at_the_midpoint():
    """With no distribution to sit in it must read as neither peak nor trough."""
    features = {"solo": F("solo", 0.93, "acousticbrainz")}
    assert ranks(features)["solo"] == 0.5


# ---------------------------------------------------------------------------
# things it must not do
# ---------------------------------------------------------------------------


def test_missing_energy_stays_missing():
    """None is safe -- the dip rule is skipped. Inventing a value is not."""
    features = {"a": F("a", None, "deezer"), "b": F("b", 0.7)}
    out = comparable_energy(features)
    assert out["a"].energy is None


def test_nothing_else_about_a_row_is_touched():
    features = {"a": F("a", 0.3, bpm=124.5, key="9B")}
    out = comparable_energy(features)["a"]
    assert (out.bpm, out.key_camelot, out.source, out.spotify_id) == (
        124.5, "9B", "getsongbpm", "a",
    )


def test_the_input_dict_is_not_mutated():
    features = {"a": F("a", 0.3), "b": F("b", 0.9)}
    comparable_energy(features)
    assert features["a"].energy == 0.3
    assert features["b"].energy == 0.9


def test_no_energy_anywhere_is_a_no_op():
    features = {"a": F("a", None), "b": F("b", None)}
    assert comparable_energy(features) == features


def test_empty_input():
    assert comparable_energy({}) == {}


# ---------------------------------------------------------------------------
# ranked over the pool, not over whatever dict was passed in
# ---------------------------------------------------------------------------


def _pool(n=12):
    """A playlist that is loud by the library's standards: 0.60-0.95 raw."""
    tracks = [
        Track(spotify_id=f"p{i}", uri=f"spotify:track:p{i}", title=f"p{i}", artist="A")
        for i in range(n)
    ]
    feats = {
        f"p{i}": F(f"p{i}", 0.60 + 0.35 * i / (n - 1), bpm=124.0 + (i % 3))
        for i in range(n)
    }
    return tracks, feats


def test_the_rest_of_the_library_does_not_move_a_set():
    """Regression: the web app passes every feature row it holds, and energy
    was ranked against all of them. A loud playlist then sat at the top of the
    library's scale, and the arc's quiet-to-loud ramp called almost every
    track too loud for the first two thirds of the night. The order of a set
    must depend on the tracks in it, not on what else is in the library."""
    tracks, pool_only = _pool()
    library = dict(pool_only)
    library.update({f"q{i}": F(f"q{i}", i / 100) for i in range(200)})  # quiet
    opts = SequenceOptions(mode=SequenceMode.BPM_KEY, use_all=True, energy_arc=True)

    alone = [t.spotify_id for t in build_set(tracks, pool_only, opts).tracks]
    in_library = [t.spotify_id for t in build_set(tracks, library, opts).tracks]

    assert in_library == alone
