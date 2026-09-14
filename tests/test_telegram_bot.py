"""Pure-function tests for the Telegram front end.

Only the argument-resolution helpers are tested here — everything else in
:mod:`djset.telegram.bot` is a thin call into engine code that already has
its own tests (sequencing, export, filtering), same reasoning as
`tests/test_web_api.py` for the FastAPI layer. Importing the module itself
must not require `python-telegram-bot`: every use of that package is a
lazy, function-local import, so these tests run without the optional
dependency installed.
"""

from djset.telegram.bot import _find_playlist, _format_playlists, _resolve_source

ROWS = [
    {"id": "p1", "name": "House Mix", "tracks": 42},
    {"id": "p2", "name": "Reggaeton 2024", "tracks": 88},
    {"id": "p3", "name": "house classics", "tracks": 10},
]


def test_resolve_source_all_means_the_whole_library():
    playlist_id, name, error = _resolve_source(ROWS, "all")
    assert (playlist_id, name, error) == (None, None, None)


def test_resolve_source_picks_by_one_based_index():
    playlist_id, name, error = _resolve_source(ROWS, "2")
    assert (playlist_id, name, error) == ("p2", "Reggaeton 2024", None)


def test_resolve_source_rejects_out_of_range_or_non_numeric():
    _, _, error = _resolve_source(ROWS, "0")
    assert error is not None
    _, _, error = _resolve_source(ROWS, "99")
    assert error is not None
    _, _, error = _resolve_source(ROWS, "house")
    assert error is not None


def test_find_playlist_by_exact_name():
    match, candidates = _find_playlist(ROWS, "Reggaeton 2024")
    assert match["id"] == "p2"
    assert candidates == []


def test_find_playlist_by_link_extracts_the_id():
    match, _ = _find_playlist(ROWS, "https://open.spotify.com/playlist/p1?si=abc123")
    assert match["id"] == "p1"
    match, _ = _find_playlist(ROWS, "spotify:playlist:p3")
    assert match["id"] == "p3"


def test_find_playlist_ambiguous_substring_lists_candidates():
    match, candidates = _find_playlist(ROWS, "house")
    assert match is None
    assert {c["id"] for c in candidates} == {"p1", "p3"}


def test_find_playlist_no_match_at_all():
    match, candidates = _find_playlist(ROWS, "does not exist")
    assert match is None
    assert candidates == []


def test_format_playlists_empty_points_at_sync():
    assert "/sync" in _format_playlists([])


def test_format_playlists_numbers_match_resolve_source():
    text = _format_playlists(ROWS)
    for i, row in enumerate(ROWS, 1):
        assert f"{i}. {row['name']}" in text
