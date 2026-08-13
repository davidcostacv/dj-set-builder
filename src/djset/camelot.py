"""Musical key -> Camelot wheel conversion.

Deliberately a lookup table rather than arithmetic over the circle of fifths:
enharmonic spellings and the various notations sources use ("Ebm", "D#m",
"F# minor", "6A", "9m") are easier to get right — and to test — as data.
"""

from __future__ import annotations

import re

# Canonical pitch class per note name, including enharmonics.
_PITCH: dict[str, int] = {
    "C": 0, "B#": 0,
    "C#": 1, "DB": 1,
    "D": 2,
    "D#": 3, "EB": 3,
    "E": 4, "FB": 4,
    "F": 5, "E#": 5,
    "F#": 6, "GB": 6,
    "G": 7,
    "G#": 8, "AB": 8,
    "A": 9,
    "A#": 10, "BB": 10,
    "B": 11, "CB": 11,
}

# Camelot number by pitch class, per mode.
_MINOR_BY_PITCH = {
    8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7, 9: 8, 4: 9, 11: 10, 6: 11, 1: 12,
}
_MAJOR_BY_PITCH = {
    11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7, 0: 8, 7: 9, 2: 10, 9: 11, 4: 12,
}

_CAMELOT_RE = re.compile(r"^\s*(\d{1,2})\s*([ABab])\s*$")
_OPENKEY_RE = re.compile(r"^\s*(\d{1,2})\s*([MDmd])\s*$")

_MINOR_WORDS = ("minor", "min", "moll")
_MAJOR_WORDS = ("major", "maj", "dur")


def to_camelot(key: str | None) -> str | None:
    """Parse any of the common key notations into a Camelot code like ``8A``.

    Accepts ``"Em"``, ``"F#m"``, ``"Db"``, ``"C minor"``, ``"A♭ maj"``,
    ``"6A"`` (passthrough), and Open Key ``"9m"`` / ``"4d"``. Returns ``None``
    for anything unparseable rather than guessing.
    """
    if not key:
        return None
    s = key.strip()
    if not s:
        return None

    m = _CAMELOT_RE.match(s)
    if m:
        n = int(m.group(1))
        return f"{n}{m.group(2).upper()}" if 1 <= n <= 12 else None

    m = _OPENKEY_RE.match(s)
    if m:
        return open_key_to_camelot(s)

    s = s.replace("♯", "#").replace("♭", "b")
    low = s.lower()

    mode: str | None = None
    for w in _MINOR_WORDS:
        if low.endswith(w):
            mode, s = "minor", s[: -len(w)]
            break
    if mode is None:
        for w in _MAJOR_WORDS:
            if low.endswith(w):
                mode, s = "major", s[: -len(w)]
                break

    s = s.strip().rstrip("-").strip()

    if mode is None:
        # Bare trailing 'm' means minor ("Em"); anything else is major ("Db").
        if len(s) >= 2 and s.endswith("m"):
            mode, s = "minor", s[:-1]
        else:
            mode = "major"

    note = s.strip().upper()
    pitch = _PITCH.get(note)
    if pitch is None:
        return None

    number = (_MINOR_BY_PITCH if mode == "minor" else _MAJOR_BY_PITCH)[pitch]
    return f"{number}{'A' if mode == 'minor' else 'B'}"


def open_key_to_camelot(open_key: str | None) -> str | None:
    """Open Key (``1m``-``12m`` / ``1d``-``12d``) -> Camelot.

    Open Key 1d is C major, which is Camelot 8B; the wheels are the same
    circle rotated by seven steps.
    """
    if not open_key:
        return None
    m = _OPENKEY_RE.match(open_key)
    if not m:
        return None
    n = int(m.group(1))
    if not 1 <= n <= 12:
        return None
    letter = "A" if m.group(2).lower() == "m" else "B"
    return f"{((n - 1 + 7) % 12) + 1}{letter}"


def camelot_to_open_key(camelot: str | None) -> str | None:
    if not camelot:
        return None
    m = _CAMELOT_RE.match(camelot)
    if not m:
        return None
    n = int(m.group(1))
    if not 1 <= n <= 12:
        return None
    suffix = "m" if m.group(2).upper() == "A" else "d"
    return f"{((n - 1 - 7) % 12) + 1}{suffix}"


def parse_camelot(camelot: str) -> tuple[int, str]:
    """``'8A'`` -> ``(8, 'A')``. Raises ValueError on malformed input."""
    m = _CAMELOT_RE.match(camelot)
    if not m:
        raise ValueError(f"not a Camelot code: {camelot!r}")
    n = int(m.group(1))
    if not 1 <= n <= 12:
        raise ValueError(f"Camelot number out of range: {camelot!r}")
    return n, m.group(2).upper()
