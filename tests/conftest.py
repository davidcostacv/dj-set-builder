import sqlite3

import pytest

from djset import db
from djset.models import Track


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "test.sqlite3")
    yield c
    c.close()


def make_track(n: int = 1, **kw) -> Track:
    base = dict(
        spotify_id=f"t{n}",
        uri=f"spotify:track:t{n}",
        title=f"Track {n}",
        artist=f"Artist {n}",
        artist_ids=[f"a{n}"],
        isrc=f"ISRC{n:08d}",
        album="Album",
        duration_ms=210_000,
        added_at="2026-01-01T00:00:00Z",
    )
    base.update(kw)
    return Track(**base)


@pytest.fixture()
def track_factory():
    return make_track
