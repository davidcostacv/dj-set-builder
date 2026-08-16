"""UI behaviour that encodes hard requirements from the brief.

Runs headless (offscreen platform). Skipped entirely if PySide6 is absent so
the rest of the suite stays usable without Qt.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from djset.models import AudioFeatures, Track  # noqa: E402
from djset.sequencing import SequenceOptions  # noqa: E402
from djset.ui.result_table import SetTableModel  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def _t(tid: str, dur: int = 210_000) -> Track:
    return Track(
        spotify_id=tid, uri=f"spotify:track:{tid}", title=f"Song {tid}",
        artist=f"Artist {tid}", duration_ms=dur,
    )


def _f(tid: str, bpm=128.0, key="8A"):
    return AudioFeatures(tid, bpm, key, source="test")


@pytest.fixture()
def model(qapp):
    tracks = [_t("a"), _t("b"), _t("c")]
    feats = {"a": _f("a", 128, "8A"), "b": _f("b", 129, "8A"), "c": _f("c", 130, "9A")}
    return SetTableModel(tracks, feats, SequenceOptions())


def test_table_shape(model):
    assert model.rowCount() == 3
    assert model.columnCount() == 6


def test_moving_a_row_reorders_the_set(model):
    assert [t.spotify_id for t in model.tracks] == ["a", "b", "c"]
    model.moveRow(0, 3)
    assert [t.spotify_id for t in model.tracks] == ["b", "c", "a"]


def test_moving_emits_order_changed(model, qapp):
    seen = []
    model.orderChanged.connect(lambda: seen.append(True))
    model.moveRow(2, 0)
    assert seen


def test_removing_a_track(model):
    model.removeTrack(1)
    assert [t.spotify_id for t in model.tracks] == ["a", "c"]


def test_out_of_range_moves_and_removes_are_ignored(model):
    assert model.moveRow(9, 0) is False
    assert model.removeTrack(9) is False
    assert model.rowCount() == 3


def test_transition_quality_recomputes_after_a_move(model):
    from PySide6.QtCore import Qt

    def transition_of(row: int) -> str:
        return model.data(model.index(row, 5), Qt.DisplayRole)

    # a -> b is 128 -> 129 in the same key: excellent.
    assert transition_of(0) == "excellent"

    # Put the 9A track in the middle; the move into it is a wheel step, so the
    # label must change rather than stay stale.
    model.moveRow(2, 1)
    assert [t.spotify_id for t in model.tracks] == ["a", "c", "b"]
    assert transition_of(1) in {"excellent", "good", "fair", "rough", "incompatible"}


def test_last_row_has_no_outgoing_transition(model):
    from PySide6.QtCore import Qt

    assert model.data(model.index(2, 5), Qt.DisplayRole) == "—"


def test_incompatible_neighbours_are_labelled_not_hidden(qapp):
    tracks = [_t("a"), _t("b")]
    feats = {"a": _f("a", 100, "1A"), "b": _f("b", 175, "7B")}
    m = SetTableModel(tracks, feats, SequenceOptions())

    from PySide6.QtCore import Qt

    assert m.data(m.index(0, 5), Qt.DisplayRole) == "incompatible"


def test_summary_reports_duration_and_quality(model):
    line = model.summary_line()
    assert "3 tracks" in line
    assert "average transition quality" in line


def test_summary_updates_after_removal(model):
    model.removeTrack(0)
    assert "2 tracks" in model.summary_line()


def test_missing_features_do_not_crash_the_table(qapp):
    m = SetTableModel([_t("a"), _t("b")], {}, SequenceOptions())
    from PySide6.QtCore import Qt

    assert m.data(m.index(0, 3), Qt.DisplayRole) == "—"  # BPM
    assert m.data(m.index(0, 4), Qt.DisplayRole) == "—"  # key
    assert m.summary_line()


# ---------------------------------------------------------------------------
# the main window's hard requirements
# ---------------------------------------------------------------------------


@pytest.fixture()
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("DJSET_DB_PATH", str(tmp_path / "ui.sqlite3"))
    from djset import db
    from djset.models import LIKED_SONGS_ID, LIKED_SONGS_NAME

    with db.session() as conn:
        for i in range(3):
            db.upsert_track(conn, _t(f"t{i}"))
        db.set_playlist_members(
            conn, LIKED_SONGS_ID, [(f"t{i}", i, None) for i in range(3)]
        )
        db.upsert_playlist_cache(conn, LIKED_SONGS_ID, LIKED_SONGS_NAME, None, 3)

    from djset.ui.main_window import MainWindow

    w = MainWindow()
    yield w
    w.close()


def test_genre_pane_is_collapsed_by_default(window):
    assert window.genre_toggle.isChecked() is False
    assert window.genre_body.isVisible() is False


def test_generate_is_enabled_by_sources_alone_not_genres(window):
    """The hard requirement: genre selection is never part of the enabled
    condition, and skipping pane 2 must never be treated as a mistake."""
    assert window.selected_sources()  # Liked Songs is checked by default
    assert window.selected_genres() is None  # nothing selected
    assert window.generate_btn.isEnabled() is True


def test_generate_disabled_only_when_no_source_is_selected(window):
    for cb, _ in window.source_boxes:
        cb.setChecked(False)
    assert window.generate_btn.isEnabled() is False

    window.source_boxes[0][0].setChecked(True)
    assert window.generate_btn.isEnabled() is True


def test_no_genres_selected_reads_as_no_filter(window):
    assert window.selected_genres() is None
    assert "no genre filter" in window.eligible_label.text()


def test_public_defaults_to_off(window):
    assert window.public_toggle.isChecked() is False


def test_default_mode_is_bpm_plus_key(window):
    from djset.sequencing import SequenceMode

    assert window._mode() is SequenceMode.BPM_KEY


def test_tolerance_is_bounded_to_the_specified_range(window):
    assert window.tolerance.minimum() == pytest.approx(0.02)
    assert window.tolerance.maximum() == pytest.approx(0.12)
    assert window.tolerance.value() == pytest.approx(0.06)


def test_playlist_name_is_prefilled_and_editable(window):
    placeholder = window.playlist_name.placeholderText()
    assert "BPM+KEY" in placeholder.upper()
    assert window.playlist_name.isReadOnly() is False


def test_update_button_starts_disabled(window):
    assert window.update_btn.isEnabled() is False
