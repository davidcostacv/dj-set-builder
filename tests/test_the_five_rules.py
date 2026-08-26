"""The five rules the set has to obey, checked end to end against a fake
Spotify rather than at any single layer.

Each was asked for explicitly, and each has already been broken once by a
change made somewhere else, so they are asserted through the whole chain:
generate -> what the page shows -> what the export sends -> what Spotify is
actually told to add.

  1. a track with no BPM or key is still in the playlist that reaches Spotify
  2. the same recording never appears twice, even when the source has it twice
  3. anything in the selection but not in the set is reported, with reasons
  4. duplicates can be removed from the source playlist in place, named first
  5. the tracks with no BPM or key are at the *front* of the set
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from djset import db  # noqa: E402
from djset.models import AudioFeatures, Track  # noqa: E402


def _t(tid: str, title: str, isrc: str | None = None) -> Track:
    return Track(
        spotify_id=tid,
        uri=f"spotify:track:{tid}",
        title=title,
        artist="A Band",
        artist_names=["A Band"],
        isrc=isrc,
    )


class FakeSpotify:
    """Records what it is told to do instead of doing it."""

    def __init__(self):
        self.added: list[str] = []
        self.replaced: list[str] = []
        self.created: dict | None = None

    def create_playlist(self, name, description="", public=False):
        self.created = {"name": name, "description": description}
        return {"id": "new-playlist"}

    def add_items(self, playlist_id, uris):
        self.added.extend(uris)
        return {}

    def replace_items(self, playlist_id, uris):
        self.replaced = list(uris)
        self.added = list(uris)
        return {}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """A playlist holding: three mixable tracks, two with no BPM/key at all,
    and the same recording twice, only one copy of which has data."""
    monkeypatch.setenv("DJSET_DB_PATH", str(tmp_path / "rules.sqlite3"))
    from djset import config

    monkeypatch.setattr(config, "_resolved", None)

    with db.session() as conn:
        rows = [
            _t("mix1", "Mixable One"),
            _t("mix2", "Mixable Two"),
            _t("mix3", "Mixable Three"),
            _t("bare1", "No Data One"),
            _t("bare2", "No Data Two"),
            _t("dupA", "Twice Over", isrc="XX1111111111"),
            _t("dupB", "Twice Over", isrc="XX1111111111"),
        ]
        for t in rows:
            db.upsert_track(conn, t)
        db.upsert_playlist_cache(conn, "src", "Source List", None, len(rows))
        db.set_playlist_members(
            conn, "src", [(t.spotify_id, i, None) for i, t in enumerate(rows)]
        )
        for tid, bpm, key in [
            ("mix1", 128.0, "8A"),
            ("mix2", 129.0, "8A"),
            ("mix3", 130.0, "9A"),
            ("dupB", 127.0, "8A"),  # only the SECOND copy is sequenceable
        ]:
            db.upsert_features(conn, AudioFeatures(tid, bpm, key, source="test"))

    from djset.web import app as web_app

    web_app.library.loaded = False
    monkeypatch.setattr(web_app, "load_config", lambda *a, **k: object())
    monkeypatch.setattr(web_app, "SpotifyAuth", lambda cfg: object())
    return web_app, TestClient(web_app.app)


def _generate(client):
    r = client.post(
        "/api/generate",
        json={"sources": ["src"], "target_kind": "all", "target_value": 100},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------


def test_rule_5_the_tracks_with_no_bpm_or_key_come_first(app):
    _, client = app
    body = _generate(client)
    carried = body["carried"]

    assert carried == 2
    assert [t["title"] for t in body["tracks"][:2]] == ["No Data One", "No Data Two"]
    assert all(t["carried"] for t in body["tracks"][:carried])
    assert not any(t["carried"] for t in body["tracks"][carried:])


def test_rule_2_the_duplicate_appears_once_in_the_set(app):
    _, client = app
    body = _generate(client)
    titles = [t["title"] for t in body["tracks"]]

    assert titles.count("Twice Over") == 1
    # and it is the copy that can actually be mixed
    kept = next(t for t in body["tracks"] if t["title"] == "Twice Over")
    assert kept["id"] == "dupB"
    assert kept["carried"] is False


def test_rule_3_the_track_left_out_is_reported_by_name(app):
    _, client = app
    info = _generate(client)["left_out"]

    assert info["total"] == 1
    assert info["duplicates"] == 1
    assert info["duplicate_tracks"][0]["title"] == "Twice Over"
    assert info["duplicate_tracks"][0]["copies"] == 2
    # and the parts account for the whole
    assert (
        info["duplicates"] + info["genre_filtered"] + info["not_chosen"]
        == info["total"]
    )


def test_rule_1_every_track_shown_reaches_spotify(app, monkeypatch):
    """The rule most easily broken by a change elsewhere: the page can show a
    track and the export still drop it."""
    web_app, client = app
    body = _generate(client)

    fake = FakeSpotify()
    monkeypatch.setattr(web_app, "SpotifyClient", lambda auth: fake)

    r = client.post(
        "/api/export",
        json={
            "name": "Set",
            "uris": [t["uri"] for t in body["tracks"]],
            "sources": ["src"],
        },
    )
    assert r.status_code == 200, r.text

    assert fake.added == [t["uri"] for t in body["tracks"]]  # order preserved
    for tid in ("bare1", "bare2"):
        assert f"spotify:track:{tid}" in fake.added, f"{tid} never reached Spotify"
    assert fake.added.count("spotify:track:dupB") == 1
    assert "spotify:track:dupA" not in fake.added


def test_rule_1_the_carried_tracks_open_the_saved_playlist_too(app, monkeypatch):
    web_app, client = app
    body = _generate(client)
    fake = FakeSpotify()
    monkeypatch.setattr(web_app, "SpotifyClient", lambda auth: fake)
    client.post(
        "/api/export",
        json={
            "name": "Set",
            "uris": [t["uri"] for t in body["tracks"]],
            "sources": ["src"],
        },
    )

    assert fake.added[:2] == ["spotify:track:bare1", "spotify:track:bare2"]


def test_rule_4_duplicates_are_named_before_anything_is_removed(app):
    _, client = app
    body = client.post("/api/duplicates", json={"sources": ["src"]}).json()

    assert len(body["playlists"]) == 1
    pl = body["playlists"][0]
    assert pl["name"] == "Source List"
    assert pl["extra_copies"] == 1
    assert pl["songs"] == [{"title": "Twice Over", "artist": "A Band", "copies": 2}]


def test_rule_4_removal_edits_the_playlist_in_place(app, monkeypatch):
    web_app, client = app
    fake = FakeSpotify()
    monkeypatch.setattr(web_app, "SpotifyClient", lambda auth: fake)

    body = client.post("/api/dedupe", json={"playlist_id": "src"}).json()

    assert body["removed"] == 1
    assert body["remaining"] == 6
    assert body["songs"][0]["title"] == "Twice Over"
    # in place: the existing playlist is rewritten, no new one is created
    assert fake.created is None
    assert fake.replaced, "the playlist was never rewritten"
    assert fake.replaced.count("spotify:track:dupB") == 1
    assert "spotify:track:dupA" not in fake.replaced
    # and everything else survives, unmixable tracks included
    for tid in ("mix1", "mix2", "mix3", "bare1", "bare2"):
        assert f"spotify:track:{tid}" in fake.replaced


def test_rule_4_the_second_run_has_nothing_to_do(app, monkeypatch):
    """Removing duplicates twice must not remove anything the second time."""
    web_app, client = app
    fake = FakeSpotify()
    monkeypatch.setattr(web_app, "SpotifyClient", lambda auth: fake)

    assert client.post("/api/dedupe", json={"playlist_id": "src"}).json()["removed"] == 1
    again = client.post("/api/dedupe", json={"playlist_id": "src"}).json()
    assert again["removed"] == 0
    assert again["remaining"] == 6
