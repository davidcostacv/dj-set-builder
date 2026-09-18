"""Second opinions: how the comparison is classified, and why the classes differ.

The resolver stops at the first hit, so nothing in the app ever notices two
sources disagreeing. This is the diagnostic that finds out whether that
matters, before any policy is built on the assumption that it does.
"""

from __future__ import annotations

import pytest

from djset.crosscheck import (
    AGREE,
    COMPATIBLE,
    CONFLICT,
    HALF_DOUBLE,
    Comparison,
    CrossCheck,
    classify_bpm,
    cross_check,
)
from djset.models import AudioFeatures, Track


def T(tid="t1", title="Sun Rising", artist="Someone"):
    return Track(tid, f"spotify:track:{tid}", title, artist, artist_names=[artist])


def F(bpm=128.0, key="8A", source="getsongbpm", tid="t1"):
    return AudioFeatures(tid, bpm, key, source=source)


def C(a, b):
    return Comparison(T(), a, b)


# ---------------------------------------------------------------------------
# BPM classification
# ---------------------------------------------------------------------------


def test_the_same_number_agrees():
    assert classify_bpm(128.0, 128.0) == AGREE
    assert classify_bpm(128.0, 128.5) == AGREE  # inside the tolerance


def test_half_and_double_are_their_own_class():
    """Not lumped in with conflicts: sequencing already tries b*2 and b/2, so
    64 against 128 still mixes correctly and damages nothing."""
    assert classify_bpm(128.0, 64.0) == HALF_DOUBLE
    assert classify_bpm(64.0, 128.0) == HALF_DOUBLE
    assert classify_bpm(174.0, 87.0) == HALF_DOUBLE


def test_a_small_difference_is_not_worth_flagging():
    """Measured on this library, four of eight apparent conflicts sat between
    2.2% and 3.9% -- outside a strict same-value tolerance but well inside the
    sequencer's, so nothing about them was broken. GetSongBPM also reports
    whole numbers where Deezer reports decimals, which manufactures a small
    disagreement on every track."""
    assert classify_bpm(88.0, 89.9) == COMPATIBLE
    assert classify_bpm(123.0, 126.0) == COMPATIBLE


def test_anything_else_is_a_real_conflict():
    """One source is simply wrong, and the transition will not beat match."""
    assert classify_bpm(128.0, 143.0) == CONFLICT
    assert classify_bpm(100.0, 150.0) == CONFLICT  # 3:2 is not half/double
    assert classify_bpm(56.0, 170.1) == CONFLICT  # nor is triple time


def test_a_missing_value_is_not_a_disagreement():
    assert classify_bpm(None, 128.0) is None
    assert classify_bpm(128.0, None) is None
    assert classify_bpm(None, None) is None


def test_the_two_thresholds_answer_different_questions():
    """"The sources printed different numbers" and "the sequencer will mix
    these wrongly" are separate, and only the second one costs anything."""
    assert classify_bpm(128.0, 131.0) == COMPATIBLE  # differs, still mixes
    assert classify_bpm(128.0, 131.0, same_tolerance=0.05) == AGREE
    assert classify_bpm(128.0, 131.0, mix_tolerance=0.01) == CONFLICT


# ---------------------------------------------------------------------------
# key classification
# ---------------------------------------------------------------------------


def test_an_identical_key_agrees():
    assert C(F(key="8A"), F(key="8A", source="acousticbrainz")).key_relation == AGREE


def test_an_adjacent_code_is_not_the_same_error_as_the_far_side_of_the_wheel():
    """A neighbour still mixes. The most common key-detection confusions --
    relative major/minor and the fifth -- land exactly there."""
    assert C(F(key="8A"), F(key="8B")).key_relation == COMPATIBLE  # relative
    assert C(F(key="8A"), F(key="9A")).key_relation == COMPATIBLE  # fifth
    assert C(F(key="8A"), F(key="2A")).key_relation == CONFLICT  # across the wheel


def test_a_missing_key_is_not_a_disagreement():
    assert C(F(key=None), F(key="8A")).key_relation is None
    assert C(F(key="8A"), F(key=None)).key_relation is None


# ---------------------------------------------------------------------------
# gathering
# ---------------------------------------------------------------------------


class Fake:
    def __init__(self, answers: dict, name="deezer"):
        self.answers = answers
        self.name = name
        self.asked: list[str] = []

    def lookup(self, track):
        self.asked.append(track.spotify_id)
        return self.answers.get(track.spotify_id)


def test_tracks_with_no_data_are_not_asked_about():
    """There is nothing to compare against, so a request would be wasted."""
    tracks = [T("t1"), T("t2")]
    src = Fake({"t1": F(tid="t1"), "t2": F(tid="t2")})

    result = cross_check(tracks, {"t1": F(tid="t1")}, src)

    assert result.asked == 1
    assert src.asked == ["t1"]


def test_a_source_is_not_compared_with_itself():
    tracks = [T("t1"), T("t2")]
    features = {"t1": F(source="deezer", tid="t1"), "t2": F(source="getsongbpm", tid="t2")}
    src = Fake({"t1": F(tid="t1"), "t2": F(tid="t2")}, name="deezer")

    result = cross_check(tracks, features, src, skip_source="deezer")

    assert src.asked == ["t2"]
    assert result.asked == 1


def test_a_challenger_with_nothing_to_say_is_counted_but_not_compared():
    result = cross_check([T("t1")], {"t1": F(tid="t1")}, Fake({"t1": None}))
    assert result.asked == 1
    assert result.answered == 0
    assert result.bpm_compared == 0


def test_one_broken_lookup_does_not_end_the_run():
    class Exploding(Fake):
        def lookup(self, track):
            if track.spotify_id == "t1":
                raise RuntimeError("boom")
            return super().lookup(track)

    tracks = [T("t1"), T("t2")]
    features = {"t1": F(tid="t1"), "t2": F(tid="t2")}

    result = cross_check(tracks, features, Exploding({"t2": F(tid="t2")}))
    assert result.answered == 1


def test_conflicts_are_kept_for_display_but_agreements_are_not():
    tracks = [T("t1"), T("t2"), T("t3")]
    features = {t.spotify_id: F(tid=t.spotify_id) for t in tracks}
    src = Fake(
        {
            "t1": F(128.0, "8A", "deezer", "t1"),  # agrees
            "t2": F(64.0, "8A", "deezer", "t2"),  # half time, still mixes
            "t3": F(143.0, "8A", "deezer", "t3"),  # a real conflict
        }
    )

    result = cross_check(tracks, features, src)

    assert result.bpm[AGREE] == 1
    assert result.bpm[HALF_DOUBLE] == 1
    assert result.bpm[CONFLICT] == 1
    assert [c.track.spotify_id for c in result.conflicts] == ["t3"]


def test_the_ratio_is_shown_because_the_pattern_is_the_point():
    """A clean 1.50 or 3.00 is a tempo-reading ambiguity, not a bad match, and
    both turned up in the real sample."""
    assert "3.04" in C(F(56.0), F(170.1, source="deezer")).describe()
    assert "1.50" in C(F(111.8), F(168.1, source="deezer")).describe()


def test_the_report_states_that_half_double_is_harmless():
    """A reader seeing 30% "disagreement" should not conclude the data is bad
    when the sequencer already handles that case."""
    result = CrossCheck()
    result.add(C(F(128.0), F(64.0, source="deezer")))
    text = result.report("deezer")

    assert HALF_DOUBLE in text
    assert "b*2" in text  # says why it does not matter


def test_an_empty_run_reports_rather_than_dividing_by_zero():
    text = CrossCheck().report("deezer")
    assert "nothing to compare" in text


# ---------------------------------------------------------------------------
# the tolerance has to actually reach the classification
# ---------------------------------------------------------------------------
#
# Regression: --tolerance was accepted by the parser and described in --help,
# but cmd_crosscheck never read it and cross_check() did not take one. A 9000x
# change in the flag produced byte-identical output. Tests that only checked
# classify_bpm directly could not see it, because the break was in the wiring.


def test_the_tolerance_reaches_the_comparison():
    lenient = Comparison(T(), F(128.0), F(140.0, source="deezer"), 0.15)
    strict = Comparison(T(), F(128.0), F(140.0, source="deezer"), 0.01)

    assert lenient.bpm_relation == COMPATIBLE
    assert strict.bpm_relation == CONFLICT


def test_cross_check_threads_the_tolerance_all_the_way_down():
    """End to end through the gathering function, which is where it was lost."""
    tracks = [T("t1")]
    features = {"t1": F(128.0, tid="t1")}
    answers = {"t1": F(140.0, "8A", "deezer", "t1")}

    strict = cross_check(tracks, features, Fake(dict(answers)), mix_tolerance=0.01)
    lenient = cross_check(tracks, features, Fake(dict(answers)), mix_tolerance=0.15)

    assert strict.bpm[CONFLICT] == 1
    assert lenient.bpm[CONFLICT] == 0
    assert lenient.bpm[COMPATIBLE] == 1


def test_a_stricter_tolerance_never_reports_fewer_conflicts():
    """The property that makes the flag meaningful at all."""
    tracks = [T(f"t{i}") for i in range(6)]
    features = {t.spotify_id: F(120.0, tid=t.spotify_id) for t in tracks}
    answers = {
        f"t{i}": F(120.0 + i * 2.0, "8A", "deezer", f"t{i}") for i in range(6)
    }

    counts = [
        cross_check(tracks, features, Fake(dict(answers)), mix_tolerance=t).bpm[CONFLICT]
        for t in (0.01, 0.03, 0.06, 0.12)
    ]
    assert counts == sorted(counts, reverse=True), counts


def test_the_report_states_the_tolerance_it_judged_at():
    """Otherwise the conflict count is uninterpretable — the same data gives a
    different number at a different setting."""
    result = cross_check([T("t1")], {"t1": F(tid="t1")},
                         Fake({"t1": F(128.0, "8A", "deezer", "t1")}),
                         mix_tolerance=0.09)
    assert "9%" in result.report("deezer")
