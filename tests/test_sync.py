"""Sync resilience.

Regression: a single 403 aborted a 272-playlist run. In development mode
Spotify returns 403 for any playlist the app owner does not own, so followed
playlists failing is the normal case, not an exceptional one.
"""

from __future__ import annotations

import pytest

from djset import db
from djset.models import LIKED_SONGS_ID, PlaylistRef, Track
from djset.net import HttpError
from djset.spotify.sync import sync_playlists


def _track(n: int) -> tuple[Track, str]:
    t = Track(
        spotify_id=f"t{n}",
        uri=f"spotify:track:t{n}",
        title=f"Track {n}",
        artist=f"Artist {n}",
        artist_ids=[f"a{n}"],
    )
    return t, "2026-01-01T00:00:00Z"


class FakeClient:
    """Minimal stand-in for SpotifyClient — no network, no auth."""

    def __init__(self, playlists: list[PlaylistRef], forbidden: set[str] = frozenset()):
        self._playlists = playlists
        self._forbidden = set(forbidden)
        self.read: list[str] = []

    def playlists(self) -> list[PlaylistRef]:
        return list(self._playlists)

    def playlist_items(self, playlist_id: str):
        self.read.append(playlist_id)
        if playlist_id in self._forbidden:
            raise HttpError(403, f"/playlists/{playlist_id}/items", "Forbidden")
        yield _track(int(playlist_id[1:]))

    def liked_songs(self):
        yield _track(99)


def test_a_forbidden_playlist_does_not_abort_the_run(conn):
    pls = [PlaylistRef(f"p{i}", f"Playlist {i}", f"snap{i}") for i in range(1, 6)]
    client = FakeClient(pls, forbidden={"p2", "p4"})

    result = sync_playlists(conn, client, include_liked=False)

    # Every playlist was attempted, not just the ones before the first failure.
    assert client.read == ["p1", "p2", "p3", "p4", "p5"]
    assert set(result.counts) == {"p1", "p3", "p5"}
    assert [name for name, _ in result.unreadable] == ["Playlist 2", "Playlist 4"]
    assert "403" in result.unreadable[0][1]
    assert "not owned by you" in result.unreadable[0][1]


def test_readable_playlists_are_fully_persisted_despite_failures(conn):
    pls = [PlaylistRef(f"p{i}", f"Playlist {i}", f"snap{i}") for i in range(1, 4)]
    sync_playlists(conn, FakeClient(pls, forbidden={"p2"}), include_liked=False)

    assert {t.spotify_id for t in db.all_tracks(conn)} == {"t1", "t3"}
    assert db.cached_snapshot(conn, "p1") == "snap1"
    assert db.cached_snapshot(conn, "p3") == "snap3"
    # A failed playlist must not be cached, or a re-run would skip it forever.
    assert db.cached_snapshot(conn, "p2") is None


def test_a_failed_playlist_is_retried_on_the_next_run(conn):
    pls = [PlaylistRef("p1", "Playlist 1", "snap1")]

    first = sync_playlists(conn, FakeClient(pls, forbidden={"p1"}), include_liked=False)
    assert first.unreadable and not first.counts

    # Access granted later (or a transient 403) — the next run picks it up.
    second = sync_playlists(conn, FakeClient(pls), include_liked=False)
    assert second.counts == {"p1": 1}
    assert not second.unreadable


def test_unchanged_playlists_are_skipped_by_snapshot_id(conn):
    pls = [PlaylistRef("p1", "Playlist 1", "snap1")]

    first = sync_playlists(conn, FakeClient(pls), include_liked=False)
    assert first.counts == {"p1": 1}
    assert first.unchanged == 0

    client = FakeClient(pls)
    second = sync_playlists(conn, client, include_liked=False)
    assert second.unchanged == 1
    assert client.read == []  # not re-fetched
    assert second.counts == {}


def test_a_new_snapshot_id_forces_a_resync(conn):
    sync_playlists(conn, FakeClient([PlaylistRef("p1", "Playlist 1", "snap1")]), include_liked=False)

    client = FakeClient([PlaylistRef("p1", "Playlist 1", "snap2")])
    result = sync_playlists(conn, client, include_liked=False)
    assert client.read == ["p1"]
    assert result.counts == {"p1": 1}


def test_force_ignores_the_snapshot_cache(conn):
    pls = [PlaylistRef("p1", "Playlist 1", "snap1")]
    sync_playlists(conn, FakeClient(pls), include_liked=False)

    client = FakeClient(pls)
    sync_playlists(conn, client, force=True, include_liked=False)
    assert client.read == ["p1"]


def test_liked_songs_always_resyncs_and_survives_failure(conn):
    result = sync_playlists(conn, FakeClient([]), include_liked=True)
    assert result.counts == {LIKED_SONGS_ID: 1}
    # No snapshot_id exists for the library, so it must never be skipped.
    assert db.cached_snapshot(conn, LIKED_SONGS_ID) is None

    again = sync_playlists(conn, FakeClient([]), include_liked=True)
    assert again.counts == {LIKED_SONGS_ID: 1}
    assert again.unchanged == 0
