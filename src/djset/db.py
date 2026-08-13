"""SQLite access. Every read/write in the app goes through here."""

from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import db_path
from .models import AudioFeatures, Track

SCHEMA = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    _seed_genre_aliases(conn)
    conn.commit()
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# tracks
# --------------------------------------------------------------------------


def upsert_track(conn: sqlite3.Connection, t: Track) -> None:
    conn.execute(
        """
        INSERT INTO tracks (spotify_id, uri, isrc, title, artist, artist_ids,
                            album, duration_ms, added_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(spotify_id) DO UPDATE SET
          uri=excluded.uri, isrc=COALESCE(excluded.isrc, tracks.isrc),
          title=excluded.title, artist=excluded.artist,
          artist_ids=excluded.artist_ids, album=excluded.album,
          duration_ms=excluded.duration_ms
        """,
        (
            t.spotify_id,
            t.uri,
            t.isrc,
            t.title,
            t.artist,
            json.dumps(t.artist_ids),
            t.album,
            t.duration_ms,
            t.added_at,
        ),
    )


def _row_to_track(r: sqlite3.Row) -> Track:
    return Track(
        spotify_id=r["spotify_id"],
        uri=r["uri"],
        title=r["title"],
        artist=r["artist"],
        artist_ids=json.loads(r["artist_ids"] or "[]"),
        isrc=r["isrc"],
        album=r["album"],
        duration_ms=r["duration_ms"],
        added_at=r["added_at"],
    )


def all_tracks(conn: sqlite3.Connection) -> list[Track]:
    return [_row_to_track(r) for r in conn.execute("SELECT * FROM tracks")]


def sample_tracks(
    conn: sqlite3.Connection, n: int, seed: int = 1234
) -> list[Track]:
    """A deterministic random sample of the library.

    Coverage has to be measured on a representative sample: taking the first N
    rows would over-weight whichever playlist happened to sync first, and a
    library is not uniformly distributed across genres or eras. The seed is
    fixed so a re-run measures the same tracks and the number is comparable
    across normalizer changes.
    """
    tracks = all_tracks(conn)
    if n >= len(tracks):
        return tracks
    rng = random.Random(seed)
    return rng.sample(tracks, n)


def tracks_in_playlists(
    conn: sqlite3.Connection, playlist_ids: Iterable[str]
) -> list[Track]:
    ids = list(playlist_ids)
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT t.* FROM tracks t
        JOIN playlist_tracks pt ON pt.spotify_id = t.spotify_id
        WHERE pt.playlist_id IN ({q})
        """,
        ids,
    )
    return [_row_to_track(r) for r in rows]


def set_playlist_members(
    conn: sqlite3.Connection,
    playlist_id: str,
    members: list[tuple[str, int, str | None]],
) -> None:
    """Replace membership wholesale — a playlist read is always complete."""
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO playlist_tracks (playlist_id, spotify_id, position, added_at)"
        " VALUES (?,?,?,?)",
        [(playlist_id, sid, pos, added) for sid, pos, added in members],
    )


# --------------------------------------------------------------------------
# artists
# --------------------------------------------------------------------------


def upsert_artist(
    conn: sqlite3.Connection, spotify_id: str, name: str, genres: list[str]
) -> None:
    conn.execute(
        """
        INSERT INTO artists (spotify_id, name, genres, fetched_at) VALUES (?,?,?,?)
        ON CONFLICT(spotify_id) DO UPDATE SET
          name=excluded.name, genres=excluded.genres, fetched_at=excluded.fetched_at
        """,
        (spotify_id, name, json.dumps(genres), utcnow()),
    )


def known_artist_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["spotify_id"] for r in conn.execute("SELECT spotify_id FROM artists")}


def artist_genres(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return {
        r["spotify_id"]: json.loads(r["genres"] or "[]")
        for r in conn.execute("SELECT spotify_id, genres FROM artists")
    }


# --------------------------------------------------------------------------
# audio features
# --------------------------------------------------------------------------


def get_features(conn: sqlite3.Connection, spotify_id: str) -> AudioFeatures | None:
    r = conn.execute(
        "SELECT * FROM audio_features WHERE spotify_id = ?", (spotify_id,)
    ).fetchone()
    return _row_to_features(r) if r else None


def _row_to_features(r: sqlite3.Row) -> AudioFeatures:
    return AudioFeatures(
        spotify_id=r["spotify_id"],
        bpm=r["bpm"],
        key_camelot=r["key_camelot"],
        key_open=r["key_open"],
        energy=r["energy"],
        source=r["source"],
        confidence=r["confidence"],
        fetched_at=r["fetched_at"],
    )


def all_features(conn: sqlite3.Connection) -> dict[str, AudioFeatures]:
    return {
        r["spotify_id"]: _row_to_features(r)
        for r in conn.execute("SELECT * FROM audio_features")
    }


def upsert_features(conn: sqlite3.Connection, f: AudioFeatures) -> None:
    conn.execute(
        """
        INSERT INTO audio_features (spotify_id, bpm, key_camelot, key_open, energy,
                                    source, confidence, fetched_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(spotify_id) DO UPDATE SET
          bpm=excluded.bpm, key_camelot=excluded.key_camelot,
          key_open=excluded.key_open, energy=excluded.energy,
          source=excluded.source, confidence=excluded.confidence,
          fetched_at=excluded.fetched_at
        """,
        (
            f.spotify_id,
            f.bpm,
            f.key_camelot,
            f.key_open,
            f.energy,
            f.source,
            f.confidence,
            f.fetched_at or utcnow(),
        ),
    )


# --------------------------------------------------------------------------
# enrichment misses
# --------------------------------------------------------------------------

MAX_ATTEMPTS = 3


def record_miss(conn: sqlite3.Connection, spotify_id: str, reason: str) -> None:
    conn.execute(
        """
        INSERT INTO enrichment_misses (spotify_id, attempts, last_try, reason)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(spotify_id) DO UPDATE SET
          attempts = enrichment_misses.attempts + 1,
          last_try = excluded.last_try,
          reason   = excluded.reason
        """,
        (spotify_id, utcnow(), reason),
    )


def clear_miss(conn: sqlite3.Connection, spotify_id: str) -> None:
    conn.execute("DELETE FROM enrichment_misses WHERE spotify_id = ?", (spotify_id,))


def exhausted_ids(conn: sqlite3.Connection) -> set[str]:
    """Tracks that hit the retry ceiling — never look them up again."""
    return {
        r["spotify_id"]
        for r in conn.execute(
            "SELECT spotify_id FROM enrichment_misses WHERE attempts >= ?",
            (MAX_ATTEMPTS,),
        )
    }


def miss_reasons(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["spotify_id"]: r["reason"]
        for r in conn.execute("SELECT spotify_id, reason FROM enrichment_misses")
    }


# --------------------------------------------------------------------------
# playlists cache
# --------------------------------------------------------------------------


def cached_snapshot(conn: sqlite3.Connection, playlist_id: str) -> str | None:
    r = conn.execute(
        "SELECT snapshot_id FROM playlists_cache WHERE spotify_id = ?", (playlist_id,)
    ).fetchone()
    return r["snapshot_id"] if r else None


def upsert_playlist_cache(
    conn: sqlite3.Connection,
    playlist_id: str,
    name: str,
    snapshot_id: str | None,
    track_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO playlists_cache (spotify_id, name, snapshot_id, track_count, synced_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(spotify_id) DO UPDATE SET
          name=excluded.name, snapshot_id=excluded.snapshot_id,
          track_count=excluded.track_count, synced_at=excluded.synced_at
        """,
        (playlist_id, name, snapshot_id, track_count, utcnow()),
    )


def cached_playlists(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM playlists_cache ORDER BY name COLLATE NOCASE")
    )


# --------------------------------------------------------------------------
# genre aliases
# --------------------------------------------------------------------------

# Seeded once at DB creation; the settings pane edits these and edits persist
# because the seed only inserts rows that are absent.
SEED_ALIASES: dict[str, str] = {
    "reggaeton urbano": "reggaeton",
    "urbano latino": "reggaeton",
    "trap latino": "reggaeton",
    "latin hip hop": "reggaeton",
    "deep house": "house",
    "tech house": "house",
    "progressive house": "house",
    "electro house": "house",
    "future house": "house",
    "melodic house": "house",
    "afro house": "house",
    "bass house": "house",
    "soulful house": "house",
    "funky house": "house",
    "minimal techno": "techno",
    "melodic techno": "techno",
    "hard techno": "techno",
    "detroit techno": "techno",
    "acid techno": "techno",
    "liquid funk": "drum and bass",
    "drum & bass": "drum and bass",
    "dnb": "drum and bass",
    "jungle": "drum and bass",
    "uk garage": "garage",
    "future garage": "garage",
    "hip hop": "hip hop",
    "rap": "hip hop",
    "trap": "hip hop",
    "conscious hip hop": "hip hop",
    "pop rap": "hip hop",
    "dance pop": "pop",
    "electropop": "pop",
    "indie pop": "pop",
    "art pop": "pop",
    "edm": "edm",
    "big room": "edm",
    "progressive trance": "trance",
    "uplifting trance": "trance",
    "psytrance": "trance",
    "nu disco": "disco",
    "disco house": "disco",
    "indie rock": "rock",
    "alt rock": "rock",
    "alternative rock": "rock",
    "classic rock": "rock",
    "hard rock": "rock",
    "neo soul": "soul",
    "contemporary r&b": "r&b",
    "rnb": "r&b",
    "salsa dura": "salsa",
    "afrobeats": "afrobeat",
    "afro pop": "afrobeat",
}


def _seed_genre_aliases(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO genre_aliases (raw, canonical) VALUES (?,?)",
        list(SEED_ALIASES.items()),
    )


def genre_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["raw"]: r["canonical"]
        for r in conn.execute("SELECT raw, canonical FROM genre_aliases")
    }


def set_genre_alias(conn: sqlite3.Connection, raw: str, canonical: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO genre_aliases (raw, canonical) VALUES (?,?)",
        (raw.strip().lower(), canonical.strip().lower()),
    )
