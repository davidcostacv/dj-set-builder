"""Track picker: choose a subset of the selected sources by hand.

Two ways to feed the sequencer are useful in practice, and they are different
jobs. Either take a whole playlist and put it into mixable order, or hand-pick
a handful of tracks and let the app work out the order. This dialog is the
second one.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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

# Given a track id, a BPM and a Camelot code, persist them and hand back the
# merged row. Passed in rather than reached for, so this dialog keeps knowing
# nothing about the database.
ManualEdit = Callable[[str, float | None, str | None], AudioFeatures]

SHOW_ALL = "All tracks"
SHOW_USABLE = "With BPM + key"
SHOW_MISSING = "Missing BPM or key"
SHOW_MODES = [SHOW_ALL, SHOW_USABLE, SHOW_MISSING]


class TrackPicker(QDialog):
    def __init__(
        self,
        tracks: list[Track],
        features: dict[str, AudioFeatures],
        preselected: set[str] | None = None,
        parent=None,
        on_manual_edit: ManualEdit | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose tracks")
        self.resize(760, 560)

        self._tracks = tracks
        self._features = features
        self._on_manual_edit = on_manual_edit
        self._by_id = {t.spotify_id: t for t in tracks}
        preselected = preselected or set()

        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by title or artist…")
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search)

        # Three states, not a checkbox. "Missing BPM or key" is what makes
        # hand-entry reachable at all: with a plain "only usable" toggle the
        # tracks worth fixing are exactly the ones hidden from view, so the
        # feature would only be findable by someone who already knew it existed.
        self.filter_mode = QComboBox()
        self.filter_mode.addItems(SHOW_MODES)
        self.filter_mode.setCurrentIndex(SHOW_MODES.index(SHOW_USABLE))
        self.filter_mode.currentIndexChanged.connect(self._apply_filter)
        top.addWidget(self.filter_mode)
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

        # No source resolves everything, so the last resort is knowing it
        # yourself. Only offered when the caller supplied somewhere to save it.
        self.edit_btn = QPushButton("Set BPM / key…")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self._edit_current)
        if on_manual_edit is not None:
            buttons.addWidget(self.edit_btn)
            self.tree.itemDoubleClicked.connect(lambda *_: self._edit_current())
            self.tree.itemSelectionChanged.connect(self._update_edit_button)

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
    # manual entry
    # ------------------------------------------------------------------
    def _current_item(self) -> QTreeWidgetItem | None:
        chosen = [i for i in self.tree.selectedItems() if not i.isHidden()]
        return chosen[0] if len(chosen) == 1 else None

    def _update_edit_button(self) -> None:
        self.edit_btn.setEnabled(self._current_item() is not None)

    def _edit_current(self) -> None:
        item = self._current_item()
        if item is None or self._on_manual_edit is None:
            return
        sid = item.data(0, Qt.UserRole)
        track = self._by_id.get(sid)
        if track is None:
            return

        from .manual_features import ManualFeaturesDialog

        dialog = ManualFeaturesDialog(track, self._features.get(sid), self)
        if dialog.exec() != QDialog.Accepted:
            return

        bpm, key = dialog.values()
        self._features[sid] = self._on_manual_edit(sid, bpm, key)
        self._refresh_row(item, sid)

        # The row may now belong to a different filter bucket than the one
        # being viewed, so re-run the filter rather than leaving a stale row.
        self._apply_filter()
        if not item.isHidden():
            self.tree.scrollToItem(item)

    def _refresh_row(self, item: QTreeWidgetItem, spotify_id: str) -> None:
        f = self._features.get(spotify_id)
        item.setText(2, f"{f.bpm:.0f}" if f and f.bpm else "—")
        item.setText(3, (f.key_camelot if f else None) or "—")

    # ------------------------------------------------------------------
    def _is_usable(self, spotify_id: str) -> bool:
        f = self._features.get(spotify_id)
        return bool(f and f.bpm is not None and f.key_camelot is not None)

    def _apply_filter(self) -> None:
        needle = self.search.text().strip().lower()
        mode = self.filter_mode.currentText()
        for item in self._items:
            sid = item.data(0, Qt.UserRole)
            text = f"{item.text(0)} {item.text(1)}".lower()
            usable = self._is_usable(sid)
            matches_mode = (
                mode == SHOW_ALL
                or (mode == SHOW_USABLE and usable)
                or (mode == SHOW_MISSING and not usable)
            )
            visible = (not needle or needle in text) and matches_mode
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
