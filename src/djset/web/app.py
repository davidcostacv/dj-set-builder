"""HTTP layer over the existing engine — step W1.

Deliberately thin. Every route below is a translation of an argument list into
a call that already exists and was already tested; no sequencing, filtering or
Spotify logic lives here. That is the whole reason the move to a browser is
affordable, and it stops being true the moment business logic leaks into a
route handler.

Local-only for now: it binds to loopback and reuses the desktop app's saved
token, so the OAuth redirect that already works keeps working. Hosting, and
the non-loopback redirect it needs, come later (W5/W6).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import db
from ..config import ConfigError, db_path, load_config
from ..enrichment import Resolver, default_sources, enrich_tracks
from ..export import (
    ExportError,
    describe_set,
    export_to_spotify,
    update_playlist_order,
)
from ..filtering import (
    dedupe_recordings,
    filter_tracks,
    genre_availability,
    genre_index,
    sequenceable,
    summarize,
)
from ..models import Track
from ..sequencing import (
    DEFAULT_TOLERANCE,
    MAX_TOLERANCE,
    MIN_TOLERANCE,
    SequenceMode,
    SequenceOptions,
    build_set,
)
from ..spotify.auth import SpotifyAuth
from ..spotify.client import SpotifyClient
from ..spotify.sync import sync_artists, sync_playlists
from .jobs import runner
from .oauth import callback_uri, pending, registration_hint

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="djset", docs_url="/api/docs", redoc_url=None)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


class Library:
    """The library, read once and held.

    The desktop app learned this the hard way: querying per interaction cost
    217ms a click and contended with a running enrichment for the write lock.
    A browser makes that worse, not better — every pane is a request.
    """

    def __init__(self) -> None:
        self.loaded = False
        self.playlists: list[dict[str, Any]] = []
        self.members: dict[str, list[str]] = {}
        self.by_id: dict[str, Track] = {}
        self.features: dict[str, Any] = {}
        self.artist_genres: dict[str, list[str]] = {}
        self.aliases: dict[str, str] = {}

    def load(self) -> None:
        with db.session() as conn:
            self.playlists = [dict(r) for r in db.cached_playlists(conn)]
            self.members = db.all_playlist_members(conn)
            self.by_id = {t.spotify_id: t for t in db.all_tracks(conn)}
            self.features = db.all_features(conn)
            self.artist_genres = db.artist_genres(conn)
            self.aliases = db.genre_aliases(conn)
        self.loaded = True
        log.info(
            "library loaded: %d tracks, %d playlists, %d with features",
            len(self.by_id), len(self.playlists), len(self.features),
        )

    def ensure(self) -> None:
        if not self.loaded:
            self.load()

    def pool(self, source_ids: list[str], picked: list[str] | None = None) -> list[Track]:
        """Union of the given sources, deduplicated, order preserved.

        A track in two selected playlists is placed once. The desktop app
        shipped a bug where the pool silently included a pre-selected playlist;
        here the caller states its sources explicitly and gets exactly those.
        """
        self.ensure()
        seen: set[str] = set()
        out: list[Track] = []
        for pid in source_ids:
            for sid in self.members.get(pid, ()):
                if sid in seen:
                    continue
                seen.add(sid)
                if (track := self.by_id.get(sid)) is not None:
                    out.append(track)
        if picked:
            wanted = set(picked)
            out = [t for t in out if t.spotify_id in wanted]
        return out


library = Library()
runner.on_finished = library.load


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


class SelectionIn(BaseModel):
    sources: list[str] = Field(default_factory=list)
    genres: list[str] | None = None  # None and [] both mean NO FILTER
    picked: list[str] | None = None


class GenerateIn(SelectionIn):
    """Bounds are declared, not clamped.

    These used to be bare floats and ints, and out-of-range values were
    silently absorbed: `target_value: 0` fell through to the default of 20
    because zero is falsy, `-5` became 1 via `max(1, n)`, and `tolerance: 9.9`
    was quietly clamped to 0.12. Nothing broke, but a caller got no signal that
    what it asked for was not what it got — and the CLI *refuses* exactly these
    values with a named error, so the two surfaces of one app disagreed about
    what is valid. Declaring the bounds here makes FastAPI answer 422 and name
    the field.
    """

    mode: SequenceMode = SequenceMode.BPM_KEY
    tolerance: float = Field(DEFAULT_TOLERANCE, ge=MIN_TOLERANCE, le=MAX_TOLERANCE)
    half_double: bool = True
    energy_boost: bool = False
    target_kind: Literal["tracks", "minutes", "all"] = "tracks"
    target_value: int = Field(20, ge=1, le=10_000)
    start_track_id: str | None = None


class ExportIn(BaseModel):
    name: str
    uris: list[str]
    # How the set was built, so the description can say. These describe the
    # *generated* set, not whatever the controls read at save time — the two
    # differ the moment someone changes a radio button and then saves.
    mode: SequenceMode = SequenceMode.BPM_KEY
    tolerance: float = Field(DEFAULT_TOLERANCE, ge=MIN_TOLERANCE, le=MAX_TOLERANCE)
    half_double: bool = True
    energy_boost: bool = False
    edited: bool = False
    # Playlist ids the set was drawn from. Ids rather than names: the names
    # are resolved here from the library, so the description cannot be made
    # to claim a source the page merely believes in.
    sources: list[str] = Field(default_factory=list)
    # An explicit description wins; None means compose one from the above.
    description: str | None = None
    public: bool = False


def _track_json(t: Track, features: dict[str, Any]) -> dict[str, Any]:
    f = features.get(t.spotify_id)
    return {
        "id": t.spotify_id,
        "uri": t.uri,
        "title": t.title,
        "artist": t.artist,
        "duration_ms": t.duration_ms,
        "bpm": f.bpm if f else None,
        "key": f.key_camelot if f else None,
        "source": f.source if f else None,
        # Where the *key* came from, which is not always where the row came
        # from — a Deezer row can carry a key the analyser supplied — and
        # whether that means it was measured rather than looked up.
        "key_source": f.key_source if f else None,
        "key_estimated": bool(f and f.key_camelot and f.key_is_estimated),
    }


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    library.ensure()
    usable = sum(
        1 for f in library.features.values()
        if f.bpm is not None and f.key_camelot is not None
    )
    return {
        "ok": True,
        "database": str(db_path()),
        "tracks": len(library.by_id),
        "playlists": len(library.playlists),
        "sequenceable": usable,
    }


@app.post("/api/reload")
def reload_library() -> dict[str, Any]:
    """Re-read the database — after a sync or an enrich has moved it on."""
    library.load()
    return health()


@app.get("/api/sources")
def sources() -> list[dict[str, Any]]:
    library.ensure()
    return [
        {
            "id": p["spotify_id"],
            "name": p["name"] or "(untitled)",
            "tracks": p["track_count"] or 0,
        }
        for p in library.playlists
    ]


@app.post("/api/selection")
def selection(body: SelectionIn) -> dict[str, Any]:
    """Everything the panes need for one selection, in a single round trip.

    Genres, the eligible counter and the availability notice all derive from
    the same pool; asking for them separately would recompute it three times.
    """
    library.ensure()
    pool = library.pool(body.sources, body.picked)
    chosen = set(body.genres) if body.genres else None

    stats = genre_index(pool, library.artist_genres, library.aliases, library.features)
    avail = genre_availability(pool, library.artist_genres)
    summary = summarize(
        pool, chosen, library.artist_genres, library.aliases, library.features
    )
    # The honest ceiling for a set from this selection. Not the same as
    # `eligible.enriched`: that counts tracks with BPM and key, and the
    # sequencer then collapses different pressings of one recording, so a
    # playlist showing 1,049 ready tracks can only ever yield 904. Someone
    # typing the playlist's own size into the target deserves to see the real
    # number rather than discover it in a short set.
    filtered = filter_tracks(pool, chosen, library.artist_genres, library.aliases)
    ready = sequenceable(filtered, library.features)
    unique = dedupe_recordings(ready)
    max_set = len(unique)

    # Where the tracks went. "I picked 1,293 and got 904" is a fair question,
    # and the honest answer has three parts that the app already knows and was
    # making the user ask for: no key, duplicate recordings, and rows that were
    # never synced. Counted here so the UI can show it without a second call.
    bpm_only = sum(
        1
        for t in filtered
        if (f := library.features.get(t.spotify_id)) is not None
        and f.bpm is not None
        and f.key_camelot is None
    )
    return {
        "pool": len(pool),
        "max_set": max_set,
        "breakdown": {
            "eligible": len(filtered),
            "no_key": len(filtered) - len(ready),
            "bpm_only": bpm_only,
            "duplicates": len(ready) - len(unique),
            "sequenceable": max_set,
        },
        "genres": [
            {"name": s.name, "tracks": s.track_count, "enriched": s.enriched_count}
            for s in stats
        ],
        "availability": {
            "usable": avail.usable,
            "headline": avail.headline,
            "detail": avail.detail,
        },
        "eligible": {
            "total": summary.total,
            "enriched": summary.enriched,
            "filtered": summary.filtered,
            "label": summary.label,
        },
    }


@app.post("/api/tracks")
def tracks(body: SelectionIn) -> list[dict[str, Any]]:
    """The pool itself, for the hand-pick view."""
    library.ensure()
    return [_track_json(t, library.features) for t in library.pool(body.sources)]


@app.post("/api/generate")
def generate(body: GenerateIn) -> dict[str, Any]:
    """Sequence a set. Creates nothing — saving is a separate, explicit call.

    Splitting them is the one place this deliberately differs from the desktop
    app, where Generate also wrote the playlist and there was no way to look
    first. In a browser the set is on screen either way, so making the write a
    second click costs nothing and stops an experiment from leaving a playlist
    behind in the account.
    """
    library.ensure()
    if not body.sources:
        raise HTTPException(400, "No sources selected.")

    # Distinguish "you named none" from "none of the ones you named exist".
    # Both used to say "No sources selected", which is wrong in the second
    # case and hides a typo'd or stale playlist id behind a message about
    # something the caller did not do.
    unknown = [pid for pid in body.sources if pid not in library.members]
    pool = library.pool(body.sources, body.picked)
    if not pool:
        if unknown:
            shown = ", ".join(unknown[:3])
            more = f" (+{len(unknown) - 3} more)" if len(unknown) > 3 else ""
            raise HTTPException(
                400,
                f"None of the selected sources are in the library: {shown}{more}. "
                "Run Sync if they are new.",
            )
        if body.picked:
            raise HTTPException(
                400, "The hand-picked subset matched nothing in those sources."
            )
        raise HTTPException(400, "Those sources contain no tracks.")

    chosen = set(body.genres) if body.genres else None
    eligible = filter_tracks(pool, chosen, library.artist_genres, library.aliases)

    opts = SequenceOptions(
        mode=body.mode,
        tolerance=body.tolerance,
        half_double=body.half_double,
        energy_boost=body.energy_boost,
        use_all=body.target_kind == "all",
        target_tracks=body.target_value if body.target_kind == "tracks" else None,
        target_minutes=float(body.target_value) if body.target_kind == "minutes" else None,
        start_track_id=body.start_track_id,
    )
    result = build_set(eligible, library.features, opts, eligible_before_filter=len(pool))

    rows = []
    for i, t in enumerate(result.tracks):
        row = _track_json(t, library.features)
        tr = result.transitions[i] if i < len(result.transitions) else None
        row["transition"] = tr.label if tr and tr.quality > 0 else (
            "incompatible" if tr else None
        )
        row["quality"] = tr.quality if tr else None
        rows.append(row)

    total_ms = sum(t.duration_ms or 0 for t in result.tracks)
    qualities = [tr.quality for tr in result.transitions if tr]
    return {
        "tracks": rows,
        "count": len(result.tracks),
        "requested": result.requested,
        "pool_size": result.pool_size,
        "reached_target": result.reached_target,
        "compromises": result.compromises,
        "duration_ms": total_ms,
        "average_quality": (sum(qualities) / len(qualities)) if qualities else 0.0,
        "explain": result.explain(),
        "limiting_factor": result.limiting_factor.value,
    }


@app.post("/api/export")
def export(body: ExportIn) -> dict[str, Any]:
    """Create the playlist. The only route that writes to the account."""
    if not body.uris:
        raise HTTPException(400, "Nothing to save.")
    library.ensure()
    try:
        client = SpotifyClient(SpotifyAuth(load_config()))
        with db.session() as conn:
            tracks_out = [library.by_id[u.rsplit(":", 1)[-1]] for u in body.uris
                          if u.rsplit(":", 1)[-1] in library.by_id]
            by_id = {p["spotify_id"]: p["name"] for p in library.playlists}
            description = body.description or describe_set(
                body.mode,
                len(tracks_out),
                tolerance=body.tolerance,
                half_double=body.half_double,
                energy_boost=body.energy_boost,
                edited=body.edited,
                sources=[by_id[s] for s in body.sources if by_id.get(s)],
            )
            result = export_to_spotify(
                conn, client, body.name, tracks_out,
                description=description, public=body.public,
            )
    except ConfigError as exc:
        raise HTTPException(500, str(exc))
    except ExportError as exc:
        raise HTTPException(502, str(exc))
    return {
        "playlist_id": result.playlist_id,
        "url": result.url,
        "message": result.message,
        "name": result.name,
        # Whether an existing playlist was returned instead of a new one. The
        # caller needs this: reusing silently looks like the save was ignored.
        "reused": result.reused,
    }


@app.post("/api/reorder/{playlist_id}")
def reorder(playlist_id: str, body: ExportIn) -> dict[str, Any]:
    library.ensure()
    try:
        client = SpotifyClient(SpotifyAuth(load_config()))
        with db.session() as conn:
            tracks_out = [library.by_id[u.rsplit(":", 1)[-1]] for u in body.uris
                          if u.rsplit(":", 1)[-1] in library.by_id]
            update_playlist_order(conn, client, playlist_id, tracks_out)
    except ExportError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True, "tracks": len(body.uris)}


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------


def _auth() -> SpotifyAuth:
    return SpotifyAuth(load_config())


@app.get("/api/auth/status")
def auth_status() -> dict[str, Any]:
    """Whether a saved login exists, and who it belongs to.

    Deliberately does not refresh: this is called on every page load, and a
    status check that silently spends a network round trip — or opens a browser
    on the *server* when the token has expired — would be a trap.
    """
    try:
        auth = _auth()
    except ConfigError as exc:
        return {"configured": False, "authorized": False, "detail": str(exc)}

    if not auth.has_saved_login():
        return {"configured": True, "authorized": False, "user": None}
    try:
        me = SpotifyClient(auth).me()
        return {
            "configured": True,
            "authorized": True,
            "user": me.get("display_name") or me.get("id"),
        }
    except Exception as exc:
        # A saved token that no longer works is not the same as no token, and
        # the page should say so rather than showing a logged-in state that
        # fails on the first real request.
        return {"configured": True, "authorized": False, "user": None,
                "detail": f"Saved login is not usable: {exc}"}


@app.get("/auth/login")
def auth_login(request: Request) -> RedirectResponse:
    """Send the browser to Spotify. The verifier stays here."""
    try:
        auth = _auth()
    except ConfigError as exc:
        raise HTTPException(500, str(exc))

    redirect_uri = callback_uri(str(request.base_url))
    url, verifier, state = auth.authorize_url(redirect_uri)
    pending.start(state, verifier, redirect_uri)
    log.info("authorization started; %s", registration_hint(redirect_uri))
    return RedirectResponse(url, status_code=307)


@app.get("/auth/callback")
def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Spotify comes back here. Validate, exchange, then return to the app.

    Every failure redirects to the page with a message rather than rendering an
    error body, because this URL is in the address bar: leaving someone on a
    dead-end page containing an authorization code is worse than sending them
    back to something they can retry from.
    """
    if error:
        return RedirectResponse(f"/?auth_error={error}", status_code=303)
    if not code or not state:
        return RedirectResponse("/?auth_error=missing_code", status_code=303)

    flow = pending.claim(state)
    if flow is None:
        # Unknown, expired, or already redeemed — all the same answer. Saying
        # which would help someone guessing at state values.
        return RedirectResponse("/?auth_error=stale_or_unknown_state", status_code=303)

    try:
        _auth().exchange_code(code, flow.verifier, flow.redirect_uri)
    except Exception as exc:
        log.warning("token exchange failed: %s", exc)
        return RedirectResponse("/?auth_error=exchange_failed", status_code=303)
    return RedirectResponse("/?authorized=1", status_code=303)


@app.post("/api/auth/logout")
def auth_logout() -> dict[str, Any]:
    """Forget the saved login. Spotify is not told; this is local only."""
    try:
        _auth().store.clear()
    except ConfigError as exc:
        raise HTTPException(500, str(exc))
    return {"authorized": False}


# ---------------------------------------------------------------------------
# long jobs
# ---------------------------------------------------------------------------


class EnrichIn(BaseModel):
    retry_misses: bool = True
    sample: int | None = None
    acousticbrainz: bool = True
    deezer: bool = True
    dsp: bool = False


@app.get("/api/job")
def job_state() -> dict[str, Any]:
    return runner.state.as_dict()


@app.post("/api/job/cancel")
def job_cancel() -> dict[str, Any]:
    stopped = runner.cancel()
    return {"cancelling": stopped, "job": runner.state.as_dict()}


@app.post("/api/job/sync")
def job_sync() -> dict[str, Any]:
    def work(progress, cancel) -> str:
        with db.session() as conn:
            client = SpotifyClient(SpotifyAuth(load_config()))
            seen = [0]

            def note(message: str) -> None:
                # sync reports by line rather than by count; show the line and
                # let the counter climb so the UI still looks alive.
                seen[0] += 1
                progress(seen[0], 0, str(message)[:90])

            result = sync_playlists(conn, client, progress=note)
            sync_artists(conn, client, progress=note)
        unreadable = f", {len(result.unreadable)} unreadable" if result.unreadable else ""
        return (
            f"{len(result.counts)} source(s), {result.tracks_seen} rows, "
            f"{result.unchanged} unchanged{unreadable}"
        )

    started, state = runner.start("sync", work)
    if not started:
        raise HTTPException(409, f"A {state.kind} job is already running.")
    return state.as_dict()


@app.post("/api/job/enrich")
def job_enrich(body: EnrichIn) -> dict[str, Any]:
    """Fill BPM and key. Long — hours for a full library — and that is why it
    lives here: held by the server it runs to completion instead of dying with
    whatever shell started it."""
    try:
        cfg = load_config(require_getsongbpm=False)
    except ConfigError as exc:
        raise HTTPException(500, str(exc))

    sources = default_sources(
        cfg.getsongbpm_api_key,
        cfg.getsongbpm_rate_per_hour,
        acousticbrainz=body.acousticbrainz,
        deezer=body.deezer,
        dsp=body.dsp,
    )
    if not sources:
        raise HTTPException(400, "No sources enabled — nothing to ask.")

    def work(progress, cancel) -> str:
        with db.session() as conn:
            tracks = db.sample_tracks(conn, body.sample) if body.sample else None
            stats = enrich_tracks(
                conn,
                Resolver(sources),
                tracks,
                cancel=cancel,
                progress=progress,
                retry_misses=body.retry_misses,
            )
        return (
            f"considered={stats.considered} resolved={stats.resolved} "
            f"missed={stats.missed} cached={stats.already_cached}"
            + (" (cancelled)" if stats.cancelled else "")
        )

    started, state = runner.start("enrich", work)
    if not started:
        raise HTTPException(409, f"A {state.kind} job is already running.")
    return state.as_dict()


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

class _RevalidatingStatic(StaticFiles):
    """Static files the browser must re-check before reusing.

    Without this the page keeps running whatever JS the browser cached, and
    since the assets are unversioned nothing ever tells it otherwise. The
    symptom is the worst kind: an update that is definitely deployed, verified
    on the server, and still not happening in the tab you are looking at.

    `no-cache` is not `no-store` — the file is still cached, the browser just
    has to ask whether it changed. Starlette answers with a 304 from the ETag
    when it has not, so the cost is one conditional request per asset.
    """

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _RevalidatingStatic(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    # Same reasoning as the assets: the page that references them must not be
    # served from cache either, or it will keep pointing at the old ones.
    return FileResponse(
        STATIC / "index.html", headers={"Cache-Control": "no-cache"}
    )
