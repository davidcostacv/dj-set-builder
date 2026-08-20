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
from dataclasses import replace
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
        """Try sources in priority order until the answer is complete.

        Returns ``(features, failure_reason)``; exactly one is non-None.

        "Complete" means a tempo *and* a key, which is what sequencing needs.
        Stopping at the first source to say anything was the wrong stopping
        rule: Deezer answers for a great many tracks and never carries harmonic
        data, so it ended the search with a half-answer and no lower-priority
        source was ever asked. Once the tempo is settled the only thing still
        worth looking for is a key, and the first source to have one supplies
        it — recorded in ``key_source``, because it is not where the rest of
        the row came from.
        """
        if not self.sources:
            return None, "no sources registered"

        best: AudioFeatures | None = None
        reasons: list[str] = []
        for source in self.sources:
            try:
                found = source.lookup(track)
            except Exception as exc:  # a broken source must not kill the pass
                log.warning("source %s raised on %s: %s", source.name, track.title, exc)
                reasons.append(f"{source.name}: error {exc}")
                continue
            if found is None:
                reasons.append(f"{source.name}: no match")
                continue
            if best is None:
                best = found
                if best.key_camelot is not None:
                    return best, None
                continue
            # A more trusted source already settled the tempo. Take nothing but
            # the key, and only from the first source that actually has one.
            if found.key_camelot is not None:
                return (
                    replace(
                        best,
                        key_camelot=found.key_camelot,
                        key_open=found.key_open,
                        key_source=found.key_source or found.source,
                    ),
                    None,
                )
        if best is not None:
            return best, None
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


def merged_with_key(
    existing: AudioFeatures | None, candidate: AudioFeatures
) -> AudioFeatures | None:
    """``existing`` with ``candidate``'s key filled in, or None if not applicable.

    Priority answers *whose tempo to believe*. It was being applied as *whose
    row wins*, and the two are not the same question: Deezer outranks the DSP
    analyser and has no harmonic data at all, so a Deezer row with a tempo and
    no key discarded a perfectly good DSP key — after paying to compute it.
    That was 670 tracks unsequenceable for want of a value the app already had.

    This only ever fills a NULL. It never changes a tempo, never replaces a key,
    and never touches a row a source with more standing already spoke for —
    which is why, unlike :meth:`Resolver.should_overwrite`, it does not need to
    fail closed on an unknown priority. Nothing is at risk of being lost.

    :func:`_would_lose_the_key` in the runner guards the mirror image: a
    higher-priority source that is *silent* on key must not erase one.
    """
    if existing is None or candidate.key_camelot is None:
        return None
    if existing.key_camelot is not None:
        return None                      # nothing to fill; never a replacement
    if existing.source == MANUAL_SOURCE:
        return None                      # a hand-typed row is not amended

    return replace(
        existing,
        key_camelot=candidate.key_camelot,
        key_open=candidate.key_open,
        key_source=candidate.key_source or candidate.source,
        fetched_at=None,                 # stamped on write: the row changed now
    )


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
    # Whoever supplied the key still supplied it. Carrying the row over without
    # this would relabel an inherited key as hand-typed, which is the one thing
    # `manual` is supposed to mean.
    key_source = MANUAL_SOURCE if key_camelot is not None else None
    if existing is not None:
        bpm = bpm if bpm is not None else existing.bpm
        if key_camelot is None:
            key_camelot = existing.key_camelot
            key_source = existing.key_source
        energy = energy if energy is not None else existing.energy

    f = AudioFeatures(
        spotify_id=spotify_id,
        bpm=bpm,
        key_camelot=key_camelot,
        key_open=None,
        energy=energy,
        source=MANUAL_SOURCE,
        key_source=key_source,
        confidence=1.0,
    )
    db.upsert_features(conn, f)
    db.clear_miss(conn, spotify_id)
    return f
