"""The HTTP layer — step W1.

It is deliberately thin, so these tests are about the seams rather than the
engine: that a route hands the right arguments to logic already tested
elsewhere, and that the two rules the desktop build got wrong stay right here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from djset import db  # noqa: E402
from djset.models import AudioFeatures, Track  # noqa: E402


def _t(tid: str, artist: str = "A") -> Track:
    return Track(
        spotify_id=tid, uri=f"spotify:track:{tid}", title=f"Song {tid}",
        artist=artist, artist_ids=[f"ar-{artist}"], duration_ms=200_000,
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DJSET_DB_PATH", str(tmp_path / "web.sqlite3"))
    from djset import config

    monkeypatch.setattr(config, "_resolved", None)

    with db.session() as conn:
        for i in range(6):
            db.upsert_track(conn, _t(f"t{i}"))
        # Two playlists that overlap on t2, so the union can be checked.
        db.upsert_playlist_cache(conn, "pl-a", "Alpha", None, 3)
        db.set_playlist_members(conn, "pl-a", [("t0", 0, None), ("t1", 1, None), ("t2", 2, None)])
        db.upsert_playlist_cache(conn, "pl-b", "Beta", None, 3)
        db.set_playlist_members(conn, "pl-b", [("t2", 0, None), ("t3", 1, None), ("t4", 2, None)])
        for i in range(5):
            db.upsert_features(
                conn, AudioFeatures(f"t{i}", 128.0 + i, "8A", source="test")
            )

    from djset.web import app as web_app

    web_app.library.loaded = False          # each test gets a fresh read
    return TestClient(web_app.app)


# ---------------------------------------------------------------------------
# the library
# ---------------------------------------------------------------------------


def test_health_reports_the_library_it_actually_opened(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["tracks"] == 6
    assert body["playlists"] == 2
    assert body["sequenceable"] == 5   # t5 has no features


def test_sources_lists_every_playlist(client):
    names = {s["name"]: s["tracks"] for s in client.get("/api/sources").json()}
    assert names == {"Alpha": 3, "Beta": 3}


# ---------------------------------------------------------------------------
# the pool: the desktop build shipped a bug here
# ---------------------------------------------------------------------------


def test_the_pool_is_exactly_what_was_asked_for(client):
    """No source is ever added on the caller's behalf. In the Qt build Liked
    Songs was pre-selected, so choosing a playlist silently produced the union
    of the two — a 66-track playlist yielded a 1,103-track pool."""
    body = client.post("/api/selection", json={"sources": ["pl-a"]}).json()
    assert body["pool"] == 3


def test_no_sources_means_an_empty_pool_not_the_whole_library(client):
    body = client.post("/api/selection", json={"sources": []}).json()
    assert body["pool"] == 0


def test_a_track_in_two_selected_playlists_is_counted_once(client):
    body = client.post("/api/selection", json={"sources": ["pl-a", "pl-b"]}).json()
    assert body["pool"] == 5    # t0..t4, not 6


def test_an_unknown_playlist_id_contributes_nothing(client):
    body = client.post("/api/selection", json={"sources": ["pl-a", "nope"]}).json()
    assert body["pool"] == 3


def test_a_hand_picked_subset_narrows_the_pool(client):
    body = client.post(
        "/api/selection", json={"sources": ["pl-a"], "picked": ["t0", "t1"]}
    ).json()
    assert body["pool"] == 2


# ---------------------------------------------------------------------------
# no genre selected means NO FILTER
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("genres", [None, []])
def test_null_and_empty_genres_both_mean_no_filter(client, genres):
    """The distinction the whole design rests on: it must never become a
    filter that excludes everything."""
    body = client.post("/api/selection", json={"sources": ["pl-a"], "genres": genres}).json()
    assert body["eligible"]["total"] == 3
    assert body["eligible"]["filtered"] is False
    assert "no genre filter" in body["eligible"]["label"]


def test_the_availability_notice_travels_to_the_client(client):
    """Untagged artists must produce an explanation, not a blank pane."""
    body = client.post("/api/selection", json={"sources": ["pl-a"]}).json()
    a = body["availability"]
    assert a["usable"] is False
    assert a["headline"]
    assert "Generate" in a["detail"]


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def test_generate_returns_a_sequenced_set(client):
    body = client.post(
        "/api/generate",
        json={"sources": ["pl-a", "pl-b"], "target_kind": "tracks", "target_value": 4},
    ).json()
    assert body["count"] == 4
    assert len(body["tracks"]) == 4
    assert all(t["bpm"] and t["key"] for t in body["tracks"])
    assert 0.0 <= body["average_quality"] <= 1.0


def test_generate_writes_nothing_to_the_account(client):
    """Splitting sequence from save is the one deliberate difference from the
    desktop build, where generating also created the playlist."""
    before = client.get("/api/health").json()
    client.post("/api/generate", json={"sources": ["pl-a"], "target_value": 2})
    assert client.get("/api/health").json() == before


def test_generate_without_sources_is_refused(client):
    r = client.post("/api/generate", json={"sources": []})
    assert r.status_code == 400
    assert "No sources" in r.json()["detail"]


def test_an_unknown_mode_is_refused_by_name(client):
    r = client.post("/api/generate", json={"sources": ["pl-a"], "mode": "vibes"})
    assert r.status_code == 400
    assert "vibes" in r.json()["detail"]


def test_a_short_set_explains_itself_rather_than_failing(client):
    body = client.post(
        "/api/generate", json={"sources": ["pl-a"], "target_value": 500}
    ).json()
    assert body["reached_target"] is False
    assert body["explain"]
    assert body["limiting_factor"]


def test_export_with_nothing_selected_is_refused(client):
    r = client.post("/api/export", json={"name": "x", "uris": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# the page itself
# ---------------------------------------------------------------------------


def test_the_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "djset" in r.text


def test_the_static_assets_are_served(client):
    for asset in ("/static/app.js", "/static/app.css"):
        assert client.get(asset).status_code == 200
