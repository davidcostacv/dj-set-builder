import pytest

from djset.enrichment.normalize import (
    artist_variants,
    match_key,
    normalize_artist,
    normalize_title,
    primary_artist,
    similarity,
    title_variants,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Blinding Lights", "blinding lights"),
        ("Blinding Lights (Remix)", "blinding lights"),
        ("Blinding Lights [Extended Mix]", "blinding lights"),
        ("Bad Guy - 2019 Remaster", "bad guy"),
        ("Levitating (feat. DaBaby)", "levitating"),
        ("Levitating feat. DaBaby", "levitating"),
        ("One More Time ft. Romanthony", "one more time"),
        ("Stay With Me (with Sam Smith)", "stay with me"),
        ("Don't Stop Me Now", "dont stop me now"),
        ("Où est le soleil?", "ou est le soleil"),
        ("The Less I Know The Better", "less i know the better"),
        ("A Sky Full of Stars", "sky full of stars"),
        ("  Multiple   Spaces  ", "multiple spaces"),
        ("Song — Radio Edit", "song"),
        ("Café del Mar", "cafe del mar"),
    ],
)
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_unbracketed_with_is_part_of_the_title_not_a_credit():
    # Regression: treating a bare "with" as a featuring marker turned
    # "Stay With Me" into "stay".
    assert normalize_title("Stay With Me") == "stay with me"
    assert normalize_title("Dancing With A Stranger") == "dancing with a stranger"
    assert normalize_title("Stay With Me (with Sam Smith)") == "stay with me"


def test_dash_suffix_only_stripped_when_it_is_metadata():
    # Production metadata: dropped.
    assert normalize_title("Insomnia - Radio Edit") == "insomnia"
    # A real part of the title: kept.
    assert normalize_title("Sunset - Sunrise") == "sunset sunrise"


def test_title_variants_are_ordered_most_specific_first():
    variants = title_variants("Strobe (Extended Mix)")
    assert variants[0] == "strobe extended mix"
    assert variants[-1] == "strobe"
    assert len(variants) == len(set(variants))


def test_title_variants_collapse_when_nothing_to_strip():
    assert title_variants("Strobe") == ["strobe"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Daft Punk", "daft punk"),
        ("Tiësto", "tiesto"),
        ("The Chemical Brothers", "chemical brothers"),
        ("Bad Bunny feat. Drake", "bad bunny"),
        ("Rüfüs Du Sol", "rufus du sol"),
        # Names containing '&' or ',' are single artists and must stay whole.
        # These used to truncate to "above" / "earth", which never matched.
        ("Above & Beyond", "above beyond"),
        ("Earth, Wind & Fire", "earth wind fire"),
        ("Tyler, The Creator", "tyler the creator"),
        ("A$AP Rocky", "asap rocky"),
    ],
)
def test_normalize_artist(raw, expected):
    """Input is ONE artist name, from Spotify's artists array — never a joined
    credit list. See tests/test_primary_artist.py for how that list is split."""
    assert normalize_artist(raw) == expected


def test_primary_artist_keeps_leading_article():
    assert primary_artist("The Weeknd") == "the weeknd"
    assert normalize_artist("The Weeknd") == "weeknd"


def test_artist_variants_dedupe():
    assert artist_variants("Daft Punk") == ["daft punk"]
    assert artist_variants("The Weeknd") == ["the weeknd", "weeknd"]


def test_multi_artist_strings_are_not_split_here():
    """Splitting is the caller's job, via Track.artist_names."""
    assert normalize_artist("Calvin Harris, Dua Lipa") == "calvin harris dua lipa"


def test_match_key_is_stable_across_spellings():
    a = match_key("The Weeknd", "Blinding Lights (Remix)")
    b = match_key("the weeknd", "Blinding Lights [Remix]")
    assert a == b


def test_similarity():
    assert similarity("blinding lights", "blinding lights") == 1.0
    assert similarity("blinding lights", "blinding") == 0.5
    assert similarity("abc", "xyz") == 0.0
    assert similarity("", "anything") == 0.0
