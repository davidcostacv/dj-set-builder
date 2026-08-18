from .acousticbrainz import AcousticBrainzSource
from .base import FeatureSource, Resolver, set_manual_features
from .deezer import DeezerSource
from .dsp import DSPSource
from .getsongbpm import ATTRIBUTION_TEXT, ATTRIBUTION_URL, GetSongBPMSource
from .rekordbox import RekordboxXMLSource
from .runner import EnrichmentStats, enrich_tracks


def default_sources(
    getsongbpm_key: str | None,
    getsongbpm_rate: int = 2400,
    *,
    acousticbrainz: bool = True,
    deezer: bool = True,
    dsp: bool = False,
) -> list[FeatureSource]:
    """The registered chain, in priority order.

    GetSongBPM (20) first: its values are human-curated, so a hit is worth more
    than an estimate. AcousticBrainz (25) next — machine analysis, but joined
    exactly on ISRC and carrying key as well as tempo. Deezer (30) last, tempo
    only. Rekordbox (10) would outrank all three if it were ever registered.
    """
    sources: list[FeatureSource] = []
    if getsongbpm_key:
        sources.append(GetSongBPMSource(getsongbpm_key, getsongbpm_rate))
    if acousticbrainz:
        sources.append(AcousticBrainzSource())
    if deezer:
        sources.append(DeezerSource())
    if dsp:
        # Off by default: it downloads a preview and spends ~2.5s of CPU per
        # track, so it changes what a run costs rather than just what it finds.
        # It is the only source whose coverage does not decay with release
        # date, which is why it exists at all — see djset.enrichment.dsp.
        sources.append(DSPSource())
    return sources


__all__ = [
    "ATTRIBUTION_TEXT",
    "ATTRIBUTION_URL",
    "AcousticBrainzSource",
    "DSPSource",
    "DeezerSource",
    "EnrichmentStats",
    "FeatureSource",
    "GetSongBPMSource",
    "RekordboxXMLSource",
    "Resolver",
    "default_sources",
    "enrich_tracks",
    "set_manual_features",
]
