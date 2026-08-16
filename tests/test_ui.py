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
        artist=f"Artist {tid}", artist_ids=[f"artist-{tid}"], duration_ms=dur,
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


# ---------------------------------------------------------------------------
# manual BPM/key entry from the track picker
# ---------------------------------------------------------------------------


@pytest.fixture()
def picker_bits(qapp):
    """A picker over two tracks: one complete, one missing its key."""
    from djset.ui.track_picker import TrackPicker

    tracks = [_t("good"), _t("keyless")]
    features = {
        "good": _f("good", 128.0, "8A"),
        "keyless": AudioFeatures("keyless", 122.5, None, source="deezer"),
    }
    saved: list[tuple] = []

    def on_manual_edit(sid, bpm, key):
        saved.append((sid, bpm, key))
        # Stand in for set_manual_features, which merges rather than replaces.
        old = features[sid]
        merged = AudioFeatures(
            sid,
            bpm if bpm is not None else old.bpm,
            key if key is not None else old.key_camelot,
            source="manual",
            confidence=1.0,
        )
        features[sid] = merged
        return merged

    picker = TrackPicker(tracks, features, None, None, on_manual_edit=on_manual_edit)
    return picker, features, saved


def _row(picker, spotify_id):
    from PySide6.QtCore import Qt

    for item in picker._items:
        if item.data(0, Qt.UserRole) == spotify_id:
            return item
    raise AssertionError(f"no row for {spotify_id}")


def test_the_edit_button_is_hidden_without_somewhere_to_save(qapp):
    """The dialog must not offer an action it cannot carry out."""
    from djset.ui.track_picker import TrackPicker

    picker = TrackPicker([_t("a")], {"a": _f("a")}, None, None)
    assert picker.edit_btn.isEnabled() is False


def test_editing_persists_through_the_callback_and_updates_the_row(picker_bits):
    picker, features, saved = picker_bits
    from PySide6.QtWidgets import QDialog

    from djset.ui.track_picker import SHOW_ALL

    picker.filter_mode.setCurrentText(SHOW_ALL)
    item = _row(picker, "keyless")
    assert item.text(3) == "—"  # no key to begin with
    picker.tree.setCurrentItem(item)
    item.setSelected(True)

    # Drive the dialog without showing it: accept a typed key, no BPM.
    class FakeDialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.Accepted

        def values(self):
            return None, "5A"

    import djset.ui.manual_features as mf

    real = mf.ManualFeaturesDialog
    mf.ManualFeaturesDialog = FakeDialog
    try:
        picker._edit_current()
    finally:
        mf.ManualFeaturesDialog = real

    assert saved == [("keyless", None, "5A")]
    assert features["keyless"].key_camelot == "5A"
    # The BPM Deezer supplied survives: the point of merging.
    assert features["keyless"].bpm == pytest.approx(122.5)
    assert item.text(3) == "5A"
    assert item.text(2) == "122"


def test_missing_data_is_reachable_rather_than_hidden(picker_bits):
    """The point of the third filter state. With only a "usable" toggle, the
    tracks worth hand-fixing are exactly the ones hidden from view, so the
    feature would only be findable by someone who already knew it existed."""
    from djset.ui.track_picker import SHOW_ALL, SHOW_MISSING, SHOW_USABLE

    picker, _, _ = picker_bits
    keyless, good = _row(picker, "keyless"), _row(picker, "good")

    picker.filter_mode.setCurrentText(SHOW_USABLE)
    assert keyless.isHidden() and not good.isHidden()

    picker.filter_mode.setCurrentText(SHOW_MISSING)
    assert not keyless.isHidden() and good.isHidden()

    picker.filter_mode.setCurrentText(SHOW_ALL)
    assert not keyless.isHidden() and not good.isHidden()


def test_a_fixed_track_moves_between_buckets(picker_bits):
    from djset.ui.track_picker import SHOW_USABLE

    picker, features, _ = picker_bits
    picker.filter_mode.setCurrentText(SHOW_USABLE)
    item = _row(picker, "keyless")
    assert item.isHidden()

    features["keyless"] = AudioFeatures("keyless", 122.5, "5A", source="manual")
    picker._apply_filter()

    assert not item.isHidden()


def test_the_button_needs_exactly_one_visible_selected_row(picker_bits):
    from djset.ui.track_picker import SHOW_ALL

    picker, _, _ = picker_bits
    picker.filter_mode.setCurrentText(SHOW_ALL)

    picker.tree.clearSelection()
    picker._update_edit_button()
    assert picker.edit_btn.isEnabled() is False

    good = _row(picker, "good")
    picker.tree.setCurrentItem(good)
    good.setSelected(True)
    picker._update_edit_button()
    assert picker.edit_btn.isEnabled() is True

    # Two rows is ambiguous: there is only one dialog.
    _row(picker, "keyless").setSelected(True)
    picker._update_edit_button()
    assert picker.edit_btn.isEnabled() is False


# ---------------------------------------------------------------------------
# the genre pane explains itself instead of going blank
# ---------------------------------------------------------------------------


def test_an_unavailable_genre_pane_says_so_on_the_collapsed_button(window):
    """The pane is collapsed by default, so an explanation only visible after
    expanding is one most people would never see."""
    assert window.genre_toggle.isChecked() is False
    assert "unavailable" in window.genre_toggle.text().lower()


def test_the_notice_replaces_the_checkbox_list(window):
    """With nothing tagged every track falls into Unknown, so the list would
    otherwise offer one bucket covering everything — an option that filters
    nothing while looking like a working filter.

    isHidden() rather than isVisible(): a child of an unshown top-level window
    is never "visible", so isVisible() would pass here no matter what the code
    did.
    """
    assert window.genre_boxes == []
    assert window.genre_notice.isHidden() is False

    text = window.genre_notice.text()
    assert "No genre tags" in text or "not fetched" in text
    # Both wordings must say the filter is optional; which one appears depends
    # on whether artists were fetched, and test_genre_availability covers that.
    assert "Generate" in text


def test_the_bulk_buttons_are_disabled_with_nothing_to_select(window):
    assert window.genre_select_all.isEnabled() is False
    assert window.genre_clear.isEnabled() is False


def test_an_unavailable_pane_still_means_no_filter_not_no_tracks(window):
    """The distinction the whole design rests on. An empty pane must not
    quietly become a filter that excludes everything."""
    assert window.selected_genres() is None
    assert window.generate_btn.isEnabled() is True
    assert "no genre filter" in window.eligible_label.text().lower()


def test_the_pane_recovers_when_tags_do_show_up(window):
    """If the artist genres question resolves, nothing needs rewiring."""
    window._artist_genres = {f"artist-t{i}": ["tech house"] for i in range(3)}
    window._recompute()

    assert window.genre_notice.isHidden() is True
    # "tech house" collapses to "house" via the seeded alias table.
    assert [cb.text().split("  —")[0] for cb in window.genre_boxes] == ["house"]
    assert window.genre_select_all.isEnabled() is True
    assert "unavailable" not in window.genre_toggle.text().lower()
