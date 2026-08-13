"""Rekordbox / Mixed In Key XML source (priority 10) — STUB, NOT REGISTERED.

Deliberately unimplemented per the build brief. It is defined here so the
priority ordering can be tested now and so dropping the real implementation in
later is a registration, not a refactor.

When built, it reads a Rekordbox ``collection.xml`` (``AverageBpm``,
``Tonality`` attributes on each ``TRACK`` node) and matches on ISRC first,
normalized artist+title second. Its data comes from analysis of the real audio,
so at priority 10 it automatically supersedes GetSongBPM (priority 20) on
conflict without any other code changing.
"""

from __future__ import annotations

from pathlib import Path

from ..models import AudioFeatures, Track


class RekordboxXMLSource:
    name = "rekordbox"
    priority = 10

    def __init__(self, xml_path: Path) -> None:
        self.xml_path = xml_path

    def lookup(self, track: Track) -> AudioFeatures | None:
        raise NotImplementedError(
            "RekordboxXMLSource is intentionally not implemented yet. "
            "Build it only if the coverage report comes back weak."
        )
