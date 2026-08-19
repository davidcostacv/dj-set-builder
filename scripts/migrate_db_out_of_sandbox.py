"""Move the library out of the Store-Python sandbox to a plain path.

Why this is needed
------------------
Microsoft Store Python runs in an AppContainer and Windows redirects its writes
under %LOCALAPPDATA% into a per-package LocalCache folder. That was survivable
while only one program touched the file. It stopped being survivable once two
did: the redirection began pairing a main database file from one location with
the -wal/-shm from the other, and *every* Python process started failing with

    sqlite3.DatabaseError: file is not a database

on both paths at once — while the running enrichment, holding its own handles,
carried on writing to a perfectly healthy 10 MB file. The data was never at
risk; the sandbox was.

A path outside %LOCALAPPDATA% is not redirected, so both the CLI and a packaged
build see the same bytes and the problem cannot recur.

What it does
------------
Copies rather than moves, verifies the copy before switching anything over, and
leaves the original untouched as a backup. Nothing is deleted.

Run it with no enrichment in flight: a live writer means an un-checkpointed WAL,
and copying a database mid-write is how you get a corrupt one.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

DEFAULT_TARGET = Path.home() / "djset" / "djset.sqlite3"


def find_source() -> Path | None:
    """The fullest database among the places one may have ended up."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from djset.config import app_data_dir, redirected_candidates

    best: tuple[int, Path] | None = None
    for candidate in [app_data_dir() / "djset.sqlite3", *redirected_candidates()]:
        if not candidate.exists():
            continue
        size = candidate.stat().st_size
        if best is None or size > best[0]:
            best = (size, candidate)
    return best[1] if best else None


def writer_running() -> bool:
    """Whether an enrichment still holds the database open."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq pythonw3.13.exe"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return "pythonw3.13.exe" in out
    except Exception:
        return False


# Order matters: parents before the rows that reference them.
_TABLES = (
    "tracks",
    "playlists_cache",
    "playlist_tracks",
    "audio_features",
    "artists",
    "genre_aliases",
    "exports",
    "enrichment_misses",
)


def rebuild_into(src: Path, dst: Path) -> dict[str, int]:
    """Rebuild the database row by row into a fresh file.

    A byte copy would be faster and was the original plan, but the sandbox left
    real damage behind: SQLite reported a rowid out of order and rows missing
    from an index, and `enrichment_misses` counted 4,180 through the index
    while a full scan returned 4,157. Copying the bytes would carry that
    forward. Reading every row and writing it into a clean schema heals it,
    because the destination's indexes are built from scratch.

    A damaged index is also why rows are read with a bare SELECT and inserted
    with INSERT OR IGNORE: the scan may hand back a duplicate the broken index
    was hiding, and that should not abort the rescue.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from djset import db as djdb

    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src, timeout=60)
    source.row_factory = sqlite3.Row
    target = djdb.connect(dst)          # creates the schema

    moved: dict[str, int] = {}
    try:
        for table in _TABLES:
            try:
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.DatabaseError as exc:
                print(f"  {table}: unreadable, skipped ({exc})")
                moved[table] = 0
                continue
            if not rows:
                moved[table] = 0
                continue
            columns = rows[0].keys()
            placeholders = ",".join("?" * len(columns))
            sql = (
                f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
                f"VALUES ({placeholders})"
            )
            target.executemany(sql, [tuple(r) for r in rows])
            moved[table] = len(rows)
        target.commit()
    finally:
        source.close()
        target.close()
    return moved


def verify(path: Path) -> tuple[int, int]:
    """Open the copy and checkpoint it, so it stands alone afterwards."""
    conn = sqlite3.connect(path, timeout=30)
    try:
        tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        usable = conn.execute(
            "SELECT COUNT(*) FROM audio_features "
            "WHERE bpm IS NOT NULL AND key_camelot IS NOT NULL"
        ).fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"integrity check failed on the copy: {integrity}")
        return tracks, usable
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    ap.add_argument("--force", action="store_true", help="proceed even if a writer is running")
    ap.add_argument("--no-setx", action="store_true", help="copy but do not set DJSET_DB_PATH")
    args = ap.parse_args()

    if writer_running() and not args.force:
        print("An enrichment is still running. Copying a database mid-write is how")
        print("you get a corrupt one. Wait for it to finish, or pass --force.")
        return 1

    src = find_source()
    if src is None:
        print("No database found to migrate.")
        return 1
    if args.target.exists():
        print(f"{args.target} already exists — refusing to overwrite it.")
        return 1

    print(f"  from : {src}  ({src.stat().st_size / 1e6:.1f} MB)")
    print(f"  to   : {args.target}")
    moved = rebuild_into(src, args.target)
    for table, n in moved.items():
        print(f"    {table:<20} {n:>6}")

    tracks, usable = verify(args.target)
    print(f"\n  verified: {tracks} tracks, {usable} with BPM+key, integrity ok")

    if args.no_setx:
        print(f"\nSet this yourself when ready:\n  setx DJSET_DB_PATH \"{args.target}\"")
        return 0

    subprocess.run(["setx", "DJSET_DB_PATH", str(args.target)], check=False,
                   capture_output=True, text=True)
    os.environ["DJSET_DB_PATH"] = str(args.target)
    print(f"\n  DJSET_DB_PATH set to {args.target}")
    print("  (new terminals pick it up; this session already has it)")
    print(f"\nThe original is untouched at:\n  {src}")
    print("Delete it only once you are satisfied the new location works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
