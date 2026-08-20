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
    artist: str  # display form, credited artists joined with ", "
    artist_ids: list[str] = field(default_factory=list)
    # Credited artists as a list, in order. Kept separate from `artist` because
    # splitting the joined string back apart is lossy: "Tyler, The Creator"
    # contains a comma inside a single artist's name.
    artist_names: list[str] = field(default_factory=list)
    isrc: str | None = None
    album: str | None = None
    duration_ms: int | None = None
    added_at: str | None = None

    @property
    def primary_artist(self) -> str:
        """First credited artist, structurally rather than by parsing."""
        if self.artist_names:
            return self.artist_names[0]
        return self.artist


# The source that measures audio instead of consulting a catalogue. Named here
# rather than in the analyser so the two cannot drift apart: `DSPSource.name`
# is this constant.
MEASURED_SOURCE = "dsp"


@dataclass(frozen=True)
class AudioFeatures:
    spotify_id: str
    bpm: float | None
    key_camelot: str | None
    key_open: str | None = None
    energy: float | None = None
    source: str = "unknown"
    # Where `key_camelot` came from, which is not always `source`. Priority
    # settles whose *tempo* to believe; a lower-trust source can still be the
    # only one with a key, and that key is worth taking. Persisting the two
    # separately keeps the row honest about itself.
    key_source: str | None = None
    confidence: float | None = None
    fetched_at: str | None = None

    @property
    def is_usable(self) -> bool:
        """Has enough data to participate in BPM+key sequencing."""
        return self.bpm is not None and self.key_camelot is not None

    @property
    def key_is_estimated(self) -> bool:
        """Whether the key was measured from audio rather than looked up.

        Measured against the catalogues on a sample of 67 tracks, the analyser
        agreed exactly 55% of the time and landed on an adjacent Camelot code
        — which still mixes — a further 18%. The remaining quarter conflicts.
        That is good enough to sequence with and not good enough to present as
        if somebody had verified it, so anything showing a key should be able
        to say which kind it is.
        """
        return (self.key_source or self.source) == MEASURED_SOURCE


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
