"""GetSongBPM lookup strategy — request budget and variant handling.

A miss used to cost up to six API calls (2 artist variants x 3 title variants).
At ~70% miss rate over a 9,800-track library that is tens of thousands of
requests against a ~2,400/hour ceiling, so the wasted calls are measured in
hours. The early exit must save requests WITHOUT costing coverage.
"""

from __future__ import annotations

from djset.enrichment.getsongbpm import GetSongBPMSource
from djset.models import Track


def _track(artist="The Weeknd", title="Blinding Lights") -> Track:
    return Track(
        spotify_id="t1", uri="spotify:track:t1", title=title,
        artist=artist, artist_names=[artist],
    )


class RecordingSource(GetSongBPMSource):
    """Captures the queries issued instead of hitting the network."""

    def __init__(self, responses):
        super().__init__(api_key="k", rate_per_hour=10**9)
        self.responses = responses
        self.queries: list[str] = []

    def _get(self, path, **params):
        self.queries.append(params.get("lookup", ""))
        if callable(self.responses):
            return self.responses(params.get("lookup", ""))
        return self.responses.pop(0) if self.responses else {"search": {"error": "no result"}}


NO_RESULT = {"search": {"error": "no result"}}


def _song_row(title="Blinding Lights", artist="The Weeknd"):
    return {
        "search": [
            {"id": "x", "title": title, "artist": {"name": artist},
             "tempo": "171", "key_of": "Cm"}
        ]
    }


def test_a_hit_on_the_first_query_costs_one_request():
    src = RecordingSource([_song_row()])
    feat = src.lookup(_track())
    assert feat is not None and feat.bpm == 171.0
    assert len(src.queries) == 1


def test_an_unknown_artist_stops_after_the_first_spelling():
    """Every title variant came back empty -> the catalogue lacks this artist,
    so a second artist spelling cannot help."""
    src = RecordingSource(lambda lookup: NO_RESULT)
    assert src.lookup(_track(artist="The Weeknd", title="Some Song")) is None

    # Only the first artist variant's title variants were tried.
    assert all("artist:the weeknd" in q for q in src.queries)
    assert not any("artist:weeknd" in q and "artist:the weeknd" not in q for q in src.queries)


def test_a_known_artist_still_gets_every_variant():
    """Rows came back but none matched, so alternative spellings are worth
    trying — the early exit must not fire here."""

    def responder(lookup: str):
        # Always return a real (but wrong) row, so saw_any_rows is True.
        return _song_row(title="Totally Different Song", artist="Someone Else")

    src = RecordingSource(responder)
    assert src.lookup(_track(artist="The Weeknd", title="Blinding Lights")) is None

    artists_tried = {q.split("artist:")[1] for q in src.queries if "artist:" in q}
    assert len(artists_tried) > 1  # both spellings attempted


def test_early_exit_reduces_the_request_budget():
    src = RecordingSource(lambda lookup: NO_RESULT)
    src.lookup(_track(artist="The Weeknd", title="Song (Extended Mix)"))
    # Three title variants for one artist spelling, not six across two.
    assert len(src.queries) <= 3


def test_a_later_title_variant_can_still_win():
    def responder(lookup: str):
        # Only the bare title is indexed, not the parenthesised form.
        return _song_row() if "song:blinding lights " in lookup + " " else NO_RESULT

    src = RecordingSource(responder)
    feat = src.lookup(_track(title="Blinding Lights (Extended Mix)"))
    assert feat is not None
    assert feat.key_camelot == "5A"  # Cm


def test_artist_without_variants_is_queried_once():
    src = RecordingSource(lambda lookup: NO_RESULT)
    src.lookup(_track(artist="Daft Punk", title="One More Time"))
    assert len(src.queries) == 1  # single artist form, single title form
