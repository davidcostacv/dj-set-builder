"""An empty genre pane has three causes and must not look identical in all of them.

Settled on 19 Aug 2026: Spotify has removed artist genres from its API. The
artist object no longer carries the field at all — checked against five
artists including Lana Del Rey, the response contains only external_urls,
href, id, images, name, type and uri.

So the pane still distinguishes its three states, because the counts differ
and a reader deserves to know what was actually checked, but none of them may
suggest that fetching would help. It cannot. Every wording also says Generate
is unaffected, because a filter that looks broken invites the reader to assume
it blocks something.
"""

from __future__ import annotations

from djset.filtering import genre_availability, genre_index
from djset.models import Track


def T(tid: str, *artist_ids: str) -> Track:
    return Track(
        spotify_id=tid,
        uri=f"spotify:track:{tid}",
        title=f"Track {tid}",
        artist="Someone",
        artist_ids=list(artist_ids),
    )


# ---------------------------------------------------------------------------
# the three states
# ---------------------------------------------------------------------------


def test_nothing_selected_has_nothing_to_explain():
    a = genre_availability([], {})
    assert a.artists == 0
    assert a.headline is None and a.detail is None
    assert not a.usable


def test_nothing_fetched_no_longer_sends_them_to_sync():
    """It used to say "run Sync". Syncing cannot return a field the API stopped
    sending, and sending someone to do it is worse than saying nothing."""
    a = genre_availability([T("t1", "a1"), T("t2", "a2")], {})

    assert (a.artists, a.fetched, a.tagged) == (2, 0, 0)
    assert not a.usable
    assert "not fetched" in a.headline
    assert "would not produce any" in a.detail
    assert "Run Sync" not in a.detail


def test_fetched_but_empty_does_not_send_them_to_sync():
    """The live state of this library. Telling someone to run Sync when Sync
    already ran and came back empty sends them in a circle."""
    a = genre_availability([T("t1", "a1"), T("t2", "a2")], {"a1": [], "a2": []})

    assert (a.artists, a.fetched, a.tagged) == (2, 2, 0)
    assert not a.usable
    assert "No genre tags" in a.headline
    assert "Run Sync" not in a.detail  # everything that could be fetched, was
    assert "removed artist genres" in a.detail  # states the settled cause


def test_a_half_finished_sync_does_not_claim_more_than_it_checked():
    """The live state of this library: an interrupted sync fetched 600 of 4,615
    artists. Saying "no tags for any of these 4,615" asserts something nobody
    has looked at."""
    tracks = [T(f"t{i}", f"a{i}") for i in range(10)]
    a = genre_availability(tracks, {"a0": [], "a1": [], "a2": []})

    assert (a.artists, a.fetched, a.tagged) == (10, 3, 0)
    assert a.partial
    assert "3 of 10" in a.headline
    assert "3 of 10 artists here were fetched" in a.detail
    assert "will not help" in a.detail  # fetching more is pointless now


def test_a_complete_fetch_can_speak_for_the_whole_selection():
    tracks = [T(f"t{i}", f"a{i}") for i in range(3)]
    a = genre_availability(tracks, {f"a{i}": [] for i in range(3)})

    assert not a.partial
    assert "any of these 3 artists" in a.headline
    assert "All 3 artists here" in a.detail


def test_tags_present_needs_no_explanation():
    a = genre_availability([T("t1", "a1")], {"a1": ["tech house"]})

    assert a.usable
    assert a.headline is None and a.detail is None


def test_every_unusable_state_says_generate_still_works():
    """The filter is optional and never gates Generate. A pane that looks
    broken invites people to think it does."""
    for artist_genres in ({}, {"a1": []}):
        a = genre_availability([T("t1", "a1")], artist_genres)
        assert "Generate" in a.detail


# ---------------------------------------------------------------------------
# counting
# ---------------------------------------------------------------------------


def test_artists_are_counted_once_across_tracks():
    tracks = [T("t1", "a1"), T("t2", "a1"), T("t3", "a1", "a2")]
    assert genre_availability(tracks, {}).artists == 2


def test_a_partial_fetch_is_reported_as_partial():
    tracks = [T("t1", "a1"), T("t2", "a2"), T("t3", "a3")]
    a = genre_availability(tracks, {"a1": ["rock"], "a2": []})

    assert (a.artists, a.fetched, a.tagged) == (3, 2, 1)
    assert a.usable  # one real tag is enough to have something to filter on


def test_blank_and_whitespace_tags_do_not_count_as_tagged():
    a = genre_availability([T("t1", "a1")], {"a1": ["", "   "]})
    assert a.tagged == 0
    assert not a.usable


def test_tracks_with_no_artist_ids_are_ignored():
    assert genre_availability([T("t1"), T("t2", "")], {}).artists == 0


# ---------------------------------------------------------------------------
# why this is not derived from genre_index
# ---------------------------------------------------------------------------


def test_the_index_looks_healthy_in_exactly_the_broken_case():
    """Guards the reason genre_availability exists at all. With nothing tagged
    every track falls into Unknown, so genre_index reports one full bucket --
    an option that filters nothing while looking like a working filter."""
    tracks = [T(f"t{i}", "a1") for i in range(5)]
    stats = genre_index(tracks, {"a1": []}, {})

    assert len(stats) == 1
    assert stats[0].is_unknown
    assert stats[0].track_count == 5  # indistinguishable from a real genre

    assert not genre_availability(tracks, {"a1": []}).usable
