"""Pure-function tests for the Telegram front end.

Only the argument-resolution helpers are tested here — everything else in
:mod:`djset.telegram.bot` is a thin call into engine code that already has
its own tests (sequencing, export, filtering), same reasoning as
`tests/test_web_api.py` for the FastAPI layer. Importing the module itself
must not require `python-telegram-bot`: every use of that package is a
lazy, function-local import, so these tests run without the optional
dependency installed.
"""

from djset.telegram.bot import (
    _chunk_lines,
    _find_playlist,
    _format_playlists,
    _playlist_lines,
    _resolve_source,
)

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


def test_chunk_lines_fits_everything_in_one_chunk_when_short():
    chunks = _chunk_lines(["a", "b", "c"])
    assert chunks == ["a\nb\nc"]


def test_chunk_lines_splits_a_library_too_big_for_one_telegram_message():
    # A real djset library can hold hundreds of playlists (274, per the
    # README) — enough that the numbered listing alone exceeds Telegram's
    # 4096-character cap on a single message.
    big_rows = [{"id": f"p{i}", "name": f"Playlist number {i}", "tracks": i} for i in range(400)]
    lines = _playlist_lines(big_rows)
    chunks = _chunk_lines(lines)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4096
    # No playlist's line was split across a chunk boundary or dropped.
    joined = "\n".join(chunks)
    for row in big_rows:
        assert f"{row['name']} — {row['tracks']} tracks" in joined


def test_chunk_lines_never_splits_a_single_line_even_over_the_limit():
    huge_line = "x" * 5000
    chunks = _chunk_lines(["short", huge_line, "also short"])
    assert huge_line in chunks
