"""Set construction — layer 3. Pure functions, zero network calls.

Sequencing is a path-finding problem, not a sort. Sorting by BPM then key
dead-ends: a greedy sort commits to an early track that strands the rest of the
set. So the eligible tracks become nodes in a directed graph, an edge exists
where the active predicates allow a transition, and a beam search looks for the
longest high-quality path.

BPM and key are SEQUENCERS: they decide order and cannot filter. Genre is the
filter, applied upstream in :mod:`djset.filtering`.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field, replace
from enum import Enum

# key_distance/key_compatible are re-exported: they moved to `camelot`,
# where the wheel arithmetic belongs, and every caller here still works.
from .camelot import key_compatible, key_distance, parse_camelot
from .filtering import dedupe_recordings, song_family
from .models import AudioFeatures, Track

# ---------------------------------------------------------------------------
# compatibility predicates
# ---------------------------------------------------------------------------

# Measured on this library: a sweep of 0.02-0.12 over 172 real playlists in
# "whole selection" mode. Tightening does not make a set smoother, because the
# joins that hurt are not the legal ones - they are the forced seams where the
# search runs out of legal moves and stitches the leftovers on. At 0.03 a third
# of all joins were forced (vs a fifth at 0.06) and the share over 10 BPM went
# UP. 0.06 had the lowest mean jump, the fewest joins over 10 BPM and near the
# fewest over 6. Judge any change by all joins, never by `Transition.bpm_ratio`:
# it is None on every forced seam, so it only ever sees the joins that fit.
DEFAULT_TOLERANCE = 0.06
MIN_TOLERANCE = 0.02

# How far energy must fall before a transition counts as a dip rather than
# noise. Applied to the percentile energies from `comparable_energy`, so it
# reads as "more than two percent of the field lower", independent of whatever
# units the source happened to report in.
ENERGY_DIP = 0.02
MAX_TOLERANCE = 0.12

# How much of a transition's score the arc is allowed to decide. Small on
# purpose: BPM and key make a join possible, energy only decides where in the
# night it belongs.
ENERGY_WEIGHT = 0.25


class SequenceMode(str, Enum):
    """Which predicates are active. One code path, three flags."""

    BPM = "bpm"
    KEY = "key"
    BPM_KEY = "bpm+key"

    @property
    def uses_bpm(self) -> bool:
        return self in (SequenceMode.BPM, SequenceMode.BPM_KEY)

    @property
    def uses_key(self) -> bool:
        return self in (SequenceMode.KEY, SequenceMode.BPM_KEY)


def bpm_ratio(a: float, b: float) -> float:
    """Relative BPM difference, per the brief: |a-b| / min(a,b)."""
    lo = min(a, b)
    if lo <= 0:
        return float("inf")
    return abs(a - b) / lo


def bpm_distance(
    a: float, b: float, *, tolerance: float = DEFAULT_TOLERANCE, half_double: bool = True
) -> float | None:
    """Best achievable BPM difference, considering half- and double-time.

    Returns the ratio if within tolerance, else None. Testing ``b*2`` and
    ``b/2`` is what lets a 70 BPM track follow a 140 BPM one.
    """
    candidates = [b]
    if half_double:
        candidates += [b * 2.0, b / 2.0]
    best: float | None = None
    for c in candidates:
        r = bpm_ratio(a, c)
        if r <= tolerance and (best is None or r < best):
            best = r
    return best


def bpm_compatible(
    a: float, b: float, *, tolerance: float = DEFAULT_TOLERANCE, half_double: bool = True
) -> bool:
    return bpm_distance(a, b, tolerance=tolerance, half_double=half_double) is not None


# ---------------------------------------------------------------------------
# transition quality
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """Quality of moving from one track to the next."""

    bpm_ratio: float | None
    key_distance: int | None
    energy_delta: float | None
    quality: float  # 0..1, higher is smoother

    @property
    def label(self) -> str:
        if self.quality >= 0.8:
            return "excellent"
        if self.quality >= 0.6:
            return "good"
        if self.quality >= 0.4:
            return "fair"
        return "rough"


@dataclass(frozen=True)
class SequenceOptions:
    mode: SequenceMode = SequenceMode.BPM_KEY
    tolerance: float = DEFAULT_TOLERANCE
    half_double: bool = True
    energy_boost: bool = False
    # Shape the set as a climb rather than only a legal path, the way Mixed In
    # Key tells DJs to build one. Off by default: on this library only 41% of
    # tracks carry an energy reading, and measured against real playlists the
    # arc helped one and hurt another, so it is offered rather than imposed.
    energy_arc: bool = False
    target_tracks: int | None = None
    target_minutes: float | None = None
    # Reorder everything that is eligible, rather than picking a fixed count.
    # Used for "take this playlist and put it in mixable order".
    use_all: bool = False
    start_track_id: str | None = None
    beam_width: int = 10

    def clamped_tolerance(self) -> float:
        return max(MIN_TOLERANCE, min(MAX_TOLERANCE, self.tolerance))


def energy_target(position: int, length: int) -> float:
    """Where on the climb the slot at ``position`` of ``length`` should sit.

    A straight ramp from the quiet end of the pool to the loud end. Energy is
    already a within-pool percentile by the time it gets here, so 0 is the
    calmest track available and 1 the hardest — the ramp spans whatever range
    this particular selection actually has.
    """
    if length <= 1:
        return 1.0
    return position / (length - 1)


def energy_fit(energy: float | None, position: int, length: int) -> float:
    """How well a track suits that slot: +1 dead on the ramp, -1 at the far end.

    Judged against the *position*, not against the previous track. Rewarding a
    one-level step was the earlier mistake and it backfired: the search chained
    gentle little rises, never reached the loud end, and finished sets lower
    than with the rule switched off. Scoring the slot keeps the whole climb in
    view — putting a peak track third costs exactly as much as ending on a
    quiet one.

    Unknown energy scores zero: neither chased nor avoided, so the half of a
    library with no reading does not distort the order.
    """
    if energy is None:
        return 0.0
    return 1.0 - 2.0 * abs(energy - energy_target(position, length))


def transition(
    a: AudioFeatures, b: AudioFeatures, opts: SequenceOptions
) -> Transition | None:
    """Score a candidate transition, or None if the predicates forbid it."""
    tol = opts.clamped_tolerance()

    ratio: float | None = None
    if opts.mode.uses_bpm:
        if a.bpm is None or b.bpm is None:
            return None
        ratio = bpm_distance(a.bpm, b.bpm, tolerance=tol, half_double=opts.half_double)
        if ratio is None:
            return None
    elif a.bpm is not None and b.bpm is not None:
        ratio = bpm_ratio(a.bpm, b.bpm)

    dist: int | None = None
    if opts.mode.uses_key:
        if not a.key_camelot or not b.key_camelot:
            return None
        dist = key_distance(
            a.key_camelot, b.key_camelot, energy_boost=opts.energy_boost
        )
        if dist is None:
            return None
    elif a.key_camelot and b.key_camelot:
        dist = key_distance(a.key_camelot, b.key_camelot, energy_boost=True)

    energy_delta = (
        b.energy - a.energy if a.energy is not None and b.energy is not None else None
    )

    # Quality: a tight BPM match and a small harmonic move are both smooth.
    parts: list[float] = []
    if opts.mode.uses_bpm and ratio is not None:
        parts.append(1.0 - min(1.0, ratio / tol) if tol else 1.0)
    if opts.mode.uses_key and dist is not None:
        parts.append({0: 1.0, 1: 0.75, 2: 0.5}.get(dist, 0.3))
    if not parts:
        parts.append(0.5)

    quality = sum(parts) / len(parts)
    return Transition(
        bpm_ratio=ratio, key_distance=dist, energy_delta=energy_delta, quality=quality
    )


# ---------------------------------------------------------------------------
# the graph
# ---------------------------------------------------------------------------


class LimitingFactor(str, Enum):
    NONE = "none"
    GENRE_FILTER = "genre filter"
    COVERAGE = "enrichment coverage"
    BPM_TOLERANCE = "BPM tolerance"
    KEY_CONSTRAINTS = "key compatibility"


@dataclass
class SetResult:
    tracks: list[Track] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    requested: int = 0
    pool_size: int = 0
    limiting_factor: LimitingFactor = LimitingFactor.NONE
    # Transitions that had to break the active predicates, which only happens
    # in "reorder everything" mode where placing every track is the point.
    compromises: int = 0
    # Tracks carried at the end because they have no BPM or key to sequence on.
    # They are part of `tracks` — the count is here so everything downstream
    # can say so rather than presenting them as though they were mixed in.
    appended: int = 0

    @property
    def sequenced(self) -> list[Track]:
        """The part that was actually put in order."""
        return self.tracks[: len(self.tracks) - self.appended] if self.appended else self.tracks

    @property
    def reached_target(self) -> bool:
        return len(self.sequenced) >= self.requested

    @property
    def average_quality(self) -> float:
        if not self.transitions:
            return 0.0
        return sum(t.quality for t in self.transitions) / len(self.transitions)

    @property
    def total_duration_ms(self) -> int:
        return sum(t.duration_ms or 0 for t in self.tracks)

    def explain(self) -> str:
        """Never silently pad a short set — say what ran out."""
        if self.reached_target:
            base = (
                f"{len(self.tracks)} tracks, average transition quality "
                f"{self.average_quality:.0%}."
            )
            if self.compromises:
                base += (
                    f" {self.compromises} transition(s) had to break the "
                    "BPM/key rules to place every track."
                )
            if self.appended:
                base += (
                    f" {self.appended} track(s) have no BPM or key, so they are "
                    "carried at the end rather than dropped."
                )
            return base
        return (
            f"Found {len(self.tracks)} of {self.requested} requested tracks. "
            f"The limiting factor was the {self.limiting_factor.value} "
            f"(pool of {self.pool_size} sequenceable tracks)."
        )


class TrackGraph:
    """Adjacency over the eligible set, indexed so neighbour lookup is cheap.

    A dense adjacency matrix would be O(n^2) — 8M pairs on a 2,800-track pool.
    Instead candidates are narrowed by key bucket and/or a BPM window first.
    """

    def __init__(
        self,
        tracks: list[Track],
        features: dict[str, AudioFeatures],
        opts: SequenceOptions,
    ) -> None:
        self.opts = opts
        self.tracks = [t for t in tracks if _usable(features.get(t.spotify_id), opts)]
        self.features = features
        self.feat = [features[t.spotify_id] for t in self.tracks]

        # Which song each node is a version of, computed once. Used to keep an
        # original and its own remix from landing next to each other.
        self._family = [song_family(t) for t in self.tracks]

        self._by_key: dict[str, list[int]] = {}
        for i, f in enumerate(self.feat):
            if f.key_camelot:
                self._by_key.setdefault(f.key_camelot, []).append(i)

        indexed = sorted(
            (f.bpm, i) for i, f in enumerate(self.feat) if f.bpm is not None
        )
        self._bpm_values = [b for b, _ in indexed]
        self._bpm_index = [i for _, i in indexed]

        self._cache: dict[int, list[tuple[int, Transition]]] = {}

    def __len__(self) -> int:
        return len(self.tracks)

    # -- candidate narrowing -------------------------------------------------
    def _candidates(self, i: int) -> set[int]:
        f = self.feat[i]
        opts = self.opts

        by_key: set[int] | None = None
        if opts.mode.uses_key and f.key_camelot:
            by_key = set()
            for code in _compatible_codes(f.key_camelot, opts.energy_boost):
                by_key.update(self._by_key.get(code, ()))

        by_bpm: set[int] | None = None
        if opts.mode.uses_bpm and f.bpm is not None:
            by_bpm = set()
            tol = opts.clamped_tolerance()
            centres = [f.bpm]
            if opts.half_double:
                centres += [f.bpm * 2.0, f.bpm / 2.0]
            for c in centres:
                lo, hi = c / (1.0 + tol) - 1e-9, c * (1.0 + tol) + 1e-9
                left = bisect.bisect_left(self._bpm_values, lo)
                right = bisect.bisect_right(self._bpm_values, hi)
                by_bpm.update(self._bpm_index[left:right])

        if by_key is not None and by_bpm is not None:
            return by_key & by_bpm
        if by_key is not None:
            return by_key
        if by_bpm is not None:
            return by_bpm
        return set(range(len(self.tracks)))

    def same_song(self, i: int, j: int) -> bool:
        """Whether two nodes are versions of one song.

        Deliberately *not* a reason to drop either — a remix is a different
        recording and belongs in the pool. It is only a reason not to play them
        consecutively, which is the same song twice however different the tempo.
        """
        return self._family[i] == self._family[j]

    def neighbours(self, i: int) -> list[tuple[int, Transition]]:
        cached = self._cache.get(i)
        if cached is not None:
            return cached
        out: list[tuple[int, Transition]] = []
        for j in self._candidates(i):
            if j == i:
                continue
            tr = transition(self.feat[i], self.feat[j], self.opts)
            if tr is not None:
                out.append((j, tr))
        out.sort(key=lambda p: -p[1].quality)
        self._cache[i] = out
        return out


def _usable(f: AudioFeatures | None, opts: SequenceOptions) -> bool:
    if f is None:
        return False
    if opts.mode.uses_bpm and f.bpm is None:
        return False
    if opts.mode.uses_key and not f.key_camelot:
        return False
    return True


def _compatible_codes(code: str, energy_boost: bool) -> list[str]:
    try:
        n, letter = parse_camelot(code)
    except ValueError:
        return []
    other = "B" if letter == "A" else "A"
    codes = [
        f"{n}{letter}",
        f"{(n % 12) + 1}{letter}",
        f"{((n - 2) % 12) + 1}{letter}",
        f"{n}{other}",
    ]
    if energy_boost:
        codes.append(f"{((n + 6) % 12) + 1}{letter}")
    return codes


# ---------------------------------------------------------------------------
# beam search
# ---------------------------------------------------------------------------


@dataclass
class _Beam:
    path: list[int]
    used: set[int]
    score: float
    dips: int  # energy decreases so far


def comparable_energy(
    features: dict[str, AudioFeatures],
) -> dict[str, AudioFeatures]:
    """Rewrite ``energy`` as a within-source percentile so deltas mean something.

    Energy is the one field with no universal unit. BPM is beats per minute
    everywhere and key normalises to Camelot, but every source measures energy
    its own way: GetSongBPM reports danceability on 0-100, Essentia reports a
    detrended-fluctuation figure on roughly 0-3, and the two do not describe
    the same quantity on the same scale even after both are squeezed into 0-1.

    That matters because energy is not merely displayed — a drop of more than
    ``ENERGY_DIP`` prunes the edge out of the beam search entirely. Mixing raw
    values from two sources would make every cross-source transition look like
    a collapse and quietly delete those edges, so the graph would *shrink* as
    coverage improved. Missing energy is safe (the rule is skipped); wrongly
    scaled energy is not.

    Ranking within each source removes the scale question rather than trying to
    calibrate it away: a value becomes "how energetic is this compared with the
    others measured the same way", which is comparable across sources by
    construction. Ranks are taken over the pool being sequenced, so energy is
    relative to the candidates actually in play — which is the only comparison
    a single set ever needs to make.
    """
    by_source: dict[str, list[tuple[float, str]]] = {}
    for sid, f in features.items():
        if f.energy is not None:
            by_source.setdefault(f.source, []).append((f.energy, sid))

    if not by_source:
        return features

    ranked: dict[str, float] = {}
    for rows in by_source.values():
        rows.sort()
        # A single sample has no distribution to sit in; call it the midpoint
        # so it neither reads as a peak nor as a trough.
        if len(rows) == 1:
            ranked[rows[0][1]] = 0.5
            continue
        last = len(rows) - 1
        # Tied values must share a rank. GetSongBPM's danceability is integer
        # derived, so ties are common — spreading them over distinct ranks
        # would manufacture energy differences between tracks the source
        # called identical, and those fake dips would prune real edges.
        i = 0
        while i < len(rows):
            j = i
            while j + 1 < len(rows) and rows[j + 1][0] == rows[i][0]:
                j += 1
            midrank = ((i + j) / 2.0) / last
            for k in range(i, j + 1):
                ranked[rows[k][1]] = midrank
            i = j + 1

    return {
        sid: (replace(f, energy=ranked[sid]) if sid in ranked else f)
        for sid, f in features.items()
    }


def build_set(
    tracks: list[Track],
    features: dict[str, AudioFeatures],
    opts: SequenceOptions,
    *,
    eligible_before_filter: int | None = None,
) -> SetResult:
    """Sequence a set from the (already genre-filtered) eligible tracks."""
    # Deduplicate by ISRC before anything else. The same recording routinely
    # appears as an album track, a single and a compilation cut, each with its
    # own Spotify id — so per-id uniqueness is not enough to stop a set playing
    # the same song twice.
    tracks = dedupe_recordings(tracks, features)
    features = comparable_energy(features)
    graph = TrackGraph(tracks, features, opts)
    target = _target_length(graph, opts)

    result = SetResult(requested=target, pool_size=len(graph))
    if not graph.tracks:
        # Nothing sequenceable at all: blame the filter only if it is what
        # emptied the pool, otherwise this is missing BPM/key data.
        filtered_everything = (
            eligible_before_filter is not None
            and eligible_before_filter > 0
            and not tracks
        )
        result.limiting_factor = (
            LimitingFactor.GENRE_FILTER if filtered_everything else LimitingFactor.COVERAGE
        )
        return result

    starts = _starting_points(graph, opts)
    best: _Beam | None = None

    for start in starts:
        seed_score = (
            ENERGY_WEIGHT * energy_fit(graph.feat[start].energy, 0, target)
            if opts.energy_arc
            else 0.0
        )
        beams = [_Beam(path=[start], used={start}, score=seed_score, dips=0)]
        while beams:
            if best is None or len(beams[0].path) > len(best.path):
                best = max(beams, key=lambda b: (len(b.path), b.score))
            if len(beams[0].path) >= target:
                break

            nxt: list[_Beam] = []
            for beam in beams:
                for j, tr in graph.neighbours(beam.path[-1]):
                    if j in beam.used:
                        continue
                    if graph.same_song(beam.path[-1], j):
                        # An original followed by its own remix is the same
                        # song twice, however different the tempo. Dedupe keeps
                        # both on purpose; adjacency is what makes it obvious.
                        continue
                    dips = beam.dips
                    if tr.energy_delta is not None and tr.energy_delta < -ENERGY_DIP:
                        # Prefer a gentle upward arc; allow one dip, and only
                        # once the set is into its back third.
                        in_back_third = len(beam.path) >= (2 * target) // 3
                        if beam.dips >= 1 or not in_back_third:
                            continue
                        dips += 1
                    # Quality says the join works; the arc says it belongs
                    # here rather than somewhere else in the night. Kept out of
                    # `quality` so the number shown against the row stays a
                    # statement about BPM and key alone.
                    step = tr.quality
                    if opts.energy_arc:
                        step += ENERGY_WEIGHT * energy_fit(
                            graph.feat[j].energy, len(beam.path), target
                        )
                    nxt.append(
                        _Beam(
                            path=[*beam.path, j],
                            used={*beam.used, j},
                            score=beam.score + step,
                            dips=dips,
                        )
                    )
            if not nxt:
                break
            nxt.sort(key=lambda b: (-len(b.path), -b.score))
            beams = nxt[: max(1, opts.beam_width)]

        candidate = max(beams, key=lambda b: (len(b.path), b.score)) if beams else None
        for c in (candidate, best):
            if c and (best is None or (len(c.path), c.score) > (len(best.path), best.score)):
                best = c
        if best and len(best.path) >= target:
            break

    if best is None:
        return result

    path = best.path[:target]

    if opts.use_all and len(path) < len(graph):
        # "Reorder everything" means every track gets a place. The strict
        # predicates rarely admit a single path through hundreds of tracks, so
        # the leftovers are appended at the least-bad seam available. Those
        # seams are counted and shown, never hidden — that is the difference
        # between an honest compromise and silent padding.
        path, result.compromises = _place_remaining(graph, path, opts)

    result.tracks = [graph.tracks[i] for i in path]
    result.transitions = [
        transition(graph.feat[a], graph.feat[b], opts) or Transition(None, None, None, 0.0)
        for a, b in zip(path, path[1:])
    ]

    if opts.use_all:
        # Asking for the whole selection means the whole selection. A track
        # with no BPM or key cannot be *mixed* into an order, but dropping it
        # loses it from the playlist entirely — which reads as the app quietly
        # eating songs. Carry them at the end instead, where they are visibly
        # unsequenced rather than invisibly gone.
        #
        # Only here: an explicit "20 tracks" is a request to choose 20, and
        # nothing is being dropped when the rest were never asked for.
        placed = {t.spotify_id for t in result.tracks}
        leftovers = [t for t in tracks if t.spotify_id not in placed]
        result.tracks.extend(leftovers)
        result.appended = len(leftovers)

    if not result.reached_target:
        result.limiting_factor = _diagnose(
            graph, opts, eligible_before_filter, len(tracks)
        )
    return result


def _soft_distance(a: AudioFeatures, b: AudioFeatures) -> float:
    """How bad a transition is when the strict predicates already failed.

    Used only to order the leftovers in "reorder everything" mode, so that a
    forced seam is at least the gentlest one available.
    """
    cost = 0.0
    if a.bpm and b.bpm:
        cost += min(bpm_ratio(a.bpm, b.bpm), bpm_ratio(a.bpm, b.bpm * 2),
                    bpm_ratio(a.bpm, b.bpm / 2)) * 10.0
    else:
        cost += 5.0
    if a.key_camelot and b.key_camelot:
        try:
            an, al = parse_camelot(a.key_camelot)
            bn, bl = parse_camelot(b.key_camelot)
            steps = min((bn - an) % 12, (an - bn) % 12)
            cost += steps + (0 if al == bl else 1)
        except ValueError:
            cost += 6.0
    else:
        cost += 3.0
    return cost


def _place_remaining(
    graph: TrackGraph, path: list[int], opts: SequenceOptions
) -> tuple[list[int], int]:
    """Append every unplaced track, preferring valid moves over forced ones."""
    placed = set(path)
    remaining = [i for i in range(len(graph)) if i not in placed]
    out = list(path)
    compromises = 0

    while remaining:
        tail = out[-1]
        left = set(remaining)
        # A legal continuation is always better than a forced one, and a
        # different song is better than either — an original followed by its
        # own remix is the same song twice.
        legal = [(j, tr) for j, tr in graph.neighbours(tail) if j in left]
        fresh = [(j, tr) for j, tr in legal if not graph.same_song(tail, j)]
        if fresh:
            j = max(fresh, key=lambda p: p[1].quality)[0]
        elif legal:
            j = max(legal, key=lambda p: p[1].quality)[0]
        else:
            others = [k for k in remaining if not graph.same_song(tail, k)] or remaining
            j = min(others, key=lambda k: _soft_distance(graph.feat[tail], graph.feat[k]))
            compromises += 1
        out.append(j)
        remaining.remove(j)

    return out, compromises


def _target_length(graph: TrackGraph, opts: SequenceOptions) -> int:
    if opts.use_all:
        # Every sequenceable track. The search will still stop early if the
        # graph cannot be traversed that far, and the result reports why.
        return max(1, len(graph))
    if opts.target_tracks:
        return max(1, opts.target_tracks)
    if opts.target_minutes:
        durations = [t.duration_ms or 0 for t in graph.tracks if t.duration_ms]
        avg = (sum(durations) / len(durations)) if durations else 210_000
        return max(1, round((opts.target_minutes * 60_000) / avg))
    return min(20, len(graph.tracks)) or 1


def _starting_points(graph: TrackGraph, opts: SequenceOptions) -> list[int]:
    if opts.start_track_id:
        for i, t in enumerate(graph.tracks):
            if t.spotify_id == opts.start_track_id:
                return [i]
    # Try the best-connected tracks first — they strand the search least often.
    ranked = sorted(
        range(len(graph.tracks)), key=lambda i: -len(graph.neighbours(i))
    )
    return ranked[: max(1, opts.beam_width)]


def _diagnose(
    graph: TrackGraph,
    opts: SequenceOptions,
    eligible_before_filter: int | None,
    eligible: int,
) -> LimitingFactor:
    """Say which constraint actually ran out, rather than padding the set.

    Three separate narrowings happen in sequence, and only the binding one
    should be reported:

      pool --[genre filter]--> eligible --[BPM/key present]--> sequenceable
           --[compatibility predicates]--> reachable path

    Comparing the wrong pair mislabels the cause: a library where no genre
    filter was applied at all must never be told the genre filter was to blame.
    """
    pool = eligible_before_filter
    if pool and eligible < pool * 0.5:
        # A genre filter actually removed most of the pool.
        return LimitingFactor.GENRE_FILTER
    if eligible and len(graph) < eligible * 0.5:
        # Plenty eligible, but most of them have no BPM/key.
        return LimitingFactor.COVERAGE
    if len(graph) < 50:
        return LimitingFactor.COVERAGE
    if opts.mode is SequenceMode.KEY:
        return LimitingFactor.KEY_CONSTRAINTS
    if opts.mode is SequenceMode.BPM:
        return LimitingFactor.BPM_TOLERANCE
    # BPM+KEY: whichever predicate is tighter.
    loosened = SequenceOptions(**{**opts.__dict__, "mode": SequenceMode.BPM})
    bpm_only = TrackGraph(graph.tracks, graph.features, loosened)
    edges_bpm = sum(len(bpm_only.neighbours(i)) for i in range(min(50, len(bpm_only))))
    edges_both = sum(len(graph.neighbours(i)) for i in range(min(50, len(graph))))
    return (
        LimitingFactor.KEY_CONSTRAINTS
        if edges_bpm > edges_both * 2
        else LimitingFactor.BPM_TOLERANCE
    )
