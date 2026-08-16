"""Playlist creation — the second half of Generate.

There is no separate export step and no file output of any kind. Pressing
Generate sequences the set and immediately creates the playlist in the user's
own account; the Spotify link is the only product.

Two constraints drive the implementation:

* **Order is the entire product.** Items are added in sequential chunks of 100,
  never concurrently — parallel POSTs scramble the order.
* **Creation is automatic, so idempotency is critical.** A stray double-click
  must not write a second playlist. The ordered URI list is hashed and checked
  against the ``exports`` table *before* anything is created.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from . import db
from .models import Track
from .net import HttpError

log = logging.getLogger(__name__)

CHUNK = 100

Progress = Callable[[str], None]


def _noop(msg: str) -> None:
    log.info(msg)


def content_hash(uris: list[str]) -> str:
    """SHA256 of the ordered URI list.

    Order-sensitive on purpose: the same tracks in a different order are a
    different DJ set and deserve their own playlist.
    """
    h = hashlib.sha256()
    for uri in uris:
        h.update(uri.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def track_uris(tracks: list[Track]) -> list[str]:
    """URIs as stored from playlist reads.

    Never URIs from a search result: Spotify's track relinking can hand back a
    different (market-relinked) id, and playlist operations need the original.
    """
    return [t.uri for t in tracks]


@dataclass(frozen=True)
class ExportResult:
    playlist_id: str
    url: str
    name: str
    track_count: int
    reused: bool  # True when an identical set already existed

    @property
    def message(self) -> str:
        if self.reused:
            return (
                f"This exact set already exists as “{self.name}”. "
                "Opened the existing playlist instead of creating a duplicate."
            )
        return f"Created “{self.name}” with {self.track_count} tracks."


def playlist_url(playlist_id: str) -> str:
    return f"https://open.spotify.com/playlist/{playlist_id}"


class ExportError(RuntimeError):
    pass


def export_to_spotify(
    conn: sqlite3.Connection,
    client,  # SpotifyClient — untyped to keep layer 3 free of layer 1 imports
    name: str,
    tracks: list[Track],
    *,
    description: str = "",
    public: bool = False,
    progress: Progress = _noop,
) -> ExportResult:
    """Create the playlist and fill it, in order.

    Raises :class:`ExportError` if the playlist could not be populated; the
    empty shell is cleaned up first so a half-filled set is never left behind
    without explanation.
    """
    uris = track_uris(tracks)
    if not uris:
        raise ExportError("Refusing to create an empty playlist.")

    digest = content_hash(uris)

    # Idempotency check BEFORE creating anything.
    existing = db.find_export(conn, digest)
    if existing is not None:
        log.info("Identical set already exported as %s — reusing.", existing["playlist_id"])
        return ExportResult(
            playlist_id=existing["playlist_id"],
            url=playlist_url(existing["playlist_id"]),
            name=existing["name"] or name,
            track_count=len(uris),
            reused=True,
        )

    progress(f"Creating “{name}”…")
    created = client.create_playlist(name, description=description, public=public)
    playlist_id = created.get("id")
    if not playlist_id:
        raise ExportError(f"Spotify did not return a playlist id: {created}")

    try:
        _add_all(client, playlist_id, uris, progress)
    except Exception as exc:
        # Never orphan a half-filled playlist.
        log.error("Adding items failed for %s: %s", playlist_id, exc)
        _cleanup(client, playlist_id)
        raise ExportError(
            f"Created the playlist but could not add all {len(uris)} tracks "
            f"({exc}). The empty playlist was removed from your library."
        ) from exc

    db.record_export(conn, playlist_id, digest, name)
    conn.commit()
    progress(f"Done — {len(uris)} tracks.")
    return ExportResult(
        playlist_id=playlist_id,
        url=playlist_url(playlist_id),
        name=name,
        track_count=len(uris),
        reused=False,
    )


def _add_all(client, playlist_id: str, uris: list[str], progress: Progress) -> None:
    """Sequential chunks of 100. Concurrency here would scramble the set."""
    total = len(uris)
    for start in range(0, total, CHUNK):
        chunk = uris[start : start + CHUNK]
        client.add_items(playlist_id, chunk)
        done = min(start + len(chunk), total)
        progress(f"Added {done}/{total} tracks…")


def _cleanup(client, playlist_id: str) -> None:
    try:
        client.unfollow_playlist(playlist_id)
        log.info("Removed the empty playlist %s.", playlist_id)
    except (HttpError, Exception) as exc:  # cleanup must not mask the real error
        log.warning("Could not remove the empty playlist %s: %s", playlist_id, exc)


def update_playlist_order(
    conn: sqlite3.Connection,
    client,
    playlist_id: str,
    tracks: list[Track],
    *,
    name: str | None = None,
) -> None:
    """Push edits from the result table back, as a wholesale replace.

    Called when the user has reordered or removed rows and clicked Update —
    batched deliberately, never on every drag.
    """
    uris = track_uris(tracks)
    if not uris:
        raise ExportError("Refusing to empty the playlist.")
    client.replace_items(playlist_id, uris[:CHUNK])
    for start in range(CHUNK, len(uris), CHUNK):
        client.add_items(playlist_id, uris[start : start + CHUNK])
    db.record_export(conn, playlist_id, content_hash(uris), name or "")
    conn.commit()


def split_by_genre(
    conn: sqlite3.Connection,
    client,
    buckets: dict[str, list[Track]],
    *,
    name_template: str = "{genre}",
    public: bool = False,
    progress: Progress = _noop,
) -> list[ExportResult]:
    """Secondary bulk action: one playlist per genre bucket, unsequenced.

    A library-organisation tool, not a DJ tool — deliberately separate from the
    main flow. A bucket that fails does not abort the rest.
    """
    results: list[ExportResult] = []
    for genre, tracks in buckets.items():
        if not tracks:
            continue
        playlist_name = name_template.format(genre=genre)
        try:
            results.append(
                export_to_spotify(
                    conn, client, playlist_name, tracks,
                    description=f"{len(tracks)} tracks · {genre}",
                    public=public, progress=progress,
                )
            )
        except ExportError as exc:
            log.error("Bucket %s failed: %s", genre, exc)
            progress(f"“{playlist_name}” failed: {exc}")
    return results
