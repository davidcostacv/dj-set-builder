"""Ask a second source about tracks we already have data for, and compare.

The resolver stops at the first hit, so a track answered by GetSongBPM is never
put to AcousticBrainz. That is right for enrichment — a second opinion costs a
second request and buys nothing most of the time — but it does mean nothing in
the app ever notices that two sources disagree.

This is a diagnostic, not a runtime feature. It writes nothing. The point is to
find out whether disagreement is common enough to be worth acting on, before
building any policy on the assumption that it is.

What is worth checking, and what is not:

* **Half- and double-time is mostly a non-issue.** ``sequencing.bpm_distance``
  already tries ``b*2`` and ``b/2``, so a source reporting 64 where another
  says 128 still mixes correctly. Counted separately for that reason — it is
  a disagreement, but not one that damages a set.
* **A conflict that is neither is a real defect.** 128 against 143 is simply
  one source being wrong, and it will produce a transition that does not beat
  match.
* **Key disagreement has nothing to absorb it.** A wrong Camelot code ruins
  the harmonic mix outright, so exact and merely-compatible agreement are
  counted apart.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from .models import AudioFeatures, Track
from .sequencing import DEFAULT_TOLERANCE, bpm_ratio, key_distance

log = logging.getLogger(__name__)

AGREE = "agree"
COMPATIBLE = "close enough"
HALF_DOUBLE = "half/double"
CONFLICT = "conflict"

BPM_CLASSES = (AGREE, COMPATIBLE, HALF_DOUBLE, CONFLICT)
KEY_CLASSES = (AGREE, COMPATIBLE, CONFLICT)

# "Is it the same number." Distinct from the question that actually matters.
SAME_VALUE_TOLERANCE = 0.02


def classify_bpm(
    a: float | None,
    b: float | None,
    *,
    same_tolerance: float = SAME_VALUE_TOLERANCE,
    mix_tolerance: float = DEFAULT_TOLERANCE,
) -> str | None:
    """Classify a tempo disagreement by whether it would damage a set.

    Two thresholds, because "the sources printed different numbers" and "the
    sequencer will mix these wrongly" are different questions and only the
    second one costs anything.

    Measured on this library, four of eight apparent conflicts sat between 2.2%
    and 3.9% — outside a strict same-value tolerance but inside the sequencer's
    own, so nothing about them was broken. GetSongBPM also reports whole
    numbers while Deezer reports decimals, which manufactures a small
    disagreement on every single track.

    That window narrowed when the sequencer's default tightened from 6% to 3%,
    and this report will now call some of those a conflict. That is the honest
    answer rather than a regression: the question asked here is whether the
    sequencer would mix them wrongly, and at 3% it would.
    """
    if a is None or b is None:
        return None
    if bpm_ratio(a, b) <= same_tolerance:
        return AGREE
    if bpm_ratio(a, b) <= mix_tolerance:
        return COMPATIBLE
    if any(bpm_ratio(a, alt) <= mix_tolerance for alt in (b * 2.0, b / 2.0)):
        return HALF_DOUBLE
    return CONFLICT


@dataclass(frozen=True)
class Comparison:
    track: Track
    baseline: AudioFeatures  # what is on file
    challenger: AudioFeatures  # what the second source says
    # The tolerance this comparison was judged at, carried rather than looked
    # up, so a Comparison means the same thing wherever it is read.
    mix_tolerance: float = DEFAULT_TOLERANCE

    @property
    def bpm_relation(self) -> str | None:
        return classify_bpm(
            self.baseline.bpm, self.challenger.bpm, mix_tolerance=self.mix_tolerance
        )

    @property
    def key_relation(self) -> str | None:
        """Exact match, compatible neighbour, or conflict."""
        a, b = self.baseline.key_camelot, self.challenger.key_camelot
        if not a or not b:
            return None
        if a == b:
            return AGREE
        # An adjacent code still mixes, so it is not the same class of error as
        # a code from the other side of the wheel.
        return COMPATIBLE if key_distance(a, b) is not None else CONFLICT

    @property
    def ratio(self) -> float | None:
        """Challenger over baseline. Printed because the pattern is the point:
        a clean 1.50 or 3.00 is a tempo-reading ambiguity, not a bad match."""
        a, b = self.baseline.bpm, self.challenger.bpm
        if not a or not b:
            return None
        return b / a

    def describe(self) -> str:
        def fmt(f: AudioFeatures) -> str:
            bpm = f"{f.bpm:.1f}" if f.bpm is not None else "—"
            return f"{bpm} {f.key_camelot or '—'} ({f.source})"

        ratio = f"  ×{self.ratio:.2f}" if self.ratio else ""
        return (
            f"{self.track.primary_artist} — {self.track.title}{ratio}\n"
            f"      on file: {fmt(self.baseline)}\n"
            f"      second : {fmt(self.challenger)}"
        )


@dataclass
class CrossCheck:
    mix_tolerance: float = DEFAULT_TOLERANCE
    asked: int = 0
    answered: int = 0  # the second source had something to say
    bpm: Counter[str] = field(default_factory=Counter)
    key: Counter[str] = field(default_factory=Counter)
    conflicts: list[Comparison] = field(default_factory=list)
    by_source: Counter[str] = field(default_factory=Counter)

    def add(self, c: Comparison) -> None:
        self.answered += 1
        self.by_source[c.baseline.source] += 1
        if (rel := c.bpm_relation) is not None:
            self.bpm[rel] += 1
        if (rel := c.key_relation) is not None:
            self.key[rel] += 1
        if CONFLICT in (c.bpm_relation, c.key_relation):
            self.conflicts.append(c)

    @property
    def bpm_compared(self) -> int:
        return sum(self.bpm.values())

    @property
    def key_compared(self) -> int:
        return sum(self.key.values())

    def report(self, challenger_name: str, show: int = 8) -> str:
        L: list[str] = []
        add = L.append
        add(f"Second opinion from {challenger_name!r}")
        add("=" * 66)
        add(f"asked about   : {self.asked} tracks that already have data")
        add(
            f"judged at     : {self.mix_tolerance:.0%} BPM tolerance — the same "
            "setting you build sets with"
        )
        add(f"it could answer: {self.answered}")
        if self.by_source:
            add("  their data came from: " + ", ".join(
                f"{s}={n}" for s, n in self.by_source.most_common()
            ))
        add("")

        def block(
            label: str, counts: Counter[str], total: int, classes, note: str
        ) -> None:
            add(f"{label} ({total} comparable)")
            add("-" * 66)
            if not total:
                add("  nothing to compare")
                add("")
                return
            for name in classes:
                n = counts.get(name, 0)
                add(f"  {name:<13} {n:>5}  {n / total:6.1%}")
            harmless = sum(counts.get(c, 0) for c in classes if c != CONFLICT)
            add(f"  {'-> harmless':<13} {harmless:>5}  {harmless / total:6.1%}")
            add(f"  {note}")
            add("")

        block(
            "BPM", self.bpm, self.bpm_compared, BPM_CLASSES,
            "only 'conflict' can damage a set: 'close enough' is inside the "
            "sequencer's\n  tolerance, and half/double is absorbed by trying "
            "b*2 and b/2",
        )
        block(
            "Key", self.key, self.key_compared, KEY_CLASSES,
            "'close enough' is an adjacent Camelot code, which still mixes",
        )

        if self.conflicts:
            add(f"Conflicts ({len(self.conflicts)}, showing up to {show})")
            add("-" * 66)
            for c in self.conflicts[:show]:
                add("  " + c.describe())
            add("")
        return "\n".join(L)


def candidates_for(
    tracks: list[Track],
    features: dict[str, AudioFeatures],
    *,
    skip_source: str | None = None,
) -> list[tuple[Track, AudioFeatures]]:
    """The tracks actually worth a second opinion, with the data to compare.

    Split out so a caller can report the real number before any requests go
    out, instead of announcing a count that the filtering then contradicts.
    """
    out: list[tuple[Track, AudioFeatures]] = []
    for track in tracks:
        baseline = features.get(track.spotify_id)
        if baseline is None:
            continue  # nothing on file to compare against
        if skip_source is not None and baseline.source == skip_source:
            continue  # comparing a source with itself measures nothing
        out.append((track, baseline))
    return out


def cross_check(
    tracks: list[Track],
    features: dict[str, AudioFeatures],
    challenger,
    *,
    skip_source: str | None = None,
    mix_tolerance: float = DEFAULT_TOLERANCE,
    progress=None,
) -> CrossCheck:
    """Put tracks that already have data to a second source.

    ``skip_source`` drops tracks whose data came from the challenger itself,
    since comparing a source with itself measures nothing.

    ``mix_tolerance`` is the sequencer's BPM tolerance, and it decides what
    counts as a conflict. It is a parameter rather than a constant because the
    only conflicts worth knowing about are the ones that would damage a set at
    the tolerance *you* generate at — someone mixing at 12% has fewer real
    problems than someone mixing at 2%, from identical data.
    """
    # Decide who is being asked *before* the loop. Filtering inside it meant
    # `continue` skipped the progress call, so the counter jumped 1,2,3,5,6 and
    # counted against a total that included tracks never asked about — it read
    # like requests were failing when nothing was wrong.
    candidates = candidates_for(tracks, features, skip_source=skip_source)

    result = CrossCheck(mix_tolerance=mix_tolerance)
    result.asked = len(candidates)
    for i, (track, baseline) in enumerate(candidates, 1):
        try:
            found = challenger.lookup(track)
        except Exception as exc:  # a diagnostic must not die on one bad row
            log.warning("crosscheck: %s raised on %s: %s", challenger.name, track.title, exc)
            found = None
        if found is not None:
            result.add(Comparison(track, baseline, found, mix_tolerance))
        if progress is not None:
            progress(i, len(candidates), f"{track.primary_artist} — {track.title}")
    return result
