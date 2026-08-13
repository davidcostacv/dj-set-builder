"""Pull playlists, Liked Songs, and artist genres into SQLite.

Respects ``snapshot_id``: a playlist whose snapshot matches the cache is
skipped entirely. Liked Songs has no snapshot id, so it always re-reads.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import db
from ..models import LIKED_SONGS_ID, LIKED_SONGS_NAME, PlaylistRef
from ..net import HttpError
from .client import SpotifyClient

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


def _noop(msg: str) -> None:
    log.info(msg)


def list_sources(client: SpotifyClient) -> list[PlaylistRef]:
    """All playlists plus the Liked Songs pseudo-playlist, for pane 1."""
    liked = PlaylistRef(spotify_id=LIKED_SONGS_ID, name=LIKED_SONGS_NAME)
    return [liked, *client.playlists()]


@dataclass
class SyncResult:
    counts: dict[str, int] = field(default_factory=dict)
    unchanged: int = 0
    # (playlist name, reason) for sources that could not be read at all.
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    @property
    def tracks_seen(self) -> int:
        return sum(self.counts.values())


def sync_playlists(
    conn: sqlite3.Connection,
    client: SpotifyClient,
    *,
    playlist_ids: list[str] | None = None,
    include_liked: bool = True,
    force: bool = False,
    progress: Progress = _noop,
) -> SyncResult:
    """Sync sources into SQLite.

    A playlist that cannot be read is recorded and skipped, never fatal. In
    development mode Spotify returns 403 for playlists the app owner does not
    own, so a followed playlist failing is normal and must not abort a
    several-hundred-playlist run.
    """
    remote = client.playlists()
    if playlist_ids is not None:
        wanted = set(playlist_ids)
        remote = [p for p in remote if p.spotify_id in wanted]
        include_liked = include_liked and LIKED_SONGS_ID in wanted

    result = SyncResult()

    for i, pl in enumerate(remote, 1):
        cached = db.cached_snapshot(conn, pl.spotify_id)
        if not force and cached and cached == pl.snapshot_id:
            result.unchanged += 1
            progress(f"[{i}/{len(remote)}] {pl.name}: unchanged, skipped")
            continue

        progress(f"[{i}/{len(remote)}] {pl.name}: syncing…")
        try:
            members: list[tuple[str, int, str | None]] = []
            for pos, (track, added_at) in enumerate(
                client.playlist_items(pl.spotify_id)
            ):
                db.upsert_track(conn, track)
                members.append((track.spotify_id, pos, added_at))
        except HttpError as exc:
            reason = _explain(exc)
            result.unreadable.append((pl.name, reason))
            progress(f"[{i}/{len(remote)}] {pl.name}: SKIPPED — {reason}")
            conn.rollback()
            continue

        db.set_playlist_members(conn, pl.spotify_id, members)
        db.upsert_playlist_cache(
            conn, pl.spotify_id, pl.name, pl.snapshot_id, len(members)
        )
        conn.commit()
        result.counts[pl.spotify_id] = len(members)

    if include_liked:
        progress("Liked Songs: syncing…")
        try:
            members = []
            for pos, (track, added_at) in enumerate(client.liked_songs()):
                db.upsert_track(conn, track)
                members.append((track.spotify_id, pos, added_at))
        except HttpError as exc:
            conn.rollback()
            result.unreadable.append((LIKED_SONGS_NAME, _explain(exc)))
        else:
            db.set_playlist_members(conn, LIKED_SONGS_ID, members)
            # No snapshot_id exists for the library, so store None and always re-read.
            db.upsert_playlist_cache(
                conn, LIKED_SONGS_ID, LIKED_SONGS_NAME, None, len(members)
            )
            conn.commit()
            result.counts[LIKED_SONGS_ID] = len(members)

    return result


def _explain(exc: HttpError) -> str:
    if exc.status == 403:
        return "403 Forbidden (not owned by you — dev mode cannot read it)"
    if exc.status == 404:
        return "404 Not Found (deleted or unavailable)"
    return f"HTTP {exc.status}"


def sync_artists(
    conn: sqlite3.Connection,
    client: SpotifyClient,
    *,
    force: bool = False,
    progress: Progress = _noop,
) -> int:
    """Fetch genres for every artist referenced by a cached track.

    One request per artist — the batch endpoint was removed in Feb 2026, so a
    first run over a large library is genuinely slow. Results are cached, and a
    cancelled run resumes because each artist is committed as it lands.
    """
    wanted: set[str] = set()
    for t in db.all_tracks(conn):
        wanted.update(t.artist_ids)

    if not force:
        wanted -= db.known_artist_ids(conn)

    total = len(wanted)
    if not total:
        progress("Artist genres already cached.")
        return 0

    progress(f"Fetching genres for {total} artists (one request each)…")
    done = 0
    for i, aid in enumerate(sorted(wanted), 1):
        artist = client.artist(aid)
        if artist is None:
            continue
        db.upsert_artist(conn, artist.spotify_id, artist.name, artist.genres)
        done += 1
        if i % 25 == 0:
            conn.commit()
            progress(f"  artists {i}/{total}")
    conn.commit()
    progress(f"Artist genres cached: {done}/{total}")
    return done
