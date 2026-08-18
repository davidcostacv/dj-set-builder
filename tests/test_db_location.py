"""Which database the app opens, when Windows has quietly made two of them.

Microsoft Store Python runs in an AppContainer and redirects its writes to
%LOCALAPPDATA% into a per-package LocalCache folder. The sandboxed process
reads back the path it asked for, so nothing looks wrong from inside — but the
PyInstaller build is not sandboxed, looks at the real path, and finds an empty
directory. Observed for real: the CLI held 9,802 tracks while the packaged
.exe created its own empty 84 KB database and showed an empty library.
"""

from __future__ import annotations

import sqlite3

import pytest

from djset import config


@pytest.fixture(autouse=True)
def _clear_resolution(monkeypatch):
    """db_path() memoises; each case needs a fresh decision."""
    monkeypatch.setattr(config, "_resolved", None)
    monkeypatch.delenv("DJSET_DB_PATH", raising=False)


def make_db(path, tracks: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tracks (spotify_id TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO tracks VALUES (?)", [(f"t{i}",) for i in range(tracks)]
    )
    conn.commit()
    conn.close()


def test_an_explicit_override_beats_every_heuristic(tmp_path, monkeypatch):
    wanted = tmp_path / "pinned.sqlite3"
    monkeypatch.setenv("DJSET_DB_PATH", str(wanted))
    monkeypatch.setattr(config, "_redirected_candidates", lambda: [tmp_path / "big.sqlite3"])
    make_db(tmp_path / "big.sqlite3", 9999)

    assert config.db_path() == wanted


def test_a_full_sandbox_copy_beats_an_empty_canonical_one(tmp_path, monkeypatch):
    """The live failure: the .exe made an empty database at the real path while
    every sync and enrich had gone into the sandbox."""
    canonical = tmp_path / "canonical" / "djset.sqlite3"
    redirected = tmp_path / "sandbox" / "djset.sqlite3"
    make_db(canonical, 0)
    make_db(redirected, 9802)

    monkeypatch.setattr(config, "app_data_dir", lambda: canonical.parent)
    monkeypatch.setattr(config, "_redirected_candidates", lambda: [redirected])

    assert config.db_path() == redirected


def test_the_canonical_one_is_kept_when_it_is_the_fuller(tmp_path, monkeypatch):
    """Once consolidated, the heuristic must get out of the way rather than
    keep dragging the app back into the sandbox."""
    canonical = tmp_path / "canonical" / "djset.sqlite3"
    redirected = tmp_path / "sandbox" / "djset.sqlite3"
    make_db(canonical, 9802)
    make_db(redirected, 12)

    monkeypatch.setattr(config, "app_data_dir", lambda: canonical.parent)
    monkeypatch.setattr(config, "_redirected_candidates", lambda: [redirected])

    assert config.db_path() == canonical


def test_a_tie_prefers_the_canonical_path(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical" / "djset.sqlite3"
    redirected = tmp_path / "sandbox" / "djset.sqlite3"
    make_db(canonical, 50)
    make_db(redirected, 50)

    monkeypatch.setattr(config, "app_data_dir", lambda: canonical.parent)
    monkeypatch.setattr(config, "_redirected_candidates", lambda: [redirected])

    assert config.db_path() == canonical


def test_no_sandbox_copy_is_the_ordinary_case(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical" / "djset.sqlite3"
    make_db(canonical, 10)
    monkeypatch.setattr(config, "app_data_dir", lambda: canonical.parent)
    monkeypatch.setattr(config, "_redirected_candidates", lambda: [])

    assert config.db_path() == canonical


def test_a_first_run_with_nothing_anywhere_still_returns_a_path(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical" / "djset.sqlite3"
    canonical.parent.mkdir(parents=True)
    monkeypatch.setattr(config, "app_data_dir", lambda: canonical.parent)
    monkeypatch.setattr(config, "_redirected_candidates", lambda: [])

    assert config.db_path() == canonical


def test_a_corrupt_candidate_does_not_win_and_does_not_raise(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical" / "djset.sqlite3"
    junk = tmp_path / "sandbox" / "djset.sqlite3"
    make_db(canonical, 5)
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"not a database at all")

    monkeypatch.setattr(config, "app_data_dir", lambda: canonical.parent)
    monkeypatch.setattr(config, "_redirected_candidates", lambda: [junk])

    assert config.db_path() == canonical
    assert config._track_count(junk) == -1


def test_the_decision_is_made_once(tmp_path, monkeypatch):
    """The scan opens every candidate; db_path() is called on nearly every
    operation, so repeating it would cost two SQLite opens each time."""
    canonical = tmp_path / "canonical" / "djset.sqlite3"
    make_db(canonical, 3)
    monkeypatch.setattr(config, "app_data_dir", lambda: canonical.parent)

    calls = []

    def counted():
        calls.append(1)
        return []

    monkeypatch.setattr(config, "_redirected_candidates", counted)

    for _ in range(5):
        config.db_path()
    assert len(calls) == 1
