"""Genre filtering — layer 3. Pure functions, zero network calls.

Two rules from the brief drive the whole design:

* **Genre is an OPTIONAL FILTER.** It decides which tracks are *eligible*. It
  cannot order anything, and it can be skipped entirely.
* **No selection means no filter**, which is a different state from "everything
  selected". They are kept distinct here so the eligible-count display stays
  honest — and so the UI can never turn "I didn't choose" into "I chose none",
  which would yield an empty result.

Spotify tags genres on artists, never tracks, so a track inherits the union of
its artists' tags.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .models import AudioFeatures, Track

# The bucket for tracks whose artists carry no genre tags at all. Selectable
# like any other genre, so it can be included or excluded deliberately.
UNKNOWN = "Unknown"


def canonical_genre(raw: str, aliases: dict[str, str]) -> str:
    """Collapse a raw Spotify tag to its canonical name."""
    key = raw.strip().lower()
    return aliases.get(key, key)


def track_genres(
    track: Track,
    artist_genres: dict[str, list[str]],
    aliases: dict[str, str],
) -> set[str]:
    """Canonical genres for one track: the union of its artists' tags.

    Returns an empty set for untagged tracks — callers map that to
    :data:`UNKNOWN` rather than this function inventing a bucket.
    """
    out: set[str] = set()
    for aid in track.artist_ids:
        for raw in artist_genres.get(aid, ()):
            if raw and raw.strip():
                out.add(canonical_genre(raw, aliases))
    return out


def bucket_of(
    track: Track,
    artist_genres: dict[str, list[str]],
    aliases: dict[str, str],
) -> set[str]:
    """Genres for filtering, with untagged tracks mapped to UNKNOWN."""
    return track_genres(track, artist_genres, aliases) or {UNKNOWN}


@dataclass(frozen=True)
class GenreStat:
    name: str
    track_count: int
    enriched_count: int  # of which this many have BPM + key

    @property
    def is_unknown(self) -> bool:
        return self.name == UNKNOWN


def genre_index(
    tracks: list[Track],
    artist_genres: dict[str, list[str]],
    aliases: dict[str, str],
    features: dict[str, AudioFeatures] | None = None,
) -> list[GenreStat]:
    """Genre rows for the checkbox list, most tracks first.

    Built from the tags actually present in the current selection — never a
    global genre list — because a genre with no tracks here is noise. The
    enriched count rides along because a genre with 200 tracks and 12 enriched
    will not produce a set, and that has to be visible *before* generating.
    """
    features = features or {}
    totals: Counter[str] = Counter()
    enriched: Counter[str] = Counter()

    for t in tracks:
        usable = _is_usable(features.get(t.spotify_id))
        for g in bucket_of(t, artist_genres, aliases):
            totals[g] += 1
            if usable:
                enriched[g] += 1

    stats = [GenreStat(g, n, enriched[g]) for g, n in totals.items()]
    # Track count descending, then name — Unknown sinks to the end on ties so
    # it never heads the list.
    stats.sort(key=lambda s: (-s.track_count, s.is_unknown, s.name))
    return stats


def _is_usable(f: AudioFeatures | None) -> bool:
    return f is not None and f.bpm is not None and f.key_camelot is not None


def filter_tracks(
    tracks: list[Track],
    selected: set[str] | None,
    artist_genres: dict[str, list[str]],
    aliases: dict[str, str],
) -> list[Track]:
    """Apply the genre filter.

    ``selected`` of ``None`` or an empty set means **no filter** — every track
    is eligible. It never means "no tracks eligible", and it never raises.
    Multiple genres are OR: a track matching any selected genre is eligible.
    """
    if not selected:
        return list(tracks)
    return [
        t for t in tracks if bucket_of(t, artist_genres, aliases) & selected
    ]


@dataclass(frozen=True)
class EligibleSummary:
    """What the live counter under the genre pane shows."""

    total: int
    enriched: int
    filtered: bool  # False when no genre filter is applied

    @property
    def label(self) -> str:
        base = f"{self.total} tracks eligible"
        if not self.filtered:
            base += " (no genre filter)"
        return f"{base} — of which {self.enriched} have BPM+key"


def summarize(
    tracks: list[Track],
    selected: set[str] | None,
    artist_genres: dict[str, list[str]],
    aliases: dict[str, str],
    features: dict[str, AudioFeatures] | None = None,
) -> EligibleSummary:
    features = features or {}
    eligible = filter_tracks(tracks, selected, artist_genres, aliases)
    enriched = sum(1 for t in eligible if _is_usable(features.get(t.spotify_id)))
    return EligibleSummary(
        total=len(eligible), enriched=enriched, filtered=bool(selected)
    )


def sequenceable(
    tracks: list[Track], features: dict[str, AudioFeatures]
) -> list[Track]:
    """Tracks that can actually take part in sequencing (BPM + key present)."""
    return [t for t in tracks if _is_usable(features.get(t.spotify_id))]


def dedupe_by_isrc(tracks: list[Track]) -> list[Track]:
    """Drop repeats of the same recording across album / single / compilation.

    Order is preserved and the first occurrence wins. Tracks without an ISRC
    are always kept — a missing identifier is not evidence of a duplicate.
    """
    seen: set[str] = set()
    out: list[Track] = []
    for t in tracks:
        if t.isrc:
            if t.isrc in seen:
                continue
            seen.add(t.isrc)
        out.append(t)
    return out
