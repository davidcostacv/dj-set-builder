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
from ..models import Track
from .base import Resolver

log = logging.getLogger(__name__)

Progress = Callable[[int, int, str], None]


@dataclass
class EnrichmentStats:
    considered: int = 0
    already_cached: int = 0
    skipped_exhausted: int = 0
    resolved: int = 0
    missed: int = 0
    cancelled: bool = False


def _noop(done: int, total: int, label: str) -> None:
    if done % 25 == 0 or done == total:
        log.info("  enrichment %d/%d — %s", done, total, label)


def enrich_tracks(
    conn: sqlite3.Connection,
    resolver: Resolver,
    tracks: list[Track] | None = None,
    *,
    cancel: threading.Event | None = None,
    progress: Progress = _noop,
    commit_every: int = 10,
) -> EnrichmentStats:
    items = tracks if tracks is not None else db.all_tracks(conn)
    stats = EnrichmentStats(considered=len(items))

    cached = db.all_features(conn)
    exhausted = db.exhausted_ids(conn)

    pending = []
    for t in items:
        existing = cached.get(t.spotify_id)
        if existing is not None and existing.is_usable:
            stats.already_cached += 1
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
            if candidate_priority is None or resolver.should_overwrite(
                existing, candidate_priority
            ):
                db.upsert_features(conn, features)
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
