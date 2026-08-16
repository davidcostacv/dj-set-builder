"""Primary-artist resolution.

Regression found while reading the first coverage sample's miss list: the
normalizer recovered the primary artist by splitting the joined display string
on commas, so every artist whose own name contains a comma was truncated —
"Tyler, The Creator" looked up as "Tyler" and never matched. Spotify returns
artists as an array; that structure is now preserved instead of reparsed.
"""

from __future__ import annotations

import pytest

from djset import db
from djset.enrichment.normalize import normalize_artist
from djset.models import Track
from djset.spotify.client import _parse_item


def _track(names: list[str]) -> Track:
    return Track(
        spotify_id="x",
        uri="spotify:track:x",
        title="Song",
        artist=", ".join(names),
        artist_names=names,
    )


@pytest.mark.parametrize(
    "names,expected_primary,expected_norm",
    [
        (["Tyler, The Creator"], "Tyler, The Creator", "tyler the creator"),
        (["Alvaro Diaz", "Feid", "Tainy"], "Alvaro Diaz", "alvaro diaz"),
        (["Swedish House Mafia", "The Weeknd"], "Swedish House Mafia", "swedish house mafia"),
        (["Above & Beyond"], "Above & Beyond", "above beyond"),
        (["Earth, Wind & Fire"], "Earth, Wind & Fire", "earth wind fire"),
        (["Crosby, Stills & Nash"], "Crosby, Stills & Nash", "crosby stills nash"),
        (["The Weeknd"], "The Weeknd", "weeknd"),
    ],
)
def test_primary_artist_is_structural_not_parsed(names, expected_primary, expected_norm):
    t = _track(names)
    assert t.primary_artist == expected_primary
    assert normalize_artist(t.primary_artist) == expected_norm


def test_stylised_dollar_signs_become_s():
    assert normalize_artist("Joey Bada$$") == "joey badass"
    assert normalize_artist("$uicideboy$") == "suicideboys"
    assert normalize_artist("A$AP Rocky") == "asap rocky"


def test_a_real_currency_amount_is_still_punctuation():
    # No adjacent letter, so it is not a stylised glyph.
    assert normalize_artist("$100") == "100"


def test_parse_item_populates_artist_names():
    item = {
        "added_at": "2026-01-01T00:00:00Z",
        "item": {
            "id": "t1",
            "uri": "spotify:track:t1",
            "type": "track",
            "name": "Song",
            "artists": [{"id": "a1", "name": "Tyler, The Creator"}, {"id": "a2", "name": "Frank Ocean"}],
        },
    }
    track, _ = _parse_item(item)
    assert track.artist_names == ["Tyler, The Creator", "Frank Ocean"]
    assert track.artist == "Tyler, The Creator, Frank Ocean"
    # The display string is genuinely ambiguous; the array is not.
    assert track.primary_artist == "Tyler, The Creator"


def test_artist_names_survive_a_db_round_trip(conn):
    t = _track(["Tyler, The Creator", "Frank Ocean"])
    db.upsert_track(conn, t)
    got = db.all_tracks(conn)[0]
    assert got.artist_names == ["Tyler, The Creator", "Frank Ocean"]
    assert got.primary_artist == "Tyler, The Creator"


def test_rows_predating_the_column_still_load(conn):
    """Legacy fallback: no artist_names -> split the display string."""
    conn.execute(
        "INSERT INTO tracks (spotify_id, uri, title, artist, artist_ids, artist_names)"
        " VALUES ('old','spotify:track:old','Song','Daft Punk, Pharrell','[]', NULL)"
    )
    got = db.all_tracks(conn)[0]
    assert got.artist_names == ["Daft Punk", "Pharrell"]
    assert got.primary_artist == "Daft Punk"
