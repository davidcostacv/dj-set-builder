"""SQLite access. Every read/write in the app goes through here."""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import db_path
from .models import AudioFeatures, Track

log = logging.getLogger(__name__)

SCHEMA = Path(__file__).with_name("schema.sql")

# How long to block waiting for another writer before giving up.
BUSY_TIMEOUT_S = 30.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Bump when schema.sql or _ADDED_COLUMNS changes, so an existing database
# picks the change up. Without a bump the setup below is skipped entirely.
SCHEMA_VERSION = 3


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    # Wait for a competing writer rather than failing instantly. Two processes
    # (or the Qt main thread and its worker) touching the DB at once is normal,
    # not exceptional: a long enrichment pass holds the write lock in bursts.
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")

    # Setup runs once per database, not once per connection. It used to run
    # every time: executescript, the column migration, the alias seed and a
    # commit, which is a *write* transaction taken by every reader. Against a
    # running enrichment that meant constant lock contention, and on the
    # sandboxed path it failed outright with SQLITE_PROTOCOL — a web request
    # could not open the library while a pass was writing to it.
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        _initialise(conn)
    return conn


def _initialise(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    _migrate(conn)
    _seed_genre_aliases(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add them to a database that already exists, so they are applied explicitly.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("tracks", "artist_names", "TEXT"),
    ("audio_features", "key_source", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    _backfill_key_source(conn)
    _widen_export_identity(conn)


def _backfill_key_source(conn: sqlite3.Connection) -> None:
    """Every key already on file came from the row's own source.

    Leaving these NULL would be indistinguishable from "provenance unknown",
    when in fact it is known for every row written before the column existed:
    a single source wrote the whole row.
    """
    changed = conn.execute(
        "UPDATE audio_features SET key_source = source "
        "WHERE key_camelot IS NOT NULL AND key_source IS NULL"
    ).rowcount
    if changed:
        log.info("key_source backfilled from source for %d rows", changed)


def _widen_export_identity(conn: sqlite3.Connection) -> None:
    """Move `exports` from UNIQUE(content_hash) to UNIQUE(content_hash, name).

    SQLite cannot drop a constraint, so the table is rebuilt. Existing rows are
    carried over: they are the record of what this app has put in the account,
    and losing them would mean re-creating playlists that already exist.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='exports'"
    ).fetchone()
    if not row or "content_hash TEXT UNIQUE" not in (row["sql"] or ""):
        return  # already the new shape, or no table yet

    conn.executescript(
        """
        ALTER TABLE exports RENAME TO exports_old;
        CREATE TABLE exports (
          id           INTEGER PRIMARY KEY,
          playlist_id  TEXT,
          content_hash TEXT,
          name         TEXT,
          created_at   TEXT,
          UNIQUE (content_hash, name)
        );
        INSERT INTO exports (id, playlist_id, content_hash, name, created_at)
          SELECT id, playlist_id, content_hash, name, created_at FROM exports_old;
        DROP TABLE exports_old;
        """
    )
    log.info("exports identity widened to (content_hash, name)")


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
                            artist_names, album, duration_ms, added_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(spotify_id) DO UPDATE SET
          uri=excluded.uri, isrc=COALESCE(excluded.isrc, tracks.isrc),
          title=excluded.title, artist=excluded.artist,
          artist_ids=excluded.artist_ids, artist_names=excluded.artist_names,
          album=excluded.album, duration_ms=excluded.duration_ms
        """,
        (
            t.spotify_id,
            t.uri,
            t.isrc,
            t.title,
            t.artist,
            json.dumps(t.artist_ids),
            json.dumps(t.artist_names),
            t.album,
            t.duration_ms,
            t.added_at,
        ),
    )


def _row_to_track(r: sqlite3.Row) -> Track:
    keys = r.keys()
    raw_names = r["artist_names"] if "artist_names" in keys else None
    # Rows written before artist_names existed fall back to splitting the
    # display string — lossy for names containing a comma, but only until the
    # next sync rewrites the row.
    names = json.loads(raw_names) if raw_names else [
        p.strip() for p in (r["artist"] or "").split(",") if p.strip()
    ]
    return Track(
        spotify_id=r["spotify_id"],
        uri=r["uri"],
        title=r["title"],
        artist=r["artist"],
        artist_ids=json.loads(r["artist_ids"] or "[]"),
        artist_names=names,
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
    if n < 0:
        # random.sample raises a ValueError whose message ("Sample larger than
        # population or is negative") describes neither what the caller did nor
        # what to do about it, and it surfaced as a bare traceback.
        raise ValueError(f"sample size cannot be negative, got {n}")
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


def all_playlist_members(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every playlist's track ids, in one pass.

    The UI recomputes on each checkbox click, and querying per click cost a
    fresh connection and a join over the whole membership table — 217ms at
    9,800 tracks, and worse while an enrichment holds the write lock. Read it
    once and keep it; membership only changes on a sync.
    """
    out: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT playlist_id, spotify_id FROM playlist_tracks ORDER BY playlist_id, position"
    ):
        out.setdefault(row["playlist_id"], []).append(row["spotify_id"])
    return out


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
        key_source=r["key_source"],
        confidence=r["confidence"],
        fetched_at=r["fetched_at"],
    )


def all_features(conn: sqlite3.Connection) -> dict[str, AudioFeatures]:
    return {
        r["spotify_id"]: _row_to_features(r)
        for r in conn.execute("SELECT * FROM audio_features")
    }


def upsert_features(conn: sqlite3.Connection, f: AudioFeatures) -> None:
    # A source that wrote the whole row is also where its key came from, so
    # every caller does not have to say so. Only a merged row — a key taken
    # from somewhere other than the row's own source — sets this explicitly.
    key_source = f.key_source
    if f.key_camelot is not None and key_source is None:
        key_source = f.source
    elif f.key_camelot is None:
        key_source = None      # no key, nowhere for it to have come from

    conn.execute(
        """
        INSERT INTO audio_features (spotify_id, bpm, key_camelot, key_open, energy,
                                    source, key_source, confidence, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(spotify_id) DO UPDATE SET
          bpm=excluded.bpm, key_camelot=excluded.key_camelot,
          key_open=excluded.key_open, energy=excluded.energy,
          source=excluded.source, key_source=excluded.key_source,
          confidence=excluded.confidence, fetched_at=excluded.fetched_at
        """,
        (
            f.spotify_id,
            f.bpm,
            f.key_camelot,
            f.key_open,
            f.energy,
            f.source,
            key_source,
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
# exports
# --------------------------------------------------------------------------


def find_export(
    conn: sqlite3.Connection, content_hash: str, name: str | None = None
) -> sqlite3.Row | None:
    """A previous export of this set under this name — the idempotency guard.

    Matching on the name as well as the hash is the difference between "you
    pressed Save twice" and "you rebuilt this set and called it something
    else". The first should reuse; the second is a new playlist, and matching
    on the hash alone silently threw the new name away.
    """
    if name is None:
        return conn.execute(
            "SELECT * FROM exports WHERE content_hash = ? ORDER BY id DESC",
            (content_hash,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM exports WHERE content_hash = ? AND name = ?",
        (content_hash, name),
    ).fetchone()


def record_export(
    conn: sqlite3.Connection, playlist_id: str, content_hash: str, name: str
) -> None:
    """Log a creation so the user can see what the app put in their account."""
    conn.execute(
        """
        INSERT INTO exports (playlist_id, content_hash, name, created_at)
        VALUES (?,?,?,?)
        ON CONFLICT(content_hash, name) DO UPDATE SET
          playlist_id=excluded.playlist_id
        """,
        (playlist_id, content_hash, name, utcnow()),
    )


def all_exports(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM exports ORDER BY created_at DESC"))


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
    """Seed the alias table once, on first use.

    Guarded by a read so that merely opening the database does not take a write
    lock — otherwise every connection contends with a running enrichment pass.
    Only seeding when empty is also what preserves the user's edits: a row they
    deleted is not silently resurrected on the next open.
    """
    already = conn.execute("SELECT 1 FROM genre_aliases LIMIT 1").fetchone()
    if already:
        return
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
