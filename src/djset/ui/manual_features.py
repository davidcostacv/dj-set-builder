"""Type in BPM and key for a track no source could resolve.

Coverage has a ceiling that no amount of API work removes — GetSongBPM's
catalogue is thin on recent releases, AcousticBrainz stopped accepting
submissions in 2022, and Deezer carries no harmonic data at all. For the
tracks that fall through all of it, the person who owns the library usually
knows the answer, and typing it in is the only route to a complete set.

A manual entry is priority 0: the highest trust there is, never overwritten by
an automated source.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..camelot import to_camelot
from ..models import AudioFeatures, Track

# Wide enough for half-time drum and bass and slow ballads alike; narrow enough
# to catch a typed year or a mistyped Camelot code.
MIN_BPM = 40.0
MAX_BPM = 250.0

KEY_HINT = "8A · Am · F#m · Db · C minor · 9m"


@dataclass(frozen=True)
class ParsedInput:
    """The result of reading both fields, valid or not."""

    bpm: float | None = None
    key_camelot: str | None = None
    bpm_error: str | None = None
    key_error: str | None = None

    @property
    def ok(self) -> bool:
        """Nothing malformed, and at least one field actually says something.

        Both blank is not an error worth shouting about — it is simply not yet
        a change, so the dialog stays open rather than writing an empty row.
        """
        return (
            self.bpm_error is None
            and self.key_error is None
            and (self.bpm is not None or self.key_camelot is not None)
        )


def parse_manual_input(bpm_text: str, key_text: str) -> ParsedInput:
    """Read both fields, reporting each problem separately.

    Blank means "leave this one alone", not zero — the caller merges omitted
    fields with whatever is already on file, so a user who knows the key but
    not the tempo can supply just the key.
    """
    bpm: float | None = None
    bpm_error: str | None = None
    raw = bpm_text.strip()
    if raw:
        # A Spanish-locale keyboard produces "128,5" as readily as "128.5".
        try:
            bpm = float(raw.replace(",", "."))
        except ValueError:
            bpm = None
            bpm_error = "Not a number."
        else:
            if not (MIN_BPM <= bpm <= MAX_BPM):
                bpm_error = f"Outside {MIN_BPM:.0f}–{MAX_BPM:.0f} BPM."
                bpm = None

    key_camelot: str | None = None
    key_error: str | None = None
    raw_key = key_text.strip()
    if raw_key:
        key_camelot = to_camelot(raw_key)
        if key_camelot is None:
            key_error = f"Not a key. Try: {KEY_HINT}"

    return ParsedInput(bpm, key_camelot, bpm_error, key_error)


class ManualFeaturesDialog(QDialog):
    """Ask for a BPM and a key, validating as they are typed."""

    def __init__(
        self,
        track: Track,
        existing: AudioFeatures | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set BPM and key by hand")
        self.resize(460, 0)

        lay = QVBoxLayout(self)

        heading = QLabel(f"<b>{track.title}</b><br>{track.artist}")
        heading.setTextFormat(Qt.RichText)
        heading.setWordWrap(True)
        lay.addWidget(heading)

        form = QFormLayout()
        self.bpm_edit = QLineEdit()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText(KEY_HINT)

        # Prefill from whatever is already known, so a partial row can be
        # completed without retyping the half that was already right.
        if existing is not None:
            if existing.bpm is not None:
                self.bpm_edit.setText(f"{existing.bpm:g}")
            if existing.key_camelot:
                self.key_edit.setText(existing.key_camelot)

        form.addRow("BPM", self.bpm_edit)
        form.addRow("Key", self.key_edit)
        lay.addLayout(form)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        self.box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.box.accepted.connect(self.accept)
        self.box.rejected.connect(self.reject)
        lay.addWidget(self.box)

        self.bpm_edit.textChanged.connect(self._revalidate)
        self.key_edit.textChanged.connect(self._revalidate)
        self._revalidate()

    # ------------------------------------------------------------------
    def parsed(self) -> ParsedInput:
        return parse_manual_input(self.bpm_edit.text(), self.key_edit.text())

    def values(self) -> tuple[float | None, str | None]:
        p = self.parsed()
        return p.bpm, p.key_camelot

    def _revalidate(self) -> None:
        p = self.parsed()
        problems = [e for e in (p.bpm_error, p.key_error) if e]
        if problems:
            self.hint.setText(
                "<span style='color:#c0392b'>" + " ".join(problems) + "</span>"
            )
        elif p.key_camelot:
            # Echo the resolved code so a typed "Am" visibly becomes 8A.
            self.hint.setText(f"Key reads as <b>{p.key_camelot}</b>.")
        else:
            self.hint.setText("Leave a field blank to keep what is already known.")

        self.box.button(QDialogButtonBox.Ok).setEnabled(p.ok)
