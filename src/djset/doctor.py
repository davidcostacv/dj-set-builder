"""Endpoint probe.

The build brief describes a 2026 API surface that differs sharply from most
documentation. Rather than silently working around a mismatch, this probes each
endpoint and reports exactly what the API actually does, so discrepancies get
raised instead of papered over.
"""

from __future__ import annotations

from dataclasses import dataclass

from .net import HttpError, request
from .spotify.client import API
from .spotify.auth import SpotifyAuth


@dataclass
class Probe:
    label: str
    expectation: str
    outcome: str
    matches: bool


def _probe(
    auth: SpotifyAuth, method: str, path: str, expectation: str, expect_ok: bool
) -> Probe:
    try:
        resp = request(
            method,
            f"{API}{path}",
            headers=auth.auth_header(),
            on_unauthorized=auth.refreshed_auth_header,
        )
        status = resp.status_code
    except HttpError as exc:
        status = exc.status
    except Exception as exc:  # transport, etc.
        return Probe(f"{method} {path}", expectation, f"error: {exc}", False)

    ok = 200 <= status < 300
    matches = ok if expect_ok else not ok
    return Probe(f"{method} {path}", expectation, f"HTTP {status}", matches)


def run_doctor(auth: SpotifyAuth) -> list[Probe]:
    probes: list[Probe] = []

    probes.append(_probe(auth, "GET", "/me", "works", True))

    # Find a real playlist and track to probe with.
    playlist_id: str | None = None
    track_id: str | None = None
    try:
        page = request(
            "GET",
            f"{API}/me/playlists",
            headers=auth.auth_header(),
            params={"limit": 1},
            on_unauthorized=auth.refreshed_auth_header,
        ).json()
        items = page.get("items") or []
        if items:
            playlist_id = items[0].get("id")
        probes.append(Probe("GET /me/playlists", "works", "HTTP 200", True))
    except Exception as exc:
        probes.append(Probe("GET /me/playlists", "works", f"error: {exc}", False))

    if playlist_id:
        probes.append(
            _probe(
                auth,
                "GET",
                f"/playlists/{playlist_id}/items",
                "renamed from /tracks in 2026 — should work",
                True,
            )
        )
        probes.append(
            _probe(
                auth,
                "GET",
                f"/playlists/{playlist_id}/tracks",
                "old path — brief says renamed, so this should 404",
                False,
            )
        )
        try:
            page = request(
                "GET",
                f"{API}/playlists/{playlist_id}/items",
                headers=auth.auth_header(),
                params={"limit": 1},
                on_unauthorized=auth.refreshed_auth_header,
            ).json()
            for item in page.get("items") or []:
                t = item.get("track") or item.get("item") or {}
                if t.get("id"):
                    track_id = t["id"]
                    break
        except Exception:
            pass

    probes.append(_probe(auth, "GET", "/me/tracks?limit=1", "works", True))

    if track_id:
        probes.append(_probe(auth, "GET", f"/tracks/{track_id}", "single fetch works", True))
        probes.append(
            _probe(
                auth,
                "GET",
                f"/audio-features/{track_id}",
                "DEAD since Nov 2024 — must fail",
                False,
            )
        )
        probes.append(
            _probe(
                auth,
                "GET",
                f"/tracks?ids={track_id}",
                "batch removed Feb 2026 — should fail",
                False,
            )
        )

    probes.append(
        _probe(auth, "GET", "/recommendations?limit=1", "DEAD — must fail", False)
    )
    probes.append(
        _probe(auth, "GET", "/browse/new-releases?limit=1", "removed Feb 2026", False)
    )

    probes.extend(_field_probes(auth, playlist_id))
    return probes


def _field_probes(auth: SpotifyAuth, playlist_id: str | None) -> list[Probe]:
    """Response-shape checks. Field renames break parsing as surely as a dead
    endpoint does, and they fail silently rather than raising."""
    if not playlist_id:
        return []
    try:
        pl = request(
            "GET",
            f"{API}/playlists/{playlist_id}",
            headers=auth.auth_header(),
            on_unauthorized=auth.refreshed_auth_header,
        ).json()
    except Exception as exc:
        return [Probe("playlist fields", "readable", f"error: {exc}", False)]

    out: list[Probe] = []

    has_items = isinstance(pl.get("items"), dict)
    has_tracks = isinstance(pl.get("tracks"), dict)
    out.append(
        Probe(
            "playlist.items.total (count field)",
            "renamed from .tracks, matching the endpoint rename",
            f"items={'present' if has_items else 'absent'}, "
            f"tracks={'present' if has_tracks else 'absent'}",
            has_items or has_tracks,
        )
    )

    out.append(
        Probe(
            "playlist.followers",
            "brief says `followers` was stripped from responses",
            "absent" if "followers" not in pl else f"still present: {pl['followers']}",
            "followers" not in pl,
        )
    )
    return out


def format_probes(probes: list[Probe]) -> str:
    lines = ["", "  API SURFACE PROBE", "  " + "=" * 62, ""]
    mismatches = 0
    for p in probes:
        mark = "OK  " if p.matches else "DIFF"
        if not p.matches:
            mismatches += 1
        lines.append(f"  [{mark}] {p.label}")
        lines.append(f"         expected: {p.expectation}")
        lines.append(f"         actual:   {p.outcome}")
    lines.append("")
    if mismatches:
        lines.append(
            f"  {mismatches} endpoint(s) behave differently from the build brief. "
            "Report these before building on them."
        )
    else:
        lines.append("  The API matches the build brief exactly.")
    lines.append("")
    return "\n".join(lines)
