"""Configuration and filesystem locations.

Secrets come from ``.env`` (or the real environment). Nothing secret is ever
written to the SQLite file or to the log; the refresh token lives in the OS
keyring, see :mod:`djset.spotify.auth`.
"""

from __future__ import annotations

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


def db_path() -> Path:
    override = os.environ.get("DJSET_DB_PATH")
    return Path(override) if override else app_data_dir() / "djset.sqlite3"


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
