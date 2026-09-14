import pytest

from djset import db
from djset.models import AudioFeatures
from djset.report import build_coverage


def test_track_round_trip(conn, track_factory):
    t = track_factory(1, artist_ids=["a1", "a2"])
    db.upsert_track(conn, t)
    got = db.all_tracks(conn)[0]
    assert got == t


def test_upsert_does_not_lose_a_known_isrc(conn, track_factory):
    db.upsert_track(conn, track_factory(1, isrc="ISRCGOOD"))
    db.upsert_track(conn, track_factory(1, isrc=None))
    assert db.all_tracks(conn)[0].isrc == "ISRCGOOD"


def test_playlist_membership_is_replaced_wholesale(conn, track_factory):
    for i in (1, 2, 3):
        db.upsert_track(conn, track_factory(i))

    db.set_playlist_members(conn, "p1", [("t1", 0, None), ("t2", 1, None)])
    assert {t.spotify_id for t in db.tracks_in_playlists(conn, ["p1"])} == {"t1", "t2"}

    db.set_playlist_members(conn, "p1", [("t3", 0, None)])
    assert {t.spotify_id for t in db.tracks_in_playlists(conn, ["p1"])} == {"t3"}


def test_selecting_several_playlists_unions_them(conn, track_factory):
    for i in (1, 2, 3):
        db.upsert_track(conn, track_factory(i))
    db.set_playlist_members(conn, "p1", [("t1", 0, None), ("t2", 1, None)])
    db.set_playlist_members(conn, "p2", [("t2", 0, None), ("t3", 1, None)])

    got = {t.spotify_id for t in db.tracks_in_playlists(conn, ["p1", "p2"])}
    assert got == {"t1", "t2", "t3"}
    assert db.tracks_in_playlists(conn, []) == []


def test_snapshot_cache(conn):
    assert db.cached_snapshot(conn, "p1") is None
    db.upsert_playlist_cache(conn, "p1", "House", "snap1", 42)
    assert db.cached_snapshot(conn, "p1") == "snap1"
    db.upsert_playlist_cache(conn, "p1", "House", "snap2", 43)
    assert db.cached_snapshot(conn, "p1") == "snap2"


def test_delete_playlist_cache_forgets_membership_and_exports(conn, track_factory):
    db.upsert_track(conn, track_factory(1))
    db.set_playlist_members(conn, "p1", [("t1", 0, None)])
    db.upsert_playlist_cache(conn, "p1", "House", "snap1", 1)
    db.record_export(conn, "p1", "hash1", "House Mix")

    db.delete_playlist_cache(conn, "p1")

    assert db.tracks_in_playlists(conn, ["p1"]) == []
    assert [p["spotify_id"] for p in db.cached_playlists(conn)] == []
    assert db.find_export(conn, "hash1", "House Mix") is None


def test_delete_playlist_cache_leaves_other_playlists_alone(conn, track_factory):
    db.upsert_track(conn, track_factory(1))
    db.upsert_track(conn, track_factory(2))
    db.set_playlist_members(conn, "p1", [("t1", 0, None)])
    db.set_playlist_members(conn, "p2", [("t2", 0, None)])
    db.upsert_playlist_cache(conn, "p1", "House", None, 1)
    db.upsert_playlist_cache(conn, "p2", "Techno", None, 1)

    db.delete_playlist_cache(conn, "p1")

    assert {t.spotify_id for t in db.tracks_in_playlists(conn, ["p2"])} == {"t2"}
    assert [p["spotify_id"] for p in db.cached_playlists(conn)] == ["p2"]


def test_genre_aliases_are_seeded_and_editable(conn):
    aliases = db.genre_aliases(conn)
    assert aliases["trap latino"] == "reggaeton"
    assert aliases["tech house"] == "house"

    db.set_genre_alias(conn, "Tech House", "techno")
    assert db.genre_aliases(conn)["tech house"] == "techno"


def test_seeding_does_not_clobber_my_edits(conn, tmp_path):
    db.set_genre_alias(conn, "tech house", "techno")
    conn.commit()
    conn.close()

    reopened = db.connect(tmp_path / "test.sqlite3")
    assert db.genre_aliases(reopened)["tech house"] == "techno"
    reopened.close()


def test_coverage_counts(conn, track_factory):
    for i in (1, 2, 3, 4):
        db.upsert_track(conn, track_factory(i))
    db.upsert_artist(conn, "a1", "Artist 1", ["tech house", "house"])
    db.upsert_artist(conn, "a2", "Artist 2", [])

    db.upsert_features(conn, AudioFeatures("t1", 128.0, "8A", source="getsongbpm"))
    db.upsert_features(conn, AudioFeatures("t2", 124.0, None, source="getsongbpm"))
    db.upsert_features(conn, AudioFeatures("t3", None, "5A", source="manual"))

    cov = build_coverage(conn)
    assert cov.total_tracks == 4
    # t4 was never looked up, so it is not part of the denominator.
    assert cov.measured == 3
    assert cov.with_bpm == 2
    assert cov.with_key == 2
    assert cov.with_both == 1
    assert cov.pct(cov.with_both) == pytest.approx(33.33, abs=0.01)
    assert cov.by_source["getsongbpm"] == 2
    assert cov.readout.startswith("1 tracks can be sequenced")
    # t2, t3, t4 lack BPM+key, so their artists show up as unresolved.
    assert dict(cov.unresolved_artists)["Artist 2"] == 1
    assert cov.untagged_tracks == 3  # only Artist 1 carries tags
    assert ("tech house", 1) in cov.raw_genres
