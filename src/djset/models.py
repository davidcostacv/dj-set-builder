"""Plain data objects shared across all three layers.

These are the only types the matching engine (layer 3) is allowed to see — it
must never import anything under :mod:`djset.spotify`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Track:
    spotify_id: str
    uri: str
    title: str
    artist: str
    artist_ids: list[str] = field(default_factory=list)
    isrc: str | None = None
    album: str | None = None
    duration_ms: int | None = None
    added_at: str | None = None


@dataclass(frozen=True)
class AudioFeatures:
    spotify_id: str
    bpm: float | None
    key_camelot: str | None
    key_open: str | None = None
    energy: float | None = None
    source: str = "unknown"
    confidence: float | None = None
    fetched_at: str | None = None

    @property
    def is_usable(self) -> bool:
        """Has enough data to participate in BPM+key sequencing."""
        return self.bpm is not None and self.key_camelot is not None


@dataclass(frozen=True)
class Artist:
    spotify_id: str
    name: str
    genres: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlaylistRef:
    spotify_id: str
    name: str
    snapshot_id: str | None = None
    track_count: int = 0


# Liked Songs is not a real playlist; it gets a sentinel id everywhere.
LIKED_SONGS_ID = "__liked__"
LIKED_SONGS_NAME = "Liked Songs"
