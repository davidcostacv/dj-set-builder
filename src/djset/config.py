"""Configuration and filesystem locations.

Secrets come from ``.env`` (or the real environment). Nothing secret is ever
written to the SQLite file or to the log; the refresh token lives in the OS
keyring, see :mod:`djset.spotify.auth`.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "djset"

# Scopes: read playlists + liked songs, write playlists back. Nothing else.
SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
)


def project_root() -> Path:
    """Repo root — where ``.env`` is expected to live during development."""
    return Path(__file__).resolve().parents[2]


def app_data_dir() -> Path:
    """OS-appropriate per-user data directory. Created on first access."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


_resolved: Path | None = None


def is_store_python() -> bool:
    """Whether this interpreter is the Microsoft Store build of Python.

    That build runs in an AppContainer, and Windows silently redirects its
    writes to %LOCALAPPDATA% into a per-package ``LocalCache`` folder. The
    process reads back the path it asked for, so nothing looks wrong from
    inside — but a program that is *not* sandboxed, such as the PyInstaller
    build of this app, looks at the real path and finds an empty directory.
    """
    return "WindowsApps" in sys.prefix or "LocalCache" in sys.prefix


def _redirected_candidates() -> list[Path]:
    """Store-Python sandbox copies of the database, wherever they landed.

    Deliberately not gated on *this* process being Store Python: the frozen
    app is the one that cannot see them, and it is the one that needs to look.
    """
    if sys.platform != "win32":
        return []
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    out = []
    for pkg in (local / "Packages").glob("*Python*"):
        candidate = pkg / "LocalCache" / "Local" / APP_NAME / "djset.sqlite3"
        if candidate.exists():
            out.append(candidate)
    return out


def _track_count(path: Path) -> int:
    """How much library a candidate actually holds. -1 if unreadable."""
    import sqlite3

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            return conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    except Exception:
        return -1


def db_path() -> Path:
    """The database this process should use.

    Store Python's sandbox splits the library in two: the CLI fills a
    redirected copy while the packaged .exe creates an empty one at the real
    path and shows no tracks at all. Rather than guess by location, take
    whichever candidate actually holds the most tracks — that is deterministic,
    self-healing once the two are consolidated, and it cannot pick an empty
    database over a full one.

    ``DJSET_DB_PATH`` overrides everything, and remains the way to be explicit.
    """
    override = os.environ.get("DJSET_DB_PATH")
    if override:
        return Path(override)

    # Resolved once per process. The scan opens every candidate to count rows,
    # and db_path() is called on nearly every operation — without this the app
    # pays for two SQLite opens each time and repeats the warning on each one.
    global _resolved
    if _resolved is None:
        _resolved = _resolve_db_path()
    return _resolved


def _resolve_db_path() -> Path:
    canonical = app_data_dir() / "djset.sqlite3"
    counts = {c: _track_count(c) for c in (canonical, *_redirected_candidates())}

    best = max(counts, key=lambda c: counts[c])
    if best != canonical and counts[best] > counts.get(canonical, -1):
        logging.getLogger(__name__).warning(
            "Using %s (%d tracks) rather than %s (%d): Store Python redirected "
            "an earlier run into its sandbox. Set DJSET_DB_PATH to pin one.",
            best, counts[best], canonical, max(counts.get(canonical, 0), 0),
        )
        return best
    return canonical


def log_path() -> Path:
    return app_data_dir() / "djset.log"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing, with a fix-it message."""


@dataclass(frozen=True)
class Config:
    spotify_client_id: str
    spotify_redirect_uri: str
    getsongbpm_api_key: str | None
    getsongbpm_rate_per_hour: int

    @property
    def scopes(self) -> str:
        return " ".join(SCOPES)


_loaded = False


def _load_dotenv_once() -> None:
    global _loaded
    if not _loaded:
        load_dotenv(project_root() / ".env")
        _loaded = True


def load_config(*, require_getsongbpm: bool = False) -> Config:
    _load_dotenv_once()

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        raise ConfigError(
            "SPOTIFY_CLIENT_ID is not set.\n"
            f"Copy {project_root() / '.env.example'} to .env and fill it in.\n"
            "Get a client ID at https://developer.spotify.com/dashboard"
        )

    redirect = os.environ.get(
        "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
    ).strip()

    gsb = os.environ.get("GETSONGBPM_API_KEY", "").strip() or None
    if require_getsongbpm and not gsb:
        raise ConfigError(
            "GETSONGBPM_API_KEY is not set — enrichment cannot run without it.\n"
            "Request a free key at https://getsongbpm.com/api"
        )

    try:
        rate = int(os.environ.get("GETSONGBPM_RATE_PER_HOUR", "2400"))
    except ValueError:
        rate = 2400

    return Config(
        spotify_client_id=client_id,
        spotify_redirect_uri=redirect,
        getsongbpm_api_key=gsb,
        getsongbpm_rate_per_hour=max(1, rate),
    )
