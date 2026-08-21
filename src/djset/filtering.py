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


@dataclass(frozen=True)
class GenreAvailability:
    """Why the genre pane looks the way it does.

    An empty genre list has three quite different causes, and the pane showing
    the same blankness for all of them is what makes it read as broken. They
    are told apart by counting artists rather than tags: an artist row exists
    once fetched, whether or not Spotify gave it any genres.

    Settled on 19 Aug 2026: Spotify has **removed** the field. The artist
    object no longer contains ``genres`` at all — checked against five artists
    including Lana Del Rey, the response carries only external_urls, href, id,
    images, name, type and uri. So this is not a fetch that might start working;
    it is a data source that no longer exists, and the wording says so.
    """

    artists: int  # distinct artists across the pool
    fetched: int  # of those, how many we hold a row for
    tagged: int  # of those, how many carry at least one tag

    @property
    def usable(self) -> bool:
        return self.tagged > 0

    @property
    def partial(self) -> bool:
        """Some artists fetched, some not — the state an interrupted sync leaves."""
        return 0 < self.fetched < self.artists

    @property
    def headline(self) -> str | None:
        """None when the filter works; otherwise the short reason it does not."""
        if self.artists == 0:
            return None  # nothing selected yet; there is nothing to explain
        if self.fetched == 0:
            return "Genre tags not fetched yet."
        if self.tagged == 0:
            # Say what was actually checked. Claiming all N artists are
            # untagged when only a fraction were ever fetched asserts
            # something nobody has looked at.
            if self.partial:
                return (
                    f"No genre tags in the {self.fetched} of {self.artists} "
                    "artists fetched so far."
                )
            return f"No genre tags for any of these {self.artists} artists."
        return None

    @property
    def detail(self) -> str | None:
        if self.artists == 0 or self.usable:
            return None

        unaffected = "Generate is unaffected — it never depends on genre."
        removed = (
            "Spotify removed artist genres from its API — the artist object no "
            "longer carries the field at all, so fetching more will not help."
        )
        if self.fetched == 0:
            # Deliberately no longer says "run Sync". Syncing cannot return a
            # field the API stopped sending, and sending someone to do it is
            # worse than saying nothing.
            return (
                f"{removed} None of the {self.artists} artists here have been "
                f"fetched, but doing so would not produce any. {unaffected}"
            )
        if self.partial:
            return (
                f"{removed} {self.fetched} of {self.artists} artists here were "
                f"fetched and none carried tags. {unaffected}"
            )
        return f"{removed} All {self.fetched} artists here came back without any. {unaffected}"


def genre_availability(
    tracks: list[Track], artist_genres: dict[str, list[str]]
) -> GenreAvailability:
    """Count artists three ways so the pane can say which case it is in.

    Deliberately not derived from :func:`genre_index`. When nothing is tagged
    every track lands in ``UNKNOWN``, so the index reports one healthy-looking
    bucket covering the whole library — which is exactly the state that needs
    explaining, not a state that explains itself.
    """
    artists: set[str] = set()
    for t in tracks:
        artists.update(aid for aid in t.artist_ids if aid)

    fetched = sum(1 for aid in artists if aid in artist_genres)
    tagged = sum(
        1
        for aid in artists
        if any(g and g.strip() for g in artist_genres.get(aid, ()))
    )
    return GenreAvailability(artists=len(artists), fetched=fetched, tagged=tagged)


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


def recording_key(track: Track) -> str:
    """Identity of a recording for dedupe when ISRCs disagree.

    Deliberately keeps version markers ("- Extended Mix", "(Radio Edit)") so a
    remix is NOT collapsed into its original — those are different recordings
    at different tempos and a DJ wants both available. It does strip featuring
    credits, so "LOYAL (feat. Drake)" and "LOYAL (feat. Drake and Bad Bunny)"
    resolve to the same song rather than playing back to back.
    """
    from .enrichment.normalize import normalize_artist, title_variants

    variants = title_variants(track.title)
    title = variants[0] if variants else track.title.lower()
    return f"{normalize_artist(track.primary_artist)}|{title}"


def song_family(track: Track) -> str:
    """Identifies a song across its versions — original, remix, edit, live.

    The counterpart to :func:`recording_key`, which deliberately keeps version
    markers so a remix is not *removed* as a duplicate. Both belong in a set;
    what does not belong is playing them back to back, which is the same song
    twice however different the tempo. ``title_variants`` already ends with the
    bare title, so the stripping is not repeated here.
    """
    from .enrichment.normalize import normalize_artist, title_variants

    variants = title_variants(track.title)
    bare = variants[-1] if variants else track.title.lower()
    return f"{normalize_artist(track.primary_artist)}|{bare}"


def dedupe_recordings(
    tracks: list[Track], features: dict[str, AudioFeatures] | None = None
) -> list[Track]:
    """Full duplicate removal: ISRC first, then artist+title.

    ISRC is authoritative but not sufficient — a re-release or remaster of the
    same song carries a different ISRC, which is how "This Love" by Maroon 5
    ended up in a generated set twice.

    When ``features`` is given, a group keeps the copy that can actually be
    sequenced. Taking whichever came first meant a playlist holding the same
    recording twice — once with a BPM and key, once with nothing — could keep
    the empty one and drop the usable one, and the survivor was then filtered
    out for having no data. One track short, for no reason a reader of the
    playlist could ever see.

    Order is unchanged: a group appears where its *first* member appeared,
    whichever member is chosen to represent it.
    """
    usable = (
        (lambda t: (f := features.get(t.spotify_id)) is not None and f.is_usable)
        if features is not None
        else (lambda t: False)
    )

    order: list[str] = []
    groups: dict[str, Track] = {}
    isrc_group: dict[str, str] = {}

    for t in tracks:
        key = recording_key(t)
        # An ISRC seen before pins this track to that group even when its
        # title differs, which is the case ISRC exists to catch.
        if t.isrc and t.isrc in isrc_group:
            key = isrc_group[t.isrc]
        if key not in groups:
            order.append(key)
            groups[key] = t
        elif usable(t) and not usable(groups[key]):
            groups[key] = t
        if t.isrc:
            isrc_group.setdefault(t.isrc, key)
    return [groups[k] for k in order]
