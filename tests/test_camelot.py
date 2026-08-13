from djset.camelot import (
    camelot_to_open_key,
    open_key_to_camelot,
    parse_camelot,
    to_camelot,
)

import pytest


@pytest.mark.parametrize(
    "key,expected",
    [
        # The twelve minors.
        ("Abm", "1A"), ("G#m", "1A"),
        ("Ebm", "2A"), ("D#m", "2A"),
        ("Bbm", "3A"), ("A#m", "3A"),
        ("Fm", "4A"),
        ("Cm", "5A"),
        ("Gm", "6A"),
        ("Dm", "7A"),
        ("Am", "8A"),
        ("Em", "9A"),
        ("Bm", "10A"),
        ("F#m", "11A"), ("Gbm", "11A"),
        ("C#m", "12A"), ("Dbm", "12A"),
        # The twelve majors.
        ("B", "1B"),
        ("F#", "2B"), ("Gb", "2B"),
        ("Db", "3B"), ("C#", "3B"),
        ("Ab", "4B"), ("G#", "4B"),
        ("Eb", "5B"), ("D#", "5B"),
        ("Bb", "6B"), ("A#", "6B"),
        ("F", "7B"),
        ("C", "8B"),
        ("G", "9B"),
        ("D", "10B"),
        ("A", "11B"),
        ("E", "12B"),
    ],
)
def test_note_names(key, expected):
    assert to_camelot(key) == expected


@pytest.mark.parametrize(
    "key,expected",
    [
        ("C minor", "5A"),
        ("c MINOR", "5A"),
        ("A major", "11B"),
        ("F# Minor", "11A"),
        ("Bb maj", "6B"),
        ("D min", "7A"),
        ("A♭m", "1A"),
        ("F♯m", "11A"),
        ("  Em  ", "9A"),
    ],
)
def test_worded_and_unicode_forms(key, expected):
    assert to_camelot(key) == expected


def test_camelot_passthrough():
    assert to_camelot("8A") == "8A"
    assert to_camelot("11b") == "11B"
    assert to_camelot("12 A") == "12A"


@pytest.mark.parametrize("bad", [None, "", "   ", "H", "Zm", "13A", "0A", "banana"])
def test_unparseable_returns_none_rather_than_guessing(bad):
    assert to_camelot(bad) is None


@pytest.mark.parametrize(
    "open_key,camelot",
    [("1d", "8B"), ("1m", "8A"), ("2d", "9B"), ("12d", "7B"), ("6m", "1A"), ("5d", "12B")],
)
def test_open_key_conversion(open_key, camelot):
    assert open_key_to_camelot(open_key) == camelot


def test_open_key_round_trip():
    for n in range(1, 13):
        for suffix in ("m", "d"):
            ok = f"{n}{suffix}"
            assert camelot_to_open_key(open_key_to_camelot(ok)) == ok


def test_open_key_agrees_with_note_names():
    # Open Key 1d is C major, which is Camelot 8B by the note-name table too.
    assert open_key_to_camelot("1d") == to_camelot("C")
    assert open_key_to_camelot("1m") == to_camelot("Am")


def test_all_24_codes_are_distinct():
    codes = set()
    for key in (
        "Abm Ebm Bbm Fm Cm Gm Dm Am Em Bm F#m C#m "
        "B F# Db Ab Eb Bb F C G D A E"
    ).split():
        codes.add(to_camelot(key))
    assert len(codes) == 24
    assert None not in codes


def test_parse_camelot():
    assert parse_camelot("8A") == (8, "A")
    assert parse_camelot("11b") == (11, "B")
    with pytest.raises(ValueError):
        parse_camelot("13A")
    with pytest.raises(ValueError):
        parse_camelot("nope")
