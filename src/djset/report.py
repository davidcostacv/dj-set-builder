"""Coverage report — the go/no-go gate before any sequencing code is written.

Decision rule from the build brief:
  >=70% BPM+key  -> proceed as specified
  50-70%         -> tune the normalizer against the unresolved list, re-run
  <50% after tuning -> build RekordboxXMLSource, or descope to genre-splitting
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from . import db


@dataclass
class Coverage:
    total_tracks: int = 0  # whole library, for context
    measured: int = 0  # tracks actually attempted — the percentage denominator
    with_bpm: int = 0
    with_key: int = 0
    with_both: int = 0
    by_source: Counter[str] = field(default_factory=Counter)
    unresolved_artists: list[tuple[str, int]] = field(default_factory=list)
    raw_genres: list[tuple[str, int]] = field(default_factory=list)
    untagged_tracks: int = 0
    artists_cached: int = 0
    artists_referenced: int = 0
    miss_reasons: list[tuple[str, int]] = field(default_factory=list)

    def pct(self, n: int) -> float:
        """Percentage of the MEASURED set, not the library.

        Coverage is measured on a sample; dividing by the whole library would
        report ~2% after a 300-track sample of a 10k library and produce a
        meaningless verdict.
        """
        return (100.0 * n / self.measured) if self.measured else 0.0

    def pct_of_library(self, n: int) -> float:
        """For genre stats, which come from artist tags and do not depend on
        whether a track was enriched."""
        return (100.0 * n / self.total_tracks) if self.total_tracks else 0.0

    @property
    def verdict(self) -> str:
        if not self.measured:
            return (
                "NO DATA — nothing has been enriched yet. Run "
                "`djset enrich --sample 300` first."
            )
        p = self.pct(self.with_both)
        if p >= 70:
            return "PROCEED — coverage is good enough to build the sequencing engine."
        if p >= 50:
            return (
                "TUNE — most misses at this level are matching failures, not missing "
                "data. Tune the normalizer against the unresolved list and re-run "
                "before building anything else."
            )
        return (
            "STOP — do not build the sequencing engine on this. Either implement "
            "RekordboxXMLSource and re-measure, or descope to genre-splitting only."
        )


def build_coverage(conn: sqlite3.Connection, top_n: int = 20) -> Coverage:
    cov = Coverage()

    tracks = db.all_tracks(conn)
    cov.total_tracks = len(tracks)
    features = db.all_features(conn)
    attempted = set(features) | set(db.miss_reasons(conn))

    # Only tracks that were actually looked up count toward the percentages.
    measured_tracks = [t for t in tracks if t.spotify_id in attempted]
    cov.measured = len(measured_tracks)

    for t in measured_tracks:
        f = features.get(t.spotify_id)
        if f is None:
            continue
        has_bpm = f.bpm is not None
        has_key = f.key_camelot is not None
        cov.with_bpm += int(has_bpm)
        cov.with_key += int(has_key)
        cov.with_both += int(has_bpm and has_key)
        if has_bpm or has_key:
            cov.by_source[f.source] += 1

    # Unresolved artists: measured tracks with no usable features, by artist.
    unresolved: Counter[str] = Counter()
    for t in measured_tracks:
        f = features.get(t.spotify_id)
        if f is None or not f.is_usable:
            unresolved[t.artist] += 1
    cov.unresolved_artists = unresolved.most_common(top_n)

    # Raw genre tags across the library, before alias collapsing.
    genres_by_artist = db.artist_genres(conn)
    cov.artists_cached = len(genres_by_artist)

    referenced: set[str] = set()
    raw: Counter[str] = Counter()
    for t in tracks:
        referenced.update(t.artist_ids)
        tags: set[str] = set()
        for aid in t.artist_ids:
            tags.update(genres_by_artist.get(aid, []))
        if not tags:
            cov.untagged_tracks += 1
        for tag in tags:
            raw[tag] += 1
    cov.artists_referenced = len(referenced)
    cov.raw_genres = raw.most_common(30)

    reasons: Counter[str] = Counter()
    for reason in db.miss_reasons(conn).values():
        reasons[(reason or "unknown")[:80]] += 1
    cov.miss_reasons = reasons.most_common(10)

    return cov


def format_coverage(cov: Coverage) -> str:
    L: list[str] = []
    add = L.append

    add("=" * 66)
    add("  ENRICHMENT COVERAGE REPORT")
    add("=" * 66)
    add("")
    add(f"  Tracks in library       : {cov.total_tracks}")
    add(f"  Tracks measured         : {cov.measured}   (percentages are of this)")
    if cov.measured and cov.measured < cov.total_tracks:
        add(
            f"                            sample of "
            f"{cov.pct_of_library(cov.measured):.1f}% of the library"
        )
    add("")
    add(f"  With BPM                : {cov.with_bpm:>6}  ({cov.pct(cov.with_bpm):.1f}%)")
    add(f"  With key                : {cov.with_key:>6}  ({cov.pct(cov.with_key):.1f}%)")
    add(
        f"  With BPM + key          : {cov.with_both:>6}  ({cov.pct(cov.with_both):.1f}%)"
        "   <-- the number that decides the project"
    )
    add("")

    add("  By source")
    add("  " + "-" * 40)
    if cov.by_source:
        for source, n in cov.by_source.most_common():
            add(f"    {source:<16} {n:>6}  ({cov.pct(n):.1f}%)")
    else:
        add("    (nothing enriched yet)")
    add("")

    add(f"  Artist genre tags cached: {cov.artists_cached}/{cov.artists_referenced} artists")
    add(
        f"  Tracks with no genre tag: {cov.untagged_tracks} "
        f"({cov.pct_of_library(cov.untagged_tracks):.1f}% of library)"
        "  -> the 'Unknown' bucket"
    )
    add("")

    add(f"  Top {len(cov.unresolved_artists)} unresolved artists (tune the normalizer against these)")
    add("  " + "-" * 60)
    if cov.unresolved_artists:
        for artist, n in cov.unresolved_artists:
            add(f"    {n:>5}  {artist[:52]}")
    else:
        add("    (none — everything resolved)")
    add("")

    add(f"  Top {len(cov.raw_genres)} raw genre tags (pre-alias)")
    add("  " + "-" * 60)
    if cov.raw_genres:
        for tag, n in cov.raw_genres:
            add(f"    {n:>5}  {tag[:52]}")
    else:
        add("    (no artist genres cached — run `djset sync` first)")
    add("")

    if cov.miss_reasons:
        add("  Miss reasons")
        add("  " + "-" * 60)
        for reason, n in cov.miss_reasons:
            add(f"    {n:>5}  {reason}")
        add("")

    add("=" * 66)
    add(f"  VERDICT: {cov.verdict}")
    add("=" * 66)
    return "\n".join(L)


def coverage_as_json(cov: Coverage) -> str:
    return json.dumps(
        {
            "total_tracks": cov.total_tracks,
            "measured": cov.measured,
            "with_bpm": cov.with_bpm,
            "with_key": cov.with_key,
            "with_both": cov.with_both,
            "pct_both": round(cov.pct(cov.with_both), 2),
            "by_source": dict(cov.by_source),
            "untagged_tracks": cov.untagged_tracks,
            "unresolved_artists": cov.unresolved_artists,
            "raw_genres": cov.raw_genres,
            "verdict": cov.verdict,
        },
        indent=2,
    )
