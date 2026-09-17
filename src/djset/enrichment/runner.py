"""The enrichment pass: walk uncached tracks, resolve, checkpoint as we go.

Cancellation is cooperative and safe at any point — every hit and every miss is
committed immediately, so a cancelled pass resumes rather than restarts.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .. import db
from ..models import AudioFeatures, Track
from .base import MANUAL_SOURCE, Resolver, merged_with_energy, merged_with_key

log = logging.getLogger(__name__)

Progress = Callable[[int, int, str], None]


@dataclass
class EnrichmentStats:
    considered: int = 0
    already_cached: int = 0
    skipped_exhausted: int = 0
    resolved: int = 0
    written: int = 0
    keys_merged: int = 0
    energy_merged: int = 0
    # Wanted energy, but no source enabled for this run could supply it: the
    # row's own source is off and nothing enabled outranks it. Skipped before
    # the lookup rather than after, so a --dsp pass stops downloading and
    # analysing Deezer rows only to throw the answer away.
    skipped_unfillable: int = 0
    # Resolved, then written nowhere because a source with more standing was
    # already on file. Counted because `resolved` alone hid the fact that a
    # whole --refresh pass could compute thousands of answers and store none.
    discarded: int = 0
    missed: int = 0
    cancelled: bool = False


def _noop(done: int, total: int, label: str) -> None:
    if done % 25 == 0 or done == total:
        log.info("  enrichment %d/%d — %s", done, total, label)


def _would_lose_the_key(
    candidate: "AudioFeatures", existing: "AudioFeatures | None"
) -> bool:
    """Whether writing ``candidate`` would erase a key already on file.

    Priority settles whose *tempo* to believe, but outranking another source
    does not imply having more to say. Deezer carries no harmonic data at all,
    and AcousticBrainz deliberately drops its own low-confidence estimates — so
    a higher-trust row can still be silent on key. Overwriting with that
    silence would turn a sequenceable track into an unsequenceable one, which
    is a strictly worse library for a marginally better BPM.
    """
    return (
        existing is not None
        and existing.key_camelot is not None
        and candidate.key_camelot is None
    )


def _energy_fillable(resolver: Resolver, row: AudioFeatures) -> bool:
    """Whether any source enabled for this run could put energy on ``row``.

    Two ways in, mirroring the write path: a source with the *same name* can
    merge energy into the row (see `merged_with_energy`), and a source that
    *outranks* the row can replace it wholesale. Anything else resolves only to
    be discarded — the cost this exists to avoid is a full download and audio
    analysis per row for an answer nobody will keep.
    """
    if row.source == MANUAL_SOURCE:
        return False                     # a hand-typed row is never amended
    row_priority = resolver.priority_of(row.source)
    for source in resolver.sources:
        if source.name == row.source:
            return True
        if row_priority is not None and source.priority < row_priority:
            return True
    return False


def enrich_tracks(
    conn: sqlite3.Connection,
    resolver: Resolver,
    tracks: list[Track] | None = None,
    *,
    cancel: threading.Event | None = None,
    progress: Progress = _noop,
    commit_every: int = 10,
    refresh: bool = False,
    retry_misses: bool = False,
    retry_incomplete: bool = False,
    retry_energy: bool = False,
) -> EnrichmentStats:
    """Resolve features for tracks that do not have them yet.

    A track with *any* cached features is skipped, not just a complete one.
    Some sources are partial by nature — Deezer supplies tempo but no key — so
    treating a BPM-only row as unfinished would re-query every source for it on
    every run, forever, for no gain.

    Two ways to widen that:

    ``retry_misses`` keeps every cached hit but ignores the retry ceiling, so
    tracks that failed before are asked again. That is the switch to pull after
    registering a new source: the misses are exactly the population the new
    source exists to serve, and re-confirming thousands of known answers would
    cost hours for nothing.

    ``retry_incomplete`` re-attempts rows that exist but cannot be sequenced —
    a tempo with no key. That is a different population from the misses: these
    tracks *were* answered, just not fully, and the answer that completes them
    usually comes from a source with less standing than the one already on
    file. Cheaper than ``refresh`` by the whole complete majority of a library.

    ``retry_energy`` re-attempts rows that are perfectly sequenceable but carry
    no energy. They are not incomplete by the old definition — BPM and key are
    both there — yet without energy they cannot take part in the energy arc, and
    the dsp source analysed thousands of them before it measured energy at all.
    A third population again, and the cheapest way to fill it without asking the
    catalogues to re-confirm answers already on file.

    ``refresh`` re-attempts *everything*, cached hits included. Only useful
    when an existing source's data is itself suspect.
    """
    items = tracks if tracks is not None else db.all_tracks(conn)
    stats = EnrichmentStats(considered=len(items))

    cached = db.all_features(conn)
    exhausted = (
        set()
        if (refresh or retry_misses or retry_incomplete or retry_energy)
        else db.exhausted_ids(conn)
    )

    pending = []
    for t in items:
        on_file = cached.get(t.spotify_id)
        incomplete = on_file is not None and not on_file.is_usable
        no_energy = on_file is not None and on_file.energy is None
        if not refresh and on_file is not None:
            retry_for_key = retry_incomplete and incomplete
            retry_for_energy = retry_energy and no_energy
            if not (retry_for_key or retry_for_energy):
                stats.already_cached += 1
                continue
            if retry_for_energy and not retry_for_key and not _energy_fillable(resolver, on_file):
                stats.skipped_unfillable += 1
                continue
        if t.spotify_id in exhausted:
            stats.skipped_exhausted += 1
            continue
        pending.append(t)

    total = len(pending)
    for i, track in enumerate(pending, 1):
        if cancel is not None and cancel.is_set():
            stats.cancelled = True
            conn.commit()
            log.info("Enrichment cancelled after %d/%d — progress saved.", i - 1, total)
            break

        features, reason = resolver.resolve(track)
        if features is not None:
            existing = cached.get(track.spotify_id)
            candidate_priority = resolver.priority_of(features.source)
            if (
                candidate_priority is not None
                and resolver.should_overwrite(existing, candidate_priority)
                and not _would_lose_the_key(features, existing)
            ):
                db.upsert_features(conn, features)
                cached[track.spotify_id] = features
                stats.written += 1
            else:
                # The row stands, but the answer may still carry a key the row
                # is missing — worth taking even from a source that has less
                # standing on tempo. Filling a gap is not overruling anyone.
                merged = merged_with_key(existing, features)
                if merged is not None:
                    stats.keys_merged += 1
                # On top of the key, not instead of it: one DSP pass can be
                # the only thing that has either for a given row.
                with_energy = merged_with_energy(merged or existing, features)
                if with_energy is not None:
                    merged = with_energy
                    stats.energy_merged += 1
                if merged is not None:
                    db.upsert_features(conn, merged)
                    cached[track.spotify_id] = merged
                else:
                    stats.discarded += 1
            db.clear_miss(conn, track.spotify_id)
            stats.resolved += 1
        else:
            db.record_miss(conn, track.spotify_id, reason or "unknown")
            stats.missed += 1

        if i % commit_every == 0:
            conn.commit()
        progress(i, total, f"{track.artist} — {track.title}")

    conn.commit()
    return stats
