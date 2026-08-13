"""Response parsing, against the real 2026 payload shapes.

Captured from the live API on 2026-08-11. Two field renames were found that the
build brief does not document, both consistent with its ``/tracks`` ->
``/items`` endpoint rename:

  * the playlist count object: ``playlist.tracks`` -> ``playlist.items``
  * the item wrapper key in ``/playlists/{id}/items``: ``track`` -> ``item``
    (``/me/tracks`` still uses ``track``)
"""

from djset.spotify.client import _parse_item, playlist_count

TRACK_OBJECT = {
    "id": "4wVOKKEHUJxHCFFNUWDn0B",
    "uri": "spotify:track:4wVOKKEHUJxHCFFNUWDn0B",
    "type": "track",
    "name": "Como Es Que Se Hace",
    "duration_ms": 195_000,
    "is_local": False,
    "external_ids": {"isrc": "US6R22575850"},
    "artists": [{"id": "a1", "name": "Artist One"}, {"id": "a2", "name": "Artist Two"}],
    "album": {"name": "The Album"},
}


def test_playlist_items_wrapper_uses_item_key():
    """The shape /playlists/{id}/items actually returns."""
    wrapper = {"added_at": "2026-08-11T16:07:29Z", "is_local": False, "item": TRACK_OBJECT}
    parsed = _parse_item(wrapper)
    assert parsed is not None
    track, added_at = parsed
    assert track.spotify_id == "4wVOKKEHUJxHCFFNUWDn0B"
    assert track.uri == "spotify:track:4wVOKKEHUJxHCFFNUWDn0B"
    assert track.isrc == "US6R22575850"
    assert track.artist == "Artist One, Artist Two"
    assert track.artist_ids == ["a1", "a2"]
    assert track.album == "The Album"
    assert added_at == "2026-08-11T16:07:29Z"


def test_liked_songs_wrapper_still_uses_track_key():
    """/me/tracks was NOT renamed — it still nests under "track"."""
    wrapper = {"added_at": "2026-01-02T00:00:00Z", "track": TRACK_OBJECT}
    parsed = _parse_item(wrapper)
    assert parsed is not None
    assert parsed[0].spotify_id == "4wVOKKEHUJxHCFFNUWDn0B"
    assert parsed[1] == "2026-01-02T00:00:00Z"


def test_unplayable_entries_are_skipped():
    assert _parse_item(None) is None
    assert _parse_item({}) is None
    assert _parse_item({"item": None}) is None
    # Local files cannot be added to a playlist via the API.
    assert _parse_item({"item": {**TRACK_OBJECT, "is_local": True}}) is None
    # Missing id (unavailable in market).
    assert _parse_item({"item": {**TRACK_OBJECT, "id": None}}) is None
    # Podcast episodes.
    assert _parse_item({"item": {**TRACK_OBJECT, "type": "episode"}}) is None


def test_track_without_isrc_parses_with_none():
    parsed = _parse_item({"item": {**TRACK_OBJECT, "external_ids": {}}})
    assert parsed is not None and parsed[0].isrc is None


def test_playlist_count_reads_the_renamed_field():
    # What the API returns today.
    assert playlist_count({"items": {"href": "…", "total": 6}}) == 6
    # The pre-rename shape, kept as a fallback.
    assert playlist_count({"tracks": {"total": 42}}) == 42
    # Neither present -> 0, never a crash.
    assert playlist_count({}) == 0
    assert playlist_count({"items": None}) == 0
    assert playlist_count({"items": {"href": "…"}}) == 0
