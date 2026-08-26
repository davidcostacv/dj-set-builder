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
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import db
from .models import Track
from .net import HttpError
from .sequencing import SequenceMode

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


# How the set was mixed, in the words a DJ would use rather than the enum's.
_MIXED_AS = {
    SequenceMode.BPM: "Mixed by BPM",
    SequenceMode.KEY: "Mixed in key",
    SequenceMode.BPM_KEY: "Mixed in key + BPM",
}


# Spotify truncates a playlist description here.
MAX_DESCRIPTION = 300


def describe_set(
    mode: SequenceMode,
    track_count: int,
    *,
    tolerance: float | None = None,
    half_double: bool = True,
    energy_boost: bool = False,
    edited: bool = False,
    carried: int = 0,
    sources: Sequence[str] = (),
) -> str:
    """The playlist description: what was done to this order, and how.

    Once a set is in Spotify the description is the only record of how it was
    built. Three playlists with the same name and the same tracks in three
    different orders are indistinguishable without it, and "built with djset"
    said nothing about the one thing that varies.

    Deviations from the defaults are named and the defaults are not, so the
    line stays short and anything unusual about a set is visible at a glance.
    """
    parts = [_MIXED_AS.get(mode, "Mixed")]

    detail: list[str] = []
    if mode.uses_bpm:
        if tolerance is not None:
            detail.append(f"±{tolerance * 100:.0f}%")
        if not half_double:
            # Off is the surprising choice: it forbids a 70 BPM track after a
            # 140 one, which is a mix most DJs would expect to be allowed.
            detail.append("strict tempo")
    if mode.uses_key and energy_boost:
        detail.append("energy boosts")
    if detail:
        parts[0] += f" ({', '.join(detail)})"

    count = f"{track_count} track{'s' if track_count != 1 else ''}"
    if edited:
        # The order is no longer purely what the sequencer produced, and
        # claiming otherwise is the sort of small lie that costs trust later.
        count += ", hand-edited"
    if carried:
        # These are in the playlist but were never mixed into it. Saying so
        # keeps the headline claim — "Mixed in key + BPM" — true of the part
        # it actually describes.
        count += f" ({carried} unmixed, at the end)"
    parts.append(count)

    # Where the tracks came from. A set is a rearrangement of something, and
    # six months later "which playlist was this?" is the question the name
    # rarely answers on its own.
    named = [n.strip() for n in sources if n and n.strip()]
    if named:
        parts.append(_from_clause(named))
    parts.append("djset")

    line = " · ".join(parts)
    if len(line) <= MAX_DESCRIPTION:
        return line
    # Playlist names have no length limit worth relying on. Drop sources until
    # it fits rather than let Spotify cut the line mid-word — losing a name is
    # recoverable, losing the end of the sentence is not.
    for keep in range(len(named) - 1, 0, -1):
        parts[-2] = _from_clause(named, keep=keep)
        line = " · ".join(parts)
        if len(line) <= MAX_DESCRIPTION:
            return line
    return line[: MAX_DESCRIPTION - 1] + "…"


def _from_clause(names: Sequence[str], keep: int = 2) -> str:
    """``from A``, ``from A, B``, ``from A, B +3 more``."""
    shown = list(names[:keep])
    rest = len(names) - len(shown)
    return "from " + ", ".join(shown) + (f" +{rest} more" if rest else "")


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

    # Idempotency check BEFORE creating anything. Keyed on the name as well as
    # the set: pressing Save twice should reuse, but rebuilding the same set
    # under a new name is a new playlist, and matching on the hash alone
    # returned the old one and discarded the name that was typed.
    existing = db.find_export(conn, digest, name)
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


def remove_duplicates(
    client,
    playlist_id: str,
    tracks: list[Track],
    groups: list,
    *,
    progress: Progress = _noop,
) -> int:
    """Rewrite ``playlist_id`` without the duplicate copies. Returns how many
    entries were removed.

    A wholesale replace rather than a per-item delete: Spotify's remove
    endpoint matches by URI, and both copies of a duplicate share one, so
    asking it to remove "that URI" would take the survivor too. Sending the
    kept order back is unambiguous about what should be there afterwards.

    Deliberately does not touch the exports table. This is somebody's own
    playlist being tidied, not a set this app produced, and recording it as an
    export would make a later save think it had already created it.
    """
    doomed = {t.spotify_id for g in groups for t in g.remove}
    if not doomed:
        return 0
    keep = [t for t in tracks if t.spotify_id not in doomed]
    if not keep:
        raise ExportError("Refusing to empty the playlist.")

    uris = track_uris(keep)
    progress(f"Rewriting {len(uris)} tracks…")
    try:
        client.replace_items(playlist_id, uris[:CHUNK])
        for start in range(CHUNK, len(uris), CHUNK):
            client.add_items(playlist_id, uris[start : start + CHUNK])
    except HttpError as exc:
        if exc.status in (403, 401):
            raise ExportError(
                "Spotify refused the edit. You can only change playlists you "
                "own — a followed or collaborative one belongs to someone else."
            ) from exc
        raise ExportError(f"Could not rewrite the playlist (HTTP {exc.status}).") from exc
    return len(doomed)


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
