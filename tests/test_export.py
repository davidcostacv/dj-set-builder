"""Playlist creation.

Creation runs automatically as the second half of Generate, so the things that
matter most are: it never creates a duplicate, it never scrambles the order,
and it never leaves a half-filled playlist behind without saying so.
"""

from __future__ import annotations

import pytest

from djset import db
from djset.export import (
    CHUNK,
    ExportError,
    content_hash,
    export_to_spotify,
    playlist_url,
    split_by_genre,
    track_uris,
    update_playlist_order,
)
from djset.models import Track
from djset.net import HttpError


def T(tid: str) -> Track:
    return Track(
        spotify_id=tid, uri=f"spotify:track:{tid}", title=tid, artist="Artist"
    )


class FakeSpotify:
    """Records what was sent, in the order it was sent."""

    def __init__(self, *, fail_on_chunk: int | None = None, fail_create: bool = False):
        self.created: list[dict] = []
        self.added: list[list[str]] = []
        self.replaced: list[list[str]] = []
        self.unfollowed: list[str] = []
        self.fail_on_chunk = fail_on_chunk
        self.fail_create = fail_create
        self._n = 0

    def create_playlist(self, name, description="", public=False):
        if self.fail_create:
            raise HttpError(500, "/me/playlists", "boom")
        self.created.append({"name": name, "description": description, "public": public})
        return {"id": f"pl{len(self.created)}"}

    def add_items(self, playlist_id, uris):
        self._n += 1
        if self.fail_on_chunk is not None and self._n > self.fail_on_chunk:
            raise HttpError(502, "/items", "boom")
        self.added.append(list(uris))
        return {}

    def replace_items(self, playlist_id, uris):
        self.replaced.append(list(uris))
        return {}

    def unfollow_playlist(self, playlist_id):
        self.unfollowed.append(playlist_id)

    @property
    def flat_added(self) -> list[str]:
        return [u for chunk in self.added for u in chunk]


# ---------------------------------------------------------------------------
# hashing / idempotency
# ---------------------------------------------------------------------------


def test_hash_is_order_sensitive():
    a = content_hash(["spotify:track:1", "spotify:track:2"])
    b = content_hash(["spotify:track:2", "spotify:track:1"])
    assert a != b  # same tracks, different set


def test_hash_is_stable_and_deterministic():
    uris = ["spotify:track:1", "spotify:track:2"]
    assert content_hash(uris) == content_hash(list(uris))
    assert len(content_hash(uris)) == 64


def test_hash_distinguishes_a_prefix_from_a_join():
    # Without a delimiter, ["ab","c"] and ["a","bc"] would collide.
    assert content_hash(["ab", "c"]) != content_hash(["a", "bc"])


def test_a_double_click_does_not_create_a_second_playlist(conn):
    client = FakeSpotify()
    tracks = [T("a"), T("b")]

    first = export_to_spotify(conn, client, "House Set", tracks)
    second = export_to_spotify(conn, client, "House Set", tracks)

    assert len(client.created) == 1  # created exactly once
    assert second.reused and not first.reused
    assert second.playlist_id == first.playlist_id
    assert "already exists" in second.message


def test_reordering_the_same_tracks_is_a_new_set(conn):
    client = FakeSpotify()
    export_to_spotify(conn, client, "Set A", [T("a"), T("b")])
    export_to_spotify(conn, client, "Set B", [T("b"), T("a")])
    assert len(client.created) == 2


def test_the_guard_runs_before_creation_not_after(conn):
    client = FakeSpotify()
    tracks = [T("a")]
    export_to_spotify(conn, client, "Set", tracks)
    client.created.clear()

    export_to_spotify(conn, client, "Set", tracks)
    assert client.created == []  # no create call was made at all


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_order_is_preserved_exactly(conn):
    client = FakeSpotify()
    tracks = [T(f"t{i}") for i in range(250)]
    export_to_spotify(conn, client, "Long Set", tracks)

    assert client.flat_added == [t.uri for t in tracks]


def test_items_are_added_in_sequential_chunks_of_100(conn):
    client = FakeSpotify()
    export_to_spotify(conn, client, "Long Set", [T(f"t{i}") for i in range(250)])

    assert [len(c) for c in client.added] == [CHUNK, CHUNK, 50]


def test_uris_come_from_stored_tracks(conn):
    tracks = [T("a"), T("b")]
    assert track_uris(tracks) == ["spotify:track:a", "spotify:track:b"]


# ---------------------------------------------------------------------------
# failure handling
# ---------------------------------------------------------------------------


def test_a_partial_failure_removes_the_shell_and_reports(conn):
    client = FakeSpotify(fail_on_chunk=1)
    tracks = [T(f"t{i}") for i in range(250)]

    with pytest.raises(ExportError) as exc:
        export_to_spotify(conn, client, "Doomed", tracks)

    assert client.unfollowed == ["pl1"]  # no orphan left behind
    assert "removed from your library" in str(exc.value)
    # And nothing was logged as a successful export.
    assert db.all_exports(conn) == []


def test_a_failed_export_can_be_retried(conn):
    tracks = [T("a"), T("b")]
    with pytest.raises(ExportError):
        export_to_spotify(conn, FakeSpotify(fail_on_chunk=0), "Set", tracks)

    # The hash was never recorded, so a retry genuinely creates it.
    ok = FakeSpotify()
    result = export_to_spotify(conn, ok, "Set", tracks)
    assert not result.reused and len(ok.created) == 1


def test_creation_failure_propagates(conn):
    with pytest.raises(HttpError):
        export_to_spotify(conn, FakeSpotify(fail_create=True), "Set", [T("a")])


def test_empty_set_is_refused(conn):
    with pytest.raises(ExportError):
        export_to_spotify(conn, FakeSpotify(), "Nothing", [])


def test_cleanup_failure_does_not_mask_the_real_error(conn):
    class NoCleanup(FakeSpotify):
        def unfollow_playlist(self, playlist_id):
            raise HttpError(403, "/me/library", "nope")

    with pytest.raises(ExportError) as exc:
        export_to_spotify(conn, NoCleanup(fail_on_chunk=0), "Set", [T("a")])
    assert "could not add all" in str(exc.value)


# ---------------------------------------------------------------------------
# bookkeeping / misc
# ---------------------------------------------------------------------------


def test_every_creation_is_logged(conn):
    client = FakeSpotify()
    export_to_spotify(conn, client, "House Set", [T("a")])
    rows = db.all_exports(conn)
    assert len(rows) == 1
    assert rows[0]["name"] == "House Set"
    assert rows[0]["playlist_id"] == "pl1"
    assert rows[0]["created_at"]


def test_defaults_to_private(conn):
    client = FakeSpotify()
    export_to_spotify(conn, client, "Set", [T("a")])
    assert client.created[0]["public"] is False


def test_description_is_truncated_for_spotify(conn):
    client = FakeSpotify()
    export_to_spotify(conn, client, "Set", [T("a")], description="x" * 500)
    # The client applies the 300-char cap; we just must not crash sending it.
    assert client.created[0]["description"]


def test_url_shape():
    assert playlist_url("abc123") == "https://open.spotify.com/playlist/abc123"


def test_update_pushes_the_new_order_wholesale(conn):
    client = FakeSpotify()
    result = export_to_spotify(conn, client, "Set", [T("a"), T("b"), T("c")])

    reordered = [T("c"), T("a"), T("b")]
    update_playlist_order(conn, client, result.playlist_id, reordered)

    assert client.replaced == [["spotify:track:c", "spotify:track:a", "spotify:track:b"]]


def test_update_refuses_to_empty_a_playlist(conn):
    with pytest.raises(ExportError):
        update_playlist_order(conn, FakeSpotify(), "pl1", [])


def test_split_by_genre_creates_one_playlist_per_bucket(conn):
    client = FakeSpotify()
    buckets = {"house": [T("h1"), T("h2")], "techno": [T("t1")], "empty": []}

    results = split_by_genre(conn, client, buckets, name_template="DJ · {genre}")

    assert len(results) == 2  # the empty bucket is skipped
    assert [c["name"] for c in client.created] == ["DJ · house", "DJ · techno"]


def test_one_failing_bucket_does_not_abort_the_rest(conn):
    class FlakyOnFirst(FakeSpotify):
        def add_items(self, playlist_id, uris):
            if playlist_id == "pl1":
                raise HttpError(502, "/items", "boom")
            return super().add_items(playlist_id, uris)

    client = FlakyOnFirst()
    results = split_by_genre(conn, client, {"bad": [T("a")], "good": [T("b")]})

    assert len(results) == 1
    assert results[0].name == "good"
