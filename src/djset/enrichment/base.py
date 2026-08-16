"""The ``FeatureSource`` protocol and the priority-ordered resolver.

Resolution order per track, stopping at the first hit:

1. Local cache (``audio_features``) — always first, never re-fetched.
2. Registered sources, ascending ``priority``.
3. Manual entry — highest trust, never overwritten by an automated source.

Adding a new source is a registration, not a refactor. Nothing above this layer
knows where a BPM came from.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Protocol, runtime_checkable

from .. import db
from ..models import AudioFeatures, Track

log = logging.getLogger(__name__)

# Manual entries win outright; anything automated must have a higher number.
MANUAL_SOURCE = "manual"
MANUAL_PRIORITY = 0


@runtime_checkable
class FeatureSource(Protocol):
    name: str  # written to audio_features.source
    priority: int  # lower wins when multiple sources have data

    def lookup(self, track: Track) -> AudioFeatures | None: ...


class Resolver:
    """Resolves features for tracks across the registered sources."""

    def __init__(self, sources: list[FeatureSource] | None = None) -> None:
        self.sources: list[FeatureSource] = sorted(
            sources or [], key=lambda s: s.priority
        )

    def register(self, source: FeatureSource) -> None:
        self.sources.append(source)
        self.sources.sort(key=lambda s: s.priority)

    # ------------------------------------------------------------------
    def resolve(self, track: Track) -> tuple[AudioFeatures | None, str | None]:
        """Try every source in priority order.

        Returns ``(features, failure_reason)``; exactly one is non-None.
        """
        if not self.sources:
            return None, "no sources registered"

        reasons: list[str] = []
        for source in self.sources:
            try:
                found = source.lookup(track)
            except Exception as exc:  # a broken source must not kill the pass
                log.warning("source %s raised on %s: %s", source.name, track.title, exc)
                reasons.append(f"{source.name}: error {exc}")
                continue
            if found is not None:
                return found, None
            reasons.append(f"{source.name}: no match")
        return None, "; ".join(reasons)

    # ------------------------------------------------------------------
    def should_overwrite(
        self, existing: AudioFeatures | None, candidate_priority: int
    ) -> bool:
        """A cached row is replaced only by a *provably* higher-trust source.

        The unknown case has to fail closed. If the source that wrote the
        cached row is not registered right now — because it was disabled for
        this run, say — its priority is unknowable, and "unknown" is not
        evidence that the candidate is better. Treating it as replaceable meant
        ``djset enrich --no-getsongbpm --refresh`` would let a BPM-only Deezer
        row silently overwrite a GetSongBPM row that carried a key, quietly
        destroying harmonic data by way of a flag that reads as read-only.
        """
        if existing is None:
            return True
        if existing.source == MANUAL_SOURCE:
            return False
        existing_priority = self.priority_of(existing.source)
        if existing_priority is None:
            return False
        return candidate_priority < existing_priority

    def priority_of(self, source_name: str) -> int | None:
        if source_name == MANUAL_SOURCE:
            return MANUAL_PRIORITY
        for s in self.sources:
            if s.name == source_name:
                return s.priority
        return None


def set_manual_features(
    conn: sqlite3.Connection,
    spotify_id: str,
    bpm: float | None,
    key_camelot: str | None,
    energy: float | None = None,
) -> AudioFeatures:
    """Write a hand-typed value. Always highest trust.

    Fields left as None are *filled in* from whatever is already on file rather
    than clearing it. Typing a value is adding knowledge, not asserting that
    everything omitted is unknown — and the commonest reason to reach for this
    at all is a track that has a BPM from Deezer but no key, where replacing
    the row wholesale would erase the BPM and leave the track exactly as
    unsequenceable as before.
    """
    existing = db.all_features(conn).get(spotify_id)
    if existing is not None:
        bpm = bpm if bpm is not None else existing.bpm
        key_camelot = key_camelot if key_camelot is not None else existing.key_camelot
        energy = energy if energy is not None else existing.energy

    f = AudioFeatures(
        spotify_id=spotify_id,
        bpm=bpm,
        key_camelot=key_camelot,
        key_open=None,
        energy=energy,
        source=MANUAL_SOURCE,
        confidence=1.0,
    )
    db.upsert_features(conn, f)
    db.clear_miss(conn, spotify_id)
    return f
