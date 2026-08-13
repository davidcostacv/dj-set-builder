"""GetSongBPM /search/ response parsing.

The documented ``type=artist`` response is a list under "search". The shape of
``type=both`` — which is what lookups actually use — is not documented, so the
extractor accepts the plausible variants and logs anything else. Reading an
unexpected shape as "no match" would silently zero out coverage for the entire
library while looking like a data problem.
"""

from __future__ import annotations

import logging

from djset.enrichment.getsongbpm import _extract_results

# Verbatim from the GetSongBPM docs, type=artist.
DOCUMENTED_ARTIST_RESPONSE = {
    "search": [
        {
            "id": "N8K",
            "name": "The Offspring",
            "uri": "https://getsongbpm.com/artist/the-offspring/N8K",
            "genres": ["punk", "rock"],
            "from": "US",
            "mbid": "23a03e33-a603-404e-bcbf-2c00159d7067",
            "similar": [{"id": "v9M", "name": "Green Day"}],
        }
    ]
}

SONG_ROW = {
    "id": "abc",
    "title": "Blinding Lights",
    "tempo": "171",
    "key_of": "F#m",
    "artist": {"name": "The Weeknd"},
}


def test_documented_list_shape():
    got = _extract_results(DOCUMENTED_ARTIST_RESPONSE)
    assert len(got) == 1
    assert got[0]["name"] == "The Offspring"


def test_no_result_object_is_an_empty_miss():
    assert _extract_results({"search": {"error": "no result"}}) == []


def test_nested_song_list_shape():
    assert _extract_results({"search": {"song": [SONG_ROW]}}) == [SONG_ROW]
    assert _extract_results({"search": {"songs": [SONG_ROW]}}) == [SONG_ROW]


def test_single_object_instead_of_a_list():
    assert _extract_results({"search": SONG_ROW}) == [SONG_ROW]


def test_missing_and_empty_payloads():
    assert _extract_results({}) == []
    assert _extract_results({"search": []}) == []
    assert _extract_results({"search": None}) == []


def test_non_dict_rows_are_filtered_out():
    assert _extract_results({"search": [SONG_ROW, "junk", None, 42]}) == [SONG_ROW]


def test_an_unknown_shape_is_logged_not_swallowed(caplog):
    with caplog.at_level(logging.WARNING):
        assert _extract_results({"search": {"totally": "unexpected"}}) == []
    assert any("Unrecognised" in r.message for r in caplog.records)


def test_an_unknown_type_is_logged(caplog):
    with caplog.at_level(logging.WARNING):
        assert _extract_results({"search": "a string"}) == []
    assert any("Unexpected" in r.message for r in caplog.records)
