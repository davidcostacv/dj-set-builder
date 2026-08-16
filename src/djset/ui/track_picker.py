"""Track picker: choose a subset of the selected sources by hand.

Two ways to feed the sequencer are useful in practice, and they are different
jobs. Either take a whole playlist and put it into mixable order, or hand-pick
a handful of tracks and let the app work out the order. This dialog is the
second one.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..models import AudioFeatures, Track


class TrackPicker(QDialog):
    def __init__(
        self,
        tracks: list[Track],
        features: dict[str, AudioFeatures],
        preselected: set[str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose tracks")
        self.resize(760, 560)

        self._tracks = tracks
        self._features = features
        preselected = preselected or set()

        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by title or artist…")
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search)

        self.only_usable = QCheckBox("Only tracks with BPM + key")
        self.only_usable.setChecked(True)
        self.only_usable.toggled.connect(self._apply_filter)
        top.addWidget(self.only_usable)
        lay.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Track", "Artist", "BPM", "Key"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        lay.addWidget(self.tree)

        self._items: list[QTreeWidgetItem] = []
        for t in tracks:
            f = features.get(t.spotify_id)
            item = QTreeWidgetItem(
                [
                    t.title,
                    t.artist,
                    f"{f.bpm:.0f}" if f and f.bpm else "—",
                    (f.key_camelot if f else None) or "—",
                ]
            )
            item.setData(0, Qt.UserRole, t.spotify_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                0, Qt.Checked if t.spotify_id in preselected else Qt.Unchecked
            )
            self.tree.addTopLevelItem(item)
            self._items.append(item)

        for i, w in enumerate((320, 240, 70, 60)):
            self.tree.setColumnWidth(i, w)

        buttons = QHBoxLayout()
        check_visible = QPushButton("Check all shown")
        check_visible.clicked.connect(lambda: self._set_visible(True))
        uncheck_all = QPushButton("Uncheck all")
        uncheck_all.clicked.connect(lambda: self._set_all(False))
        buttons.addWidget(check_visible)
        buttons.addWidget(uncheck_all)
        buttons.addStretch(1)
        self.count_label = QLabel()
        buttons.addWidget(self.count_label)
        lay.addLayout(buttons)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

        self.tree.itemChanged.connect(self._update_count)
        self._apply_filter()
        self._update_count()

    # ------------------------------------------------------------------
    def _is_usable(self, spotify_id: str) -> bool:
        f = self._features.get(spotify_id)
        return bool(f and f.bpm is not None and f.key_camelot is not None)

    def _apply_filter(self) -> None:
        needle = self.search.text().strip().lower()
        usable_only = self.only_usable.isChecked()
        for item in self._items:
            sid = item.data(0, Qt.UserRole)
            text = f"{item.text(0)} {item.text(1)}".lower()
            visible = (not needle or needle in text) and (
                not usable_only or self._is_usable(sid)
            )
            item.setHidden(not visible)
        self._update_count()

    def _set_visible(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for item in self._items:
            if not item.isHidden():
                item.setCheckState(0, state)
        self._update_count()

    def _set_all(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for item in self._items:
            item.setCheckState(0, state)
        self._update_count()

    def _update_count(self, *_: object) -> None:
        chosen = self.selected_ids()
        usable = sum(1 for sid in chosen if self._is_usable(sid))
        self.count_label.setText(f"{len(chosen)} chosen · {usable} with BPM+key")

    # ------------------------------------------------------------------
    def selected_ids(self) -> set[str]:
        return {
            item.data(0, Qt.UserRole)
            for item in self._items
            if item.checkState(0) == Qt.Checked
        }

    def selected_tracks(self) -> list[Track]:
        chosen = self.selected_ids()
        return [t for t in self._tracks if t.spotify_id in chosen]
