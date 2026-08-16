"""Pane 4's ordered set table: drag-reorderable, with live transition quality.

The table owns the edited order. Because the playlist already exists by the
time this renders, edits are batched and pushed back only when the user clicks
Update — never on every drag.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor

from ..models import AudioFeatures, Track
from ..sequencing import SequenceOptions, transition

COLUMNS = ["#", "Title", "Artist", "BPM", "Key", "Transition"]


class SetTableModel(QAbstractTableModel):
    """Rows are tracks; the Transition column describes the move *into* the
    next row, recomputed live as rows move."""

    orderChanged = Signal()

    def __init__(
        self,
        tracks: list[Track],
        features: dict[str, AudioFeatures],
        opts: SequenceOptions,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._tracks = list(tracks)
        self._features = features
        self._opts = opts

    # -- data ---------------------------------------------------------------
    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._tracks)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def _transition_into_next(self, row: int):
        if row + 1 >= len(self._tracks):
            return None
        a = self._features.get(self._tracks[row].spotify_id)
        b = self._features.get(self._tracks[row + 1].spotify_id)
        if a is None or b is None:
            return None
        return transition(a, b, self._opts)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        track = self._tracks[row]
        feat = self._features.get(track.spotify_id)

        if role == Qt.DisplayRole:
            if col == 0:
                return row + 1
            if col == 1:
                return track.title
            if col == 2:
                return track.artist
            if col == 3:
                return f"{feat.bpm:.0f}" if feat and feat.bpm else "—"
            if col == 4:
                return (feat.key_camelot if feat else None) or "—"
            if col == 5:
                tr = self._transition_into_next(row)
                if tr is None:
                    return "—" if row + 1 >= len(self._tracks) else "incompatible"
                return tr.label

        if role == Qt.TextAlignmentRole and col in (0, 3, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)

        if role == Qt.ForegroundRole and col == 5:
            tr = self._transition_into_next(row)
            if tr is None:
                return QColor("#94A3B8") if row + 1 >= len(self._tracks) else QColor("#E24B4A")
            if tr.quality >= 0.8:
                return QColor("#1D9E75")
            if tr.quality >= 0.6:
                return QColor("#639922")
            if tr.quality >= 0.4:
                return QColor("#BA7517")
            return QColor("#E24B4A")

        return None

    # -- drag reordering ----------------------------------------------------
    def flags(self, index: QModelIndex):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.isValid():
            return base | Qt.ItemIsDragEnabled
        return base | Qt.ItemIsDropEnabled

    def supportedDropActions(self):  # noqa: N802
        return Qt.MoveAction

    def moveRow(self, src: int, dst: int) -> bool:  # noqa: N802
        if src == dst or not (0 <= src < len(self._tracks)):
            return False
        dst = max(0, min(dst, len(self._tracks)))
        self.beginResetModel()
        track = self._tracks.pop(src)
        if dst > src:
            dst -= 1
        self._tracks.insert(dst, track)
        self.endResetModel()
        self.orderChanged.emit()
        return True

    def removeTrack(self, row: int) -> bool:  # noqa: N802
        if not (0 <= row < len(self._tracks)):
            return False
        self.beginResetModel()
        self._tracks.pop(row)
        self.endResetModel()
        self.orderChanged.emit()
        return True

    # -- summary ------------------------------------------------------------
    def total_duration_ms(self) -> int:
        return sum(t.duration_ms or 0 for t in self._tracks)

    def average_quality(self) -> float:
        qualities = [
            tr.quality
            for tr in (self._transition_into_next(i) for i in range(len(self._tracks) - 1))
            if tr is not None
        ]
        return sum(qualities) / len(qualities) if qualities else 0.0

    def summary_line(self) -> str:
        ms = self.total_duration_ms()
        mins = ms // 60_000
        return (
            f"{len(self._tracks)} tracks · {mins // 60}h {mins % 60:02d}m · "
            f"average transition quality {self.average_quality():.0%}"
        )
