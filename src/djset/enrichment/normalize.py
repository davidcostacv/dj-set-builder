"""Artist/title normalization for matching against sources that have no
Spotify or ISRC index.

This module is the single biggest determinant of enrichment coverage. It is
deliberately isolated and heavily tested so it can be tuned against the
unresolved list from the coverage report without touching anything else.
"""

from __future__ import annotations

import re
import unicodedata

# " - 2011 Remaster", " - Radio Edit", " - Live" … Spotify puts a lot of
# metadata after a dash. Only strip it when it looks like production metadata.
_SUFFIX_NOISE = (
    "remaster", "remastered", "radio edit", "radio mix", "single version",
    "album version", "original mix", "original version", "extended mix",
    "extended version", "club mix", "club edit", "edit", "version", "mix",
    "live", "acoustic", "instrumental", "mono", "stereo", "bonus track",
    "deluxe", "reissue", "remix", "rerecorded", "re recorded", "demo",
    "explicit", "clean", "anniversary edition",
)

# A bracketed credit: "(feat. X)", "[with Y]". Safe to strip on sight.
_FEAT_BRACKETED_RE = re.compile(
    r"\s*[\(\[\{]\s*(feat|ft|featuring|with|w/|con|avec)\b\.?[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)
# An unbracketed credit runs to the end of the string: "Levitating feat. DaBaby".
# "with" is deliberately NOT in this list — "Stay With Me" is a title, not a credit.
_FEAT_BARE_RE = re.compile(r"\s+\b(feat|ft|featuring)\b\.?\s+.*$", re.IGNORECASE)

_BRACKETED_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an|el|la|los|las|le|les|der|die|das)\s+", re.IGNORECASE)
# Artists get a much shorter list: stripping "a" would maul "A$AP Rocky".
_ARTIST_ARTICLE_RE = re.compile(r"^(the|los|las|les|die)\s+", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# NOTE: there is deliberately no artist-splitting regex here. Callers pass a
# single artist name taken from Spotify's artists array (Track.primary_artist),
# so splitting on "," / "&" would corrupt names that legitimately contain them
# — "Tyler, The Creator" became "Tyler", and "Above & Beyond" became "Above".


def _fold(s: str) -> str:
    """Lowercase, strip accents, unify dashes, elide apostrophes.

    Apostrophes are deleted rather than replaced with a space so "Don't"
    becomes "dont", not "don t" — the latter would never match an index.
    """
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("‐", "-")
    s = s.replace("'", "")
    # Stylised letters: "Joey Bada$$" -> "joey badass", "$uicideboy$" ->
    # "suicideboys", "A$AP" -> "asap". Runs are substituted whole, and only
    # when the run touches a letter, so a currency amount ("$100") is still
    # treated as punctuation. Trailing-side first, then leading-side.
    _s_run = lambda m: "s" * len(m.group())  # noqa: E731
    s = re.sub(r"(?<=[A-Za-z])\$+", _s_run, s)
    s = re.sub(r"\$+(?=[A-Za-z])", _s_run, s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def _strip_credits(s: str) -> str:
    return _FEAT_BARE_RE.sub("", _FEAT_BRACKETED_RE.sub(" ", s))


def _strip_dash_suffix(s: str) -> str:
    """Drop a trailing ' - <production metadata>' clause, if that's what it is."""
    parts = s.split(" - ")
    if len(parts) < 2:
        return s
    tail = parts[-1].strip().lower()
    if any(word in tail for word in _SUFFIX_NOISE):
        return " - ".join(parts[:-1])
    return s


def _clean(s: str, *, strip_articles: bool) -> str:
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if strip_articles:
        s = _LEADING_ARTICLE_RE.sub("", s).strip()
    return s


def normalize_title(title: str) -> str:
    """Canonical form of a track title for fuzzy joining."""
    s = _strip_credits(_fold(title))
    s = _strip_dash_suffix(s)
    s = _BRACKETED_RE.sub(" ", s)
    return _clean(s, strip_articles=True)


def title_variants(title: str) -> list[str]:
    """Ordered lookup candidates, most specific first.

    Sources like GetSongBPM index the plain song title, but a remix or extended
    mix is a genuinely different recording with a different tempo — so try the
    fuller form before falling back to the bare title.
    """
    seen: list[str] = []

    def add(v: str) -> None:
        v = v.strip()
        if v and v not in seen:
            seen.append(v)

    no_feat = _strip_credits(_fold(title))

    add(_clean(no_feat, strip_articles=False))            # keeps "(extended mix)" content
    add(_clean(_strip_dash_suffix(no_feat), strip_articles=False))
    add(normalize_title(title))                            # bare title
    return seen


def primary_artist(artist: str) -> str:
    """Canonical form of a single artist name, articles kept.

    Expects one artist, not a joined credit list — see ``Track.primary_artist``.
    """
    s = _strip_credits(_fold(artist))
    s = _BRACKETED_RE.sub(" ", s)
    return _clean(s, strip_articles=False)


def normalize_artist(artist: str) -> str:
    """Canonical artist form, article-stripped, for equality comparison."""
    return _ARTIST_ARTICLE_RE.sub("", primary_artist(artist)).strip()


def artist_variants(artist: str) -> list[str]:
    seen: list[str] = []
    for v in (primary_artist(artist), normalize_artist(artist)):
        if v and v not in seen:
            seen.append(v)
    return seen


def match_key(artist: str, title: str) -> str:
    """Stable join key: ``normalized artist|normalized title``."""
    return f"{normalize_artist(artist)}|{normalize_title(title)}"


def similarity(a: str, b: str) -> float:
    """Token-overlap ratio in [0, 1]. Cheap, order-insensitive, good enough
    to reject a wrong hit from a search endpoint."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
