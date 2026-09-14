"""Telegram front end — the phone equivalent of the browser UI.

Same shape as :mod:`djset.web.app`: a thin translation layer over the engine
that already exists and was already tested. No sequencing, filtering, or
Spotify logic lives here — everything below is a command parsed into a call
against ``djset.spotify`` / ``djset.filtering`` / ``djset.sequencing`` /
``djset.export`` / ``djset.db``, exactly as the web routes do.

This is a single-user bot. Only ``TELEGRAM_OWNER_ID`` may issue commands —
anyone else's messages are refused, because a command here can create or
delete playlists in the one Spotify account this app is authorized against.
It never runs the interactive PKCE login itself (that opens a browser and
blocks for up to three minutes, which would wedge the bot process); run
``djset login`` once on the machine hosting the bot first.
"""

from __future__ import annotations

import functools
import logging
import re
from typing import Any, Awaitable, Callable

from .. import db
from ..config import ConfigError, load_config, project_root
from ..export import ExportError, describe_set, export_to_spotify
from ..filtering import filter_tracks
from ..models import Track
from ..sequencing import DEFAULT_TOLERANCE, SequenceMode, SequenceOptions, build_set
from ..spotify.auth import SpotifyAuth
from ..spotify.client import SpotifyClient
from ..spotify.sync import sync_artists, sync_playlists
from ..web.jobs import JobRunner, JobState

log = logging.getLogger(__name__)

MODES = {"bpm": SequenceMode.BPM, "key": SequenceMode.KEY, "bpm+key": SequenceMode.BPM_KEY,
         "bpmkey": SequenceMode.BPM_KEY, "both": SequenceMode.BPM_KEY}
DEFAULT_COUNT = 20
MAX_COUNT = 300

_PLAYLIST_ID_RE = re.compile(r"playlist[/:]([A-Za-z0-9]+)")

runner = JobRunner()


# ---------------------------------------------------------------------------
# environment / access control
# ---------------------------------------------------------------------------


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(project_root() / ".env")


def _owner_id() -> int | None:
    import os

    raw = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    return int(raw) if raw.isdigit() else None


def restricted(handler: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """Refuse anyone who is not the configured owner.

    Checked per-update rather than once at startup: Telegram bots are public
    endpoints by nature (anyone who finds the username can message them), and
    the owner id is the only thing standing between a stranger and this
    account's Spotify library.
    """

    @functools.wraps(handler)
    async def wrapper(update, context):
        owner = _owner_id()
        user = update.effective_user
        if owner is None or user is None or user.id != owner:
            log.warning("rejected update from uid=%s", user.id if user else None)
            if update.effective_message:
                await update.effective_message.reply_text("This bot is private.")
            return
        return await handler(update, context)

    return wrapper


def _client_or_reason() -> tuple[SpotifyClient | None, str | None]:
    """A ready client, or a human-readable reason there isn't one.

    Never calls the interactive login: a saved refresh token must already
    exist (from `djset login` run once, interactively, on this machine).
    """
    try:
        cfg = load_config()
    except ConfigError as exc:
        return None, str(exc)
    auth = SpotifyAuth(cfg)
    if not auth.has_saved_login():
        return None, (
            "No saved Spotify login on this machine yet. Run `djset login` "
            "once in a terminal where this bot runs, then try again."
        )
    return SpotifyClient(auth), None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _playlist_rows(conn) -> list[dict[str, Any]]:
    return [
        {"id": r["spotify_id"], "name": r["name"] or "(untitled)", "tracks": r["track_count"] or 0}
        for r in db.cached_playlists(conn)
    ]


def _playlist_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["No playlists cached yet. Run /sync first."]
    lines = ["Your playlists:"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['name']} — {r['tracks']} tracks")
    lines.append("\nUse /mix <number> [count] [bpm|key|bpm+key] to build a set from one.")
    lines.append("Use /mix all [count] to draw from your whole library.")
    return lines


def _format_playlists(rows: list[dict[str, Any]]) -> str:
    return "\n".join(_playlist_lines(rows))


# Telegram's sendMessage rejects the whole message past this length rather
# than truncating it, and a library of a few hundred playlists (real
# libraries in this app's own README run to 274) produces a listing longer
# than that. Chunking on line boundaries keeps each playlist's line intact.
TELEGRAM_MESSAGE_LIMIT = 4096


def _chunk_lines(lines: list[str], limit: int = TELEGRAM_MESSAGE_LIMIT - 100) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        if current and length + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_job(state: JobState) -> str:
    if not state.kind:
        return "No job has run yet. Try /sync or /enrich."
    if state.running:
        pct = f" ({state.as_dict()['percent']:.0f}%)" if state.total else ""
        return f"{state.kind} running: {state.done}/{state.total}{pct}\n{state.label}"
    if state.error:
        return f"{state.kind} failed: {state.error}"
    if state.cancelled:
        return f"{state.kind} cancelled. {state.summary or ''}".strip()
    return f"{state.kind} finished: {state.summary or 'done'}"


def _resolve_source(rows: list[dict[str, Any]], token: str) -> tuple[str | None, str | None, str | None]:
    """``(playlist_id, name, error)`` for the first /mix argument."""
    if token.lower() == "all":
        return None, None, None
    if not token.isdigit() or not (1 <= int(token) <= len(rows)):
        return None, None, (
            f"“{token}” is not a valid playlist number. Run /playlists to see the list."
        )
    row = rows[int(token) - 1]
    return row["id"], row["name"], None


def _find_playlist(rows: list[dict[str, Any]], needle: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """A single match for /delete, or the list of ambiguous candidates."""
    m = _PLAYLIST_ID_RE.search(needle)
    if m:
        pid = m.group(1)
        for r in rows:
            if r["id"] == pid:
                return r, []
        return None, []

    needle_lc = needle.strip().lower()
    exact = [r for r in rows if r["name"].lower() == needle_lc]
    if len(exact) == 1:
        return exact[0], []
    contains = [r for r in rows if needle_lc in r["name"].lower()]
    if len(contains) == 1:
        return contains[0], []
    return None, contains


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


@restricted
async def cmd_start(update, context) -> None:
    text = (
        "djset — mix your Spotify playlists in key, from Telegram.\n\n"
        "/playlists — list your synced playlists\n"
        "/sync — pull playlists from Spotify into the local cache\n"
        "/enrich — fill in BPM/key data (needed before mixing)\n"
        "/status — progress of a running /sync or /enrich\n"
        "/mix <#|all> [count] [bpm|key|bpm+key] — build a set and save it to Spotify\n"
        "/delete <name or link> — remove a playlist from your account\n"
        "/whoami — check the Spotify login\n"
    )
    await update.effective_message.reply_text(text)


@restricted
async def cmd_whoami(update, context) -> None:
    client, reason = _client_or_reason()
    if reason:
        await update.effective_message.reply_text(reason)
        return

    def work() -> str:
        me = client.me()
        return me.get("display_name") or me.get("id") or "unknown"

    try:
        name = await _to_thread(work)
    except Exception as exc:  # the saved token exists but no longer works
        await update.effective_message.reply_text(f"Saved login is not usable: {exc}")
        return
    await update.effective_message.reply_text(f"Logged in as {name}.")


@restricted
async def cmd_playlists(update, context) -> None:
    def work() -> list[str]:
        with db.session() as conn:
            return _chunk_lines(_playlist_lines(_playlist_rows(conn)))

    for chunk in await _to_thread(work):
        await update.effective_message.reply_text(chunk)


@restricted
async def cmd_sync(update, context) -> None:
    client, reason = _client_or_reason()
    if reason:
        await update.effective_message.reply_text(reason)
        return
    full = bool(context.args) and context.args[0].lower() == "full"

    def job(progress, cancel) -> str:
        with db.session() as conn:
            seen = [0]

            def note(message: str) -> None:
                seen[0] += 1
                progress(seen[0], 0, str(message)[:120])

            result = sync_playlists(conn, client, progress=note)
            if full:
                sync_artists(conn, client, progress=note)
        unreadable = f", {len(result.unreadable)} unreadable" if result.unreadable else ""
        return f"{len(result.counts)} source(s), {result.tracks_seen} rows, {result.unchanged} unchanged{unreadable}"

    started, state = runner.start("sync", job)
    if not started:
        await update.effective_message.reply_text(f"A {state.kind} job is already running. Use /status.")
        return
    await update.effective_message.reply_text("Sync started. Use /status to check progress.")


@restricted
async def cmd_enrich(update, context) -> None:
    from ..enrichment import Resolver, default_sources, enrich_tracks

    try:
        cfg = load_config(require_getsongbpm=False)
    except ConfigError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    sample = None
    for arg in context.args or ():
        if arg.isdigit():
            sample = int(arg)

    sources = default_sources(cfg.getsongbpm_api_key, cfg.getsongbpm_rate_per_hour)
    if not sources:
        await update.effective_message.reply_text(
            "No enrichment sources available — set GETSONGBPM_API_KEY in .env."
        )
        return

    def job(progress, cancel) -> str:
        with db.session() as conn:
            tracks = db.sample_tracks(conn, sample) if sample else None
            stats = enrich_tracks(
                conn, Resolver(sources), tracks, cancel=cancel, progress=progress, retry_misses=True
            )
        return (
            f"considered={stats.considered} resolved={stats.resolved} "
            f"missed={stats.missed} cached={stats.already_cached}"
        )

    started, state = runner.start("enrich", job)
    if not started:
        await update.effective_message.reply_text(f"A {state.kind} job is already running. Use /status.")
        return
    await update.effective_message.reply_text(
        "Enrichment started — this can take a long time for a big library. Use /status to check progress."
    )


@restricted
async def cmd_status(update, context) -> None:
    await update.effective_message.reply_text(_format_job(runner.state))


@restricted
async def cmd_stop(update, context) -> None:
    stopped = runner.cancel()
    await update.effective_message.reply_text(
        "Cancelling…" if stopped else "Nothing is running."
    )


@restricted
async def cmd_mix(update, context) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /mix <#|all> [count] [bpm|key|bpm+key]\nRun /playlists to see the numbers."
        )
        return
    client, reason = _client_or_reason()
    if reason:
        await update.effective_message.reply_text(reason)
        return

    count = DEFAULT_COUNT
    mode = SequenceMode.BPM_KEY
    if len(context.args) > 1 and context.args[1].isdigit():
        count = max(1, min(MAX_COUNT, int(context.args[1])))
    if len(context.args) > 2 and context.args[2].lower() in MODES:
        mode = MODES[context.args[2].lower()]

    def job() -> str:
        with db.session() as conn:
            rows = _playlist_rows(conn)
            playlist_id, name, error = _resolve_source(rows, context.args[0])
            if error:
                return error

            pool: list[Track] = db.all_tracks(conn) if playlist_id is None else db.tracks_in_playlists(conn, [playlist_id])
            if not pool:
                return "That source has no tracks locally. Run /sync first."

            features = db.all_features(conn)
            eligible = filter_tracks(pool, None, {}, {})
            opts = SequenceOptions(mode=mode, tolerance=DEFAULT_TOLERANCE, target_tracks=count)
            result = build_set(eligible, features, opts, eligible_before_filter=len(pool))
            if not result.tracks:
                return f"Could not build a set: {result.explain()}\nHave you run /enrich yet?"

            label = "your library" if playlist_id is None else name
            playlist_name = f"{label} · {mode.value.upper()} · {len(result.tracks)} tracks"
            description = describe_set(
                mode, len(result.tracks), tolerance=DEFAULT_TOLERANCE,
                carried=result.carried, sources=[name] if name else [],
            )
            try:
                export = export_to_spotify(
                    conn, client, playlist_name, result.tracks, description=description
                )
            except ExportError as exc:
                return f"Export failed: {exc}"
            return f"{export.message}\n{export.url}"

    await update.effective_message.reply_text(await _to_thread(job))


@restricted
async def cmd_delete(update, context) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /delete <playlist name or link>")
        return
    needle = " ".join(context.args)

    def work() -> tuple[str, Any]:
        with db.session() as conn:
            rows = _playlist_rows(conn)
        match, candidates = _find_playlist(rows, needle)
        return match, candidates

    match, candidates = await _to_thread(work)
    if match is None and not candidates:
        await update.effective_message.reply_text(
            "No cached playlist matches that. Run /playlists or /sync first."
        )
        return
    if match is None:
        listed = "\n".join(f"- {c['name']}" for c in candidates[:10])
        await update.effective_message.reply_text(
            f"That matches more than one playlist:\n{listed}\n\nBe more specific, or paste the playlist link."
        )
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, delete it", callback_data=f"delconfirm:{match['id']}"),
        InlineKeyboardButton("Cancel", callback_data="delcancel"),
    ]])
    await update.effective_message.reply_text(
        f"Delete “{match['name']}” ({match['tracks']} tracks)? "
        "This removes it from your Spotify account and cannot be undone.",
        reply_markup=keyboard,
    )


@restricted
async def cmd_delete_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "delcancel":
        await query.edit_message_text("Cancelled.")
        return

    playlist_id = query.data.split(":", 1)[1]
    client, reason = _client_or_reason()
    if reason:
        await query.edit_message_text(reason)
        return

    def work() -> str:
        with db.session() as conn:
            rows = {r["id"]: r["name"] for r in _playlist_rows(conn)}
            name = rows.get(playlist_id, playlist_id)
            client.unfollow_playlist(playlist_id)
            db.delete_playlist_cache(conn, playlist_id)
        return name

    try:
        name = await _to_thread(work)
    except Exception as exc:
        await query.edit_message_text(f"Could not delete it: {exc}")
        return
    await query.edit_message_text(f"Deleted “{name}”.")


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------


async def _to_thread(fn: Callable[[], Any]) -> Any:
    import asyncio

    return await asyncio.to_thread(fn)


def run_bot() -> int:
    import os

    from telegram import Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler

    _load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set. Add it to .env (see .env.example).")
        return 1
    if _owner_id() is None:
        print(
            "TELEGRAM_OWNER_ID is not set. Add your numeric Telegram user id to "
            ".env — message @userinfobot to find it. Without this the bot would "
            "accept commands from anyone who finds it."
        )
        return 1

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("playlists", cmd_playlists))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("enrich", cmd_enrich))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("mix", cmd_mix))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CallbackQueryHandler(cmd_delete_callback, pattern=r"^del(confirm|cancel)"))

    log.info("djset telegram bot starting (owner id %s)", _owner_id())
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0
