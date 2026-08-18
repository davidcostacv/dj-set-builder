"""The single window: sources -> genre filter -> generate -> result.

Pane 2 is collapsed by default and never gates Generate. Generate is enabled
the moment one source is selected; genre selection is never part of that
condition.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import db
from ..config import ConfigError, load_config
from ..enrichment import ATTRIBUTION_URL, Resolver, default_sources, enrich_tracks
from ..export import ExportError, export_to_spotify, split_by_genre, update_playlist_order
from ..filtering import (
    UNKNOWN,
    filter_tracks,
    genre_availability,
    genre_index,
    summarize,
)
from ..models import LIKED_SONGS_ID, LIKED_SONGS_NAME
from ..sequencing import SequenceMode, SequenceOptions, build_set
from ..spotify.auth import SpotifyAuth
from ..spotify.client import SpotifyClient
from ..spotify.sync import sync_artists, sync_playlists
from .result_table import SetTableModel
from .workers import TaskRunner

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("djset — Spotify DJ set builder")
        self.resize(1180, 760)

        self.runner = TaskRunner()
        self.export_runner = TaskRunner()
        self._result_model: SetTableModel | None = None
        self._playlist_id: str | None = None
        self._dirty = False

        self._build_toolbar()
        self._build_body()
        self._build_status()

        self.reload_from_db()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)

        sync = QAction("Sync library", self)
        sync.triggered.connect(self.on_sync)
        bar.addAction(sync)

        enrich = QAction("Enrich BPM/key", self)
        enrich.triggered.connect(self.on_enrich)
        bar.addAction(enrich)

        bar.addSeparator()
        split = QAction("Split by genre", self)
        split.setToolTip("Library organisation, not a DJ set: one playlist per genre")
        split.triggered.connect(self.on_split_by_genre)
        bar.addAction(split)

        bar.addSeparator()
        about = QAction("About", self)
        about.triggered.connect(self.on_about)
        bar.addAction(about)

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

        splitter.addWidget(self._pane_sources())
        splitter.addWidget(self._pane_genres())
        splitter.addWidget(self._pane_generate())
        splitter.addWidget(self._pane_result())
        # Pane 4 carries a six-column table; give it the most room.
        splitter.setSizes([250, 210, 250, 560])

        self.setCentralWidget(splitter)

    def _pane_sources(self) -> QWidget:
        box = QGroupBox("1 · Sources")
        lay = QVBoxLayout(box)

        self.sources_list = QWidget()
        self.sources_layout = QVBoxLayout(self.sources_list)
        self.sources_layout.setAlignment(Qt.AlignTop)

        from PySide6.QtWidgets import QScrollArea

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.sources_list)
        lay.addWidget(scroll)

        self.sources_summary = QLabel("—")
        self.sources_summary.setWordWrap(True)
        lay.addWidget(self.sources_summary)

        picker_row = QHBoxLayout()
        self.pick_btn = QPushButton("Choose tracks…")
        self.pick_btn.setToolTip("Hand-pick a subset instead of using every track")
        self.pick_btn.clicked.connect(self.on_pick_tracks)
        self.clear_pick_btn = QPushButton("Use all")
        self.clear_pick_btn.setToolTip("Clear the hand-picked subset")
        self.clear_pick_btn.clicked.connect(self.on_clear_picked)
        self.clear_pick_btn.setEnabled(False)
        picker_row.addWidget(self.pick_btn)
        picker_row.addWidget(self.clear_pick_btn)
        lay.addLayout(picker_row)
        return box

    def _pane_genres(self) -> QWidget:
        box = QGroupBox("2 · Genre filter (optional)")
        lay = QVBoxLayout(box)

        # Collapsed by default. It expands only when clicked, and never blocks
        # Generate — skipping this pane is a normal path, not a mistake.
        self.genre_toggle = QPushButton("Show genres ▸")
        self.genre_toggle.setCheckable(True)
        self.genre_toggle.toggled.connect(self._on_genre_toggled)
        lay.addWidget(self.genre_toggle)

        self.genre_body = QWidget()
        body = QVBoxLayout(self.genre_body)

        buttons = QHBoxLayout()
        self.genre_select_all = QPushButton("Select all")
        self.genre_select_all.clicked.connect(lambda: self._set_all_genres(True))
        self.genre_clear = QPushButton("Clear")
        self.genre_clear.clicked.connect(lambda: self._set_all_genres(False))
        buttons.addWidget(self.genre_select_all)
        buttons.addWidget(self.genre_clear)
        body.addLayout(buttons)

        # Shown in place of the checkbox list when there is nothing to filter
        # on. Blankness alone reads as a bug; the pane has to say which of the
        # three causes it is in.
        self.genre_notice = QLabel()
        self.genre_notice.setWordWrap(True)
        self.genre_notice.setVisible(False)
        body.addWidget(self.genre_notice)

        from PySide6.QtWidgets import QScrollArea

        self.genre_list = QWidget()
        self.genre_layout = QVBoxLayout(self.genre_list)
        self.genre_layout.setAlignment(Qt.AlignTop)
        self.genre_scroll = QScrollArea()
        self.genre_scroll.setWidgetResizable(True)
        self.genre_scroll.setWidget(self.genre_list)
        body.addWidget(self.genre_scroll)

        self.genre_body.setVisible(False)
        lay.addWidget(self.genre_body)

        self.eligible_label = QLabel("—")
        self.eligible_label.setWordWrap(True)
        lay.addWidget(self.eligible_label)
        return box

    def _pane_generate(self) -> QWidget:
        box = QGroupBox("3 · Generate")
        lay = QVBoxLayout(box)
        lay.setAlignment(Qt.AlignTop)

        lay.addWidget(QLabel("Sequence by"))
        self.mode_bpm = QRadioButton("BPM")
        self.mode_key = QRadioButton("Key")
        self.mode_both = QRadioButton("BPM + Key")
        self.mode_both.setChecked(True)
        for r in (self.mode_bpm, self.mode_key, self.mode_both):
            lay.addWidget(r)

        lay.addSpacing(8)
        lay.addWidget(QLabel("BPM tolerance"))
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setRange(0.02, 0.12)
        self.tolerance.setSingleStep(0.01)
        self.tolerance.setValue(0.06)
        self.tolerance.setDecimals(2)
        lay.addWidget(self.tolerance)

        self.half_double = QCheckBox("Match half/double time (70 ↔ 140)")
        self.half_double.setChecked(True)
        lay.addWidget(self.half_double)

        self.energy_boost = QCheckBox("Allow energy-boost transitions (+7)")
        lay.addWidget(self.energy_boost)

        lay.addSpacing(8)
        row = QHBoxLayout()
        self.target_kind = QComboBox()
        # "whole selection" reorders everything eligible rather than picking a
        # fixed count — for taking a playlist and putting it in mixable order.
        self.target_kind.addItems(["tracks", "minutes", "whole selection"])
        self.target_kind.currentTextChanged.connect(self._on_target_kind)
        self.target_value = QSpinBox()
        self.target_value.setRange(2, 500)
        self.target_value.setValue(24)
        row.addWidget(QLabel("Target"))
        row.addWidget(self.target_value)
        row.addWidget(self.target_kind)
        lay.addLayout(row)

        lay.addSpacing(8)
        lay.addWidget(QLabel("Playlist name"))
        self.playlist_name = QLineEdit()
        lay.addWidget(self.playlist_name)

        self.public_toggle = QCheckBox("Make public")  # private by default
        lay.addWidget(self.public_toggle)

        lay.addSpacing(12)
        self.generate_btn = QPushButton("Generate && Save to Spotify")
        self.generate_btn.setMinimumHeight(40)
        self.generate_btn.clicked.connect(self.on_generate)
        lay.addWidget(self.generate_btn)

        for w in (self.mode_bpm, self.mode_key, self.mode_both):
            w.toggled.connect(self._refresh_name_placeholder)
        self.target_value.valueChanged.connect(self._refresh_name_placeholder)
        return box

    def _pane_result(self) -> QWidget:
        box = QGroupBox("4 · Result")
        lay = QVBoxLayout(box)

        self.link_label = QLabel("No set generated yet.")
        self.link_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.link_label.setWordWrap(True)
        f = self.link_label.font()
        f.setPointSize(f.pointSize() + 2)
        self.link_label.setFont(f)
        lay.addWidget(self.link_label)

        row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy link")
        self.copy_btn.clicked.connect(self.on_copy_link)
        self.open_btn = QPushButton("Open in Spotify")
        self.open_btn.clicked.connect(self.on_open_link)
        self.update_btn = QPushButton("Update playlist")
        self.update_btn.clicked.connect(self.on_update_playlist)
        for b in (self.copy_btn, self.open_btn, self.update_btn):
            b.setEnabled(False)
            row.addWidget(b)
        lay.addLayout(row)

        self.result_table = QTableView()
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setDragDropMode(QAbstractItemView.InternalMove)
        self.result_table.setDragDropOverwriteMode(False)
        self.result_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.result_table.customContextMenuRequested.connect(self._result_menu)
        self.result_table.verticalHeader().setVisible(False)
        lay.addWidget(self.result_table)

        self.result_summary = QLabel("—")
        lay.addWidget(self.result_summary)
        return box

    def _build_status(self) -> None:
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setMaximumWidth(180)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.runner.cancel)

        self.statusBar().addPermanentWidget(self.cancel_btn)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("Ready")

    # ------------------------------------------------------------------
    # data plumbing
    # ------------------------------------------------------------------
    def _client(self) -> SpotifyClient:
        return SpotifyClient(SpotifyAuth(load_config()))

    def reload_from_db(self) -> None:
        with db.session() as conn:
            self._playlists = db.cached_playlists(conn)
            self._artist_genres = db.artist_genres(conn)
            self._aliases = db.genre_aliases(conn)
            self._features = db.all_features(conn)
        self._rebuild_sources()
        self._recompute()

    def _rebuild_sources(self) -> None:
        while self.sources_layout.count():
            item = self.sources_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.source_boxes: list[tuple[QCheckBox, str]] = []
        if not self._playlists:
            self.sources_layout.addWidget(
                QLabel("Nothing synced yet.\nUse “Sync library”.")
            )
            return

        for row in self._playlists:
            name = row["name"] or "(untitled)"
            count = row["track_count"] or 0
            cb = QCheckBox(f"{name}  ({count})")
            cb.toggled.connect(self._recompute)
            self.sources_layout.addWidget(cb)
            self.source_boxes.append((cb, row["spotify_id"]))
            if row["spotify_id"] == LIKED_SONGS_ID:
                cb.setChecked(True)

    def selected_sources(self) -> list[str]:
        return [pid for cb, pid in getattr(self, "source_boxes", []) if cb.isChecked()]

    def selected_genres(self) -> set[str] | None:
        """None means NO FILTER — deliberately distinct from 'none selected'."""
        chosen = {
            cb.text().split("  —")[0]
            for cb in getattr(self, "genre_boxes", [])
            if cb.isChecked()
        }
        return chosen or None

    def _all_source_tracks(self) -> list:
        sources = self.selected_sources()
        if not sources:
            return []
        with db.session() as conn:
            return db.tracks_in_playlists(conn, sources)

    def _pool(self) -> list:
        """Tracks feeding the sequencer: everything in the selected sources,
        or just the hand-picked subset when one is active."""
        tracks = self._all_source_tracks()
        picked = getattr(self, "_picked_ids", None)
        if picked:
            return [t for t in tracks if t.spotify_id in picked]
        return tracks

    def _recompute(self) -> None:
        pool = self._pool()
        picked = getattr(self, "_picked_ids", None)
        suffix = f" (hand-picked from {len(self._all_source_tracks())})" if picked else ""
        self.sources_summary.setText(
            f"{len(self.selected_sources())} source(s) · {len(pool)} tracks{suffix}"
        )
        self.clear_pick_btn.setEnabled(bool(picked))

        # Genre rows are built from the tags present in THIS selection.
        stats = genre_index(pool, self._artist_genres, self._aliases, self._features)
        self._genre_availability = genre_availability(pool, self._artist_genres)
        self._rebuild_genres(stats, self._genre_availability)

        summary = summarize(
            pool, self.selected_genres(), self._artist_genres, self._aliases, self._features
        )
        self.eligible_label.setText(summary.label)

        # Enabled by source selection alone — never by genre selection.
        self.generate_btn.setEnabled(bool(self.selected_sources()))
        self._refresh_name_placeholder()

    def _rebuild_genres(self, stats, availability=None) -> None:
        previously = {
            cb.text().split("  —")[0]
            for cb in getattr(self, "genre_boxes", [])
            if cb.isChecked()
        }
        while self.genre_layout.count():
            item = self.genre_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.genre_boxes: list[QCheckBox] = []

        # With nothing tagged, every track falls into Unknown and the list
        # would offer a single bucket covering the whole library — an option
        # that filters nothing while looking like a working filter. Say what
        # happened instead of rendering that.
        if availability is not None and not availability.usable:
            self.genre_notice.setText(
                f"<b>{availability.headline}</b><br>{availability.detail}"
            )
            self.genre_notice.setVisible(True)
            # Hide the list itself too. Leaving an empty bordered box below the
            # explanation is a third of the pane spent showing nothing.
            self.genre_scroll.setVisible(False)
            self.genre_select_all.setEnabled(False)
            self.genre_clear.setEnabled(False)
            self._refresh_genre_toggle()
            return

        self.genre_notice.setVisible(False)
        self.genre_scroll.setVisible(True)
        self.genre_select_all.setEnabled(True)
        self.genre_clear.setEnabled(True)

        for s in stats:
            cb = QCheckBox(f"{s.name}  — {s.track_count} tracks ({s.enriched_count} enriched)")
            if s.name in previously:
                cb.setChecked(True)
            cb.toggled.connect(self._on_genre_changed)
            self.genre_layout.addWidget(cb)
            self.genre_boxes.append(cb)

        self._refresh_genre_toggle()

    def _refresh_genre_toggle(self) -> None:
        """Say it on the collapsed button too.

        The pane is collapsed by default, so an explanation only visible after
        expanding is one most people would never see.
        """
        availability = getattr(self, "_genre_availability", None)
        shown = self.genre_toggle.isChecked()
        if availability is not None and availability.headline:
            self.genre_toggle.setText(
                "Genres unavailable ▾" if shown else "Genres unavailable ▸"
            )
        else:
            self.genre_toggle.setText("Hide genres ▾" if shown else "Show genres ▸")

    def _on_genre_toggled(self, shown: bool) -> None:
        self.genre_body.setVisible(shown)
        self._refresh_genre_toggle()

    def _on_genre_changed(self) -> None:
        pool = self._pool()
        summary = summarize(
            pool, self.selected_genres(), self._artist_genres, self._aliases, self._features
        )
        self.eligible_label.setText(summary.label)
        self._refresh_name_placeholder()

    def _set_all_genres(self, checked: bool) -> None:
        for cb in getattr(self, "genre_boxes", []):
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)
        self._on_genre_changed()

    def _mode(self) -> SequenceMode:
        if self.mode_bpm.isChecked():
            return SequenceMode.BPM
        if self.mode_key.isChecked():
            return SequenceMode.KEY
        return SequenceMode.BPM_KEY

    def _refresh_name_placeholder(self) -> None:
        genres = self.selected_genres()
        parts = []
        if genres:
            parts.append(" / ".join(sorted(g.title() for g in genres)))
        parts.append(self._mode().value.upper())
        kind = self.target_kind.currentText()
        parts.append(
            "reordered" if kind == "whole selection"
            else f"{self.target_value.value()} {kind}"
        )
        self.playlist_name.setPlaceholderText(" · ".join(parts))

    # ------------------------------------------------------------------
    # long-running actions
    # ------------------------------------------------------------------
    def _busy(self, on: bool, message: str = "") -> None:
        self.progress.setVisible(on)
        self.cancel_btn.setVisible(on)
        self.generate_btn.setEnabled(not on and bool(self.selected_sources()))
        if message:
            self.statusBar().showMessage(message)

    def _on_error(self, message: str) -> None:
        self._busy(False, "Failed")
        QMessageBox.warning(self, "Something went wrong", message)

    def on_sync(self) -> None:
        if self.runner.busy:
            return
        self._busy(True, "Syncing…")

        def job(progress, cancel):
            client = self._client()
            with db.session() as conn:
                result = sync_playlists(conn, client, progress=progress)
                sync_artists(conn, client, progress=progress)
            return result

        self.runner.start(
            job,
            on_progress=self.statusBar().showMessage,
            on_done=self._after_sync,
            on_error=self._on_error,
        )

    def _after_sync(self, result) -> None:
        self._busy(False, "Sync complete")
        if getattr(result, "unreadable", None):
            QMessageBox.information(
                self,
                "Some playlists could not be read",
                f"{len(result.unreadable)} playlist(s) you follow but do not own were "
                "skipped. Development mode cannot read those.",
            )
        self.reload_from_db()

    def on_enrich(self) -> None:
        if self.runner.busy:
            return
        try:
            cfg = load_config(require_getsongbpm=True)
        except ConfigError as exc:
            QMessageBox.warning(self, "GetSongBPM key missing", str(exc))
            return

        self._busy(True, "Enriching…")

        def job(progress, cancel):
            resolver = Resolver(
                default_sources(cfg.getsongbpm_api_key, cfg.getsongbpm_rate_per_hour)
            )
            with db.session() as conn:
                tracks = db.all_tracks(conn)
                return enrich_tracks(
                    conn,
                    resolver,
                    tracks,
                    cancel=cancel,
                    progress=lambda d, t, label: progress(f"{d}/{t} — {label}"),
                )

        self.runner.start(
            job,
            on_progress=self.statusBar().showMessage,
            on_done=lambda stats: (self._busy(False, "Enrichment done"), self.reload_from_db()),
            on_error=self._on_error,
        )

    def on_generate(self) -> None:
        """Sequence the set AND create the playlist — one action, no second step."""
        if self.export_runner.busy:
            return
        pool = self._pool()
        if not pool:
            QMessageBox.information(self, "No sources", "Select at least one source.")
            return

        genres = self.selected_genres()
        eligible = filter_tracks(pool, genres, self._artist_genres, self._aliases)

        kind = self.target_kind.currentText()
        opts = SequenceOptions(
            mode=self._mode(),
            tolerance=self.tolerance.value(),
            half_double=self.half_double.isChecked(),
            energy_boost=self.energy_boost.isChecked(),
            use_all=kind == "whole selection",
            target_tracks=self.target_value.value() if kind == "tracks" else None,
            target_minutes=float(self.target_value.value()) if kind == "minutes" else None,
        )

        result = build_set(
            eligible, self._features, opts, eligible_before_filter=len(pool)
        )
        if not result.tracks:
            QMessageBox.information(self, "No set found", result.explain())
            return
        if not result.reached_target:
            self.statusBar().showMessage(result.explain())

        self._show_result_table(result, opts)

        name = self.playlist_name.text().strip() or self.playlist_name.placeholderText()
        # Disabled for the duration: creation is automatic, so a double-click
        # would otherwise write a second playlist.
        self._busy(True, "Creating playlist…")

        def job(progress, cancel):
            with db.session() as conn:
                return export_to_spotify(
                    conn,
                    self._client(),
                    name,
                    result.tracks,
                    description=f"{opts.mode.value} · built with djset",
                    public=self.public_toggle.isChecked(),
                    progress=progress,
                )

        self.export_runner.start(
            job,
            on_progress=self.statusBar().showMessage,
            on_done=self._after_export,
            on_error=lambda m: (self._busy(False), self._export_failed(m)),
        )

    def _export_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Could not save to Spotify", message)

    def _after_export(self, export) -> None:
        self._busy(False, export.message)
        self._playlist_id = export.playlist_id
        self.link_label.setText(export.url)
        for b in (self.copy_btn, self.open_btn):
            b.setEnabled(True)
        self._dirty = False
        self.update_btn.setEnabled(False)

    def _show_result_table(self, result, opts: SequenceOptions) -> None:
        model = SetTableModel(result.tracks, self._features, opts, self)
        model.orderChanged.connect(self._on_order_changed)
        self.result_table.setModel(model)

        # BPM, key and transition quality are the whole point of the table —
        # they must never be the columns that get truncated. Only the text
        # columns flex.
        header = self.result_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # #
        header.setSectionResizeMode(1, QHeaderView.Stretch)           # Title
        header.setSectionResizeMode(2, QHeaderView.Stretch)           # Artist
        for col in (3, 4, 5):                                         # BPM/Key/Transition
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self._result_model = model
        self.result_summary.setText(model.summary_line())

    def _on_order_changed(self) -> None:
        self._dirty = True
        if self._result_model:
            self.result_summary.setText(self._result_model.summary_line())
        self.update_btn.setEnabled(self._playlist_id is not None)

    def _result_menu(self, pos) -> None:
        index = self.result_table.indexAt(pos)
        if not index.isValid() or self._result_model is None:
            return
        menu = QMenu(self)
        remove = menu.addAction("Remove track")
        if menu.exec(self.result_table.viewport().mapToGlobal(pos)) == remove:
            self._result_model.removeTrack(index.row())

    def on_update_playlist(self) -> None:
        if not (self._playlist_id and self._result_model):
            return
        tracks = self._result_model.tracks
        self._busy(True, "Updating playlist…")

        def job(progress, cancel):
            with db.session() as conn:
                update_playlist_order(
                    conn, self._client(), self._playlist_id, tracks
                )
            return True

        self.export_runner.start(
            job,
            on_done=lambda _: (
                self._busy(False, "Playlist updated"),
                self.update_btn.setEnabled(False),
            ),
            on_error=self._on_error,
        )

    def on_split_by_genre(self) -> None:
        pool = self._pool()
        if not pool:
            QMessageBox.information(self, "No sources", "Select at least one source.")
            return

        stats = genre_index(pool, self._artist_genres, self._aliases, self._features)
        buckets = {
            s.name: filter_tracks(pool, {s.name}, self._artist_genres, self._aliases)
            for s in stats
            if s.name != UNKNOWN
        }
        if not buckets:
            # Same three cases as the pane. Telling someone to run Sync when
            # Sync already ran and came back empty sends them in a circle.
            avail = genre_availability(pool, self._artist_genres)
            QMessageBox.information(
                self,
                "No genres available",
                f"{avail.headline}\n\n{avail.detail}"
                if avail.headline
                else "None of the selected tracks carry genre tags, so there is "
                "nothing to split by.",
            )
            return

        confirm = QMessageBox.question(
            self,
            "Split by genre",
            f"This will create {len(buckets)} new playlists in your account.\n\nContinue?",
        )
        if confirm != QMessageBox.Yes:
            return

        self._busy(True, "Creating playlists…")

        def job(progress, cancel):
            with db.session() as conn:
                return split_by_genre(
                    conn, self._client(), buckets, progress=progress
                )

        self.export_runner.start(
            job,
            on_progress=self.statusBar().showMessage,
            on_done=lambda results: (
                self._busy(False, f"Created {len(results)} playlists"),
            ),
            on_error=self._on_error,
        )

    def on_pick_tracks(self) -> None:
        """Hand-pick a subset of the selected sources."""
        from .track_picker import TrackPicker

        tracks = self._all_source_tracks()
        if not tracks:
            QMessageBox.information(self, "No sources", "Select at least one source.")
            return

        dialog = TrackPicker(
            tracks,
            self._features,
            getattr(self, "_picked_ids", None),
            self,
            on_manual_edit=self._save_manual_features,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        chosen = dialog.selected_ids()
        # An empty selection means "no subset", not "no tracks" — the same
        # distinction the genre pane makes.
        self._picked_ids = chosen or None
        self._recompute()

    def _save_manual_features(self, spotify_id: str, bpm, key_camelot):
        """Persist a hand-typed BPM/key and keep the in-memory copy in step.

        Fields left blank are merged with whatever is already on file, so
        supplying only the key does not discard a BPM another source found.
        """
        from ..enrichment.base import set_manual_features

        with db.session() as conn:
            features = set_manual_features(conn, spotify_id, bpm, key_camelot)
        self._features[spotify_id] = features
        return features

    def on_clear_picked(self) -> None:
        self._picked_ids = None
        self._recompute()

    def _on_target_kind(self, kind: str) -> None:
        # "whole selection" has no number to set.
        self.target_value.setEnabled(kind != "whole selection")
        self._refresh_name_placeholder()

    def on_copy_link(self) -> None:
        QGuiApplication.clipboard().setText(self.link_label.text())
        self.statusBar().showMessage("Link copied")

    def on_open_link(self) -> None:
        QDesktopServices.openUrl(QUrl(self.link_label.text()))

    def on_about(self) -> None:
        QMessageBox.about(
            self,
            "About djset",
            "<h3>djset</h3>"
            "<p>Personal Spotify playlist &amp; DJ set generator.</p>"
            "<p><b>BPM and musical key data provided by "
            f'<a href="{ATTRIBUTION_URL}">GetSongBPM</a>.</b><br>'
            f'<a href="{ATTRIBUTION_URL}">{ATTRIBUTION_URL}</a></p>'
            "<p>Spotify removed its audio-features endpoint in November 2024, so "
            "every tempo and key here comes from GetSongBPM.</p>",
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                "You reordered the set but have not pushed it to Spotify. Close anyway?",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.runner.cancel()
        self.export_runner.cancel()
        event.accept()
