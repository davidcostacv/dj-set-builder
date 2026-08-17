"""Headless CLI. Everything through build-order step 6 is driven from here;
the Qt window does not exist yet.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import db
from .config import ConfigError, app_data_dir, db_path, load_config, log_path
from .crosscheck import SAME_VALUE_TOLERANCE
from .enrichment import (
    ATTRIBUTION_TEXT,
    AcousticBrainzSource,
    DeezerSource,
    GetSongBPMSource,
    Resolver,
    default_sources,
    enrich_tracks,
)
from .logging_setup import setup_logging
from .net import close_client
from .report import build_coverage, coverage_as_json, format_coverage
from .spotify.auth import SpotifyAuth
from .spotify.client import SpotifyClient
from .spotify.sync import list_sources, sync_artists, sync_playlists

log = logging.getLogger("djset")


def _client() -> SpotifyClient:
    return SpotifyClient(SpotifyAuth(load_config()))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    auth = SpotifyAuth(load_config())
    if args.forget:
        auth.store.clear()
        print("Saved login cleared.")
        return 0
    auth.login()
    me = SpotifyClient(auth).me()
    print(f"Logged in as {me.get('display_name') or me.get('id')} ({me.get('id')})")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    me = _client().me()
    print(f"id           : {me.get('id')}")
    print(f"display_name : {me.get('display_name')}")
    print(f"uri          : {me.get('uri')}")
    # country/email/product/explicit_content were stripped from /me; if they
    # show up, the brief has drifted and that is worth knowing.
    unexpected = [k for k in ("country", "email", "product", "explicit_content") if k in me]
    if unexpected:
        print(f"NOTE: /me still returns {unexpected} — the build brief says these were removed.")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    for p in list_sources(_client()):
        print(f"{p.spotify_id:<24} {p.track_count:>5}  {p.name}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    client = _client()
    with db.session() as conn:
        result = sync_playlists(
            conn,
            client,
            playlist_ids=args.playlist or None,
            include_liked=not args.no_liked,
            force=args.force,
            progress=print,
        )
        print(
            f"\nSynced {len(result.counts)} source(s), {result.tracks_seen} rows; "
            f"{result.unchanged} unchanged."
        )
        if result.unreadable:
            print(f"\n{len(result.unreadable)} source(s) could not be read:")
            for name, reason in result.unreadable[:20]:
                print(f"  - {name}: {reason}")
            if len(result.unreadable) > 20:
                print(f"  … and {len(result.unreadable) - 20} more (see the log)")
            print(
                "\n  These are almost certainly playlists you follow but do not own.\n"
                "  Development mode cannot read them; only your own are usable."
            )
        if not args.no_artists:
            sync_artists(conn, client, force=args.force, progress=print)
        print(f"\nLibrary now holds {len(db.all_tracks(conn))} unique tracks.")
    return 0


def cmd_artists(args: argparse.Namespace) -> int:
    """Fetch artist genres only — the genre filter's data source.

    Separate from `sync` because it is the slow half (one request per artist,
    the batch endpoint having been removed) and playlists rarely need re-reading
    at the same time.
    """
    client = _client()
    with db.session() as conn:
        done = sync_artists(conn, client, force=args.force, progress=print)
        tagged = sum(1 for g in db.artist_genres(conn).values() if g)
        total = len(db.artist_genres(conn))
    print(f"\n{done} artist(s) fetched this run.")
    print(f"{tagged}/{total} cached artists carry at least one genre tag.")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    cfg = load_config(require_getsongbpm=not args.no_getsongbpm)
    sources = default_sources(
        None if args.no_getsongbpm else cfg.getsongbpm_api_key,
        cfg.getsongbpm_rate_per_hour,
        acousticbrainz=not args.no_acousticbrainz,
        deezer=not args.no_deezer,
    )
    resolver = Resolver(sources)
    print("Sources: " + ", ".join(f"{s.name}({s.priority})" for s in resolver.sources))

    with db.session() as conn:
        if args.sample:
            tracks = db.sample_tracks(conn, args.sample)
            scope = f"a random sample of {len(tracks)}"
        elif args.limit:
            tracks = db.all_tracks(conn)[: args.limit]
            scope = f"the first {len(tracks)}"
        else:
            tracks = db.all_tracks(conn)
            scope = f"all {len(tracks)}"
        if not tracks:
            print("No tracks in the local DB — run `djset sync` first.")
            return 1

        print(f"Enriching {scope} tracks…")
        print("(Ctrl+C is safe — every result is committed as it lands.)\n")
        try:
            stats = enrich_tracks(
                conn,
                resolver,
                tracks,
                progress=_enrich_progress,
                refresh=args.refresh,
                retry_misses=args.retry_misses,
            )
        except KeyboardInterrupt:
            conn.commit()
            print("\nCancelled. Progress saved — re-run to resume.")
            return 130

        print(
            f"\nconsidered={stats.considered} cached={stats.already_cached} "
            f"resolved={stats.resolved} missed={stats.missed} "
            f"skipped(retry limit)={stats.skipped_exhausted}"
        )
        print(f"\n{ATTRIBUTION_TEXT}")
    return 0


def _enrich_progress(done: int, total: int, label: str) -> None:
    sys.stdout.write(f"\r  {done}/{total}  {label[:60]:<60}")
    sys.stdout.flush()
    if done == total:
        sys.stdout.write("\n")


def cmd_coverage(args: argparse.Namespace) -> int:
    with db.session() as conn:
        cov = build_coverage(conn)
    print(coverage_as_json(cov) if args.json else format_coverage(cov))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """sync + enrich + coverage, in one go — the step-3 checkpoint."""
    rc = cmd_sync(args)
    if rc:
        return rc
    rc = cmd_enrich(args)
    if rc not in (0, 130):
        return rc
    return cmd_coverage(args)


def cmd_generate(args: argparse.Namespace) -> int:
    """Sequence a set and create the playlist — the whole of Generate."""
    from .export import ExportError, export_to_spotify
    from .filtering import filter_tracks, summarize
    from .sequencing import SequenceMode, SequenceOptions, build_set

    mode = SequenceMode(args.mode)
    with db.session() as conn:
        pool = (
            db.tracks_in_playlists(conn, args.playlist)
            if args.playlist
            else db.all_tracks(conn)
        )
        if not pool:
            print("No tracks. Run `djset sync` first.")
            return 1

        if args.track:
            wanted = set(args.track)
            pool = [t for t in pool if t.spotify_id in wanted]
            missing = wanted - {t.spotify_id for t in pool}
            if missing:
                print(f"Not in the local library: {', '.join(sorted(missing))}")
            if not pool:
                print("None of the given track ids are in the selected sources.")
                return 1

        genres = set(args.genre) if args.genre else None
        artist_genres = db.artist_genres(conn)
        aliases = db.genre_aliases(conn)
        features = db.all_features(conn)

        eligible = filter_tracks(pool, genres, artist_genres, aliases)
        summary = summarize(pool, genres, artist_genres, aliases, features)
        print(f"Pool: {len(pool)} tracks. {summary.label}")

        opts = SequenceOptions(
            mode=mode,
            tolerance=args.tolerance,
            half_double=not args.no_half_double,
            energy_boost=args.energy_boost,
            use_all=args.all,
            target_tracks=args.tracks,
            target_minutes=args.minutes,
            start_track_id=args.start_track,
        )
        result = build_set(
            eligible, features, opts, eligible_before_filter=len(pool)
        )
        print(f"\n{result.explain()}\n")
        if not result.tracks:
            return 1

        for i, t in enumerate(result.tracks, 1):
            f = features.get(t.spotify_id)
            bpm = f"{f.bpm:.0f}" if f and f.bpm else "—"
            key = (f.key_camelot if f else None) or "—"
            arrow = ""
            if i <= len(result.transitions):
                arrow = f"   -> {result.transitions[i - 1].label}"
            print(f"  {i:>3}. {bpm:>4} {key:<4} {t.artist[:26]:26} {t.title[:32]:32}{arrow}")

        if args.dry_run:
            print("\n--dry-run: nothing was created in your account.")
            return 0

        name = args.name or _default_name(genres, mode, len(result.tracks))
        try:
            export = export_to_spotify(
                conn,
                _client(),
                name,
                result.tracks,
                description=f"{mode.value} · {len(result.tracks)} tracks · built with djset",
                public=args.public,
                progress=print,
            )
        except ExportError as exc:
            print(f"\nExport failed: {exc}")
            return 1

        print(f"\n{export.message}")
        print(f"\n  {export.url}\n")
    return 0


def cmd_tracks(args: argparse.Namespace) -> int:
    """List track ids, so `generate --track` can be pointed at specific ones."""
    with db.session() as conn:
        pool = (
            db.tracks_in_playlists(conn, args.playlist)
            if args.playlist
            else db.all_tracks(conn)
        )
        features = db.all_features(conn)

    needle = (args.search or "").lower()
    shown = 0
    for t in pool:
        if needle and needle not in f"{t.title} {t.artist}".lower():
            continue
        f = features.get(t.spotify_id)
        if args.usable_only and not (f and f.bpm and f.key_camelot):
            continue
        bpm = f"{f.bpm:.0f}" if f and f.bpm else "—"
        key = (f.key_camelot if f else None) or "—"
        print(f"{t.spotify_id}  {bpm:>4} {key:<4} {t.artist[:26]:26} {t.title[:36]}")
        shown += 1
        if args.limit and shown >= args.limit:
            break
    if not shown:
        print("Nothing matched.")
    return 0


def _default_name(genres, mode, n: int) -> str:
    """Prefilled name, e.g. 'House · BPM+Key · 24 tracks'."""
    parts = []
    if genres:
        parts.append(" / ".join(sorted(g.title() for g in genres)))
    parts.append(mode.value.upper())
    parts.append(f"{n} tracks")
    return " · ".join(parts)


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui import run

    return run()


def cmd_exports(args: argparse.Namespace) -> int:
    with db.session() as conn:
        rows = db.all_exports(conn)
    if not rows:
        print("Nothing created yet.")
        return 0
    for r in rows:
        print(f"{r['created_at']}  {r['playlist_id']:<24} {r['name']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import format_probes, run_doctor

    probes = run_doctor(SpotifyAuth(load_config()))
    print(format_probes(probes))
    return 0 if all(p.matches for p in probes) else 2


def cmd_about(args: argparse.Namespace) -> int:
    print("djset - personal Spotify playlist & DJ set generator")
    print(f"  data dir : {app_data_dir()}")
    print(f"  database : {db_path()}")
    print(f"  log file : {log_path()}")
    print()
    print(f"  {ATTRIBUTION_TEXT}")
    return 0


def cmd_crosscheck(args: argparse.Namespace) -> int:
    from .crosscheck import cross_check

    if args.against == "getsongbpm":
        cfg = load_config(require_getsongbpm=True)
        challenger = GetSongBPMSource(cfg.getsongbpm_api_key, cfg.getsongbpm_rate_per_hour)
    elif args.against == "acousticbrainz":
        challenger = AcousticBrainzSource()
        print(
            "Note: AcousticBrainz goes through MusicBrainz at 1 req/s. Do not run "
            "this alongside a full `djset enrich` — two processes sharing that "
            "budget risks a block.\n"
        )
    else:
        challenger = DeezerSource()

    with db.session() as conn:
        features = db.all_features(conn)
        tracks = [t for t in db.sample_tracks(conn, args.sample * 4)
                  if features.get(t.spotify_id) is not None][: args.sample]
        if not tracks:
            print("Nothing enriched yet — run `djset enrich` first.")
            return 1

        print(f"Putting {len(tracks)} already-resolved tracks to {args.against}…\n")
        result = cross_check(
            tracks, features, challenger, skip_source=challenger.name,
            progress=_enrich_progress,
        )

    print("\n")
    print(result.report(args.against))
    return 0


def cmd_manual(args: argparse.Namespace) -> int:
    from .camelot import to_camelot
    from .enrichment.base import set_manual_features

    camelot = to_camelot(args.key) if args.key else None
    if args.key and camelot is None:
        print(f"Could not parse key {args.key!r}. Try '8A', 'Am', 'F#m', 'Db'.")
        return 1
    with db.session() as conn:
        set_manual_features(conn, args.track_id, args.bpm, camelot)
    print(f"{args.track_id}: bpm={args.bpm} key={camelot} (source=manual, highest trust)")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="djset", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("login", help="run the PKCE flow and save the refresh token")
    sp.add_argument("--forget", action="store_true", help="delete the saved login")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("whoami", help="GET /me smoke test")
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("sources", help="list playlists + Liked Songs")
    sp.set_defaults(func=cmd_sources)

    sp = sub.add_parser("sync", help="pull playlists and artist genres into SQLite")
    _add_sync_args(sp)
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("artists", help="fetch artist genres only (slow: 1 req/artist)")
    sp.add_argument("--force", action="store_true", help="re-fetch already-cached artists")
    sp.set_defaults(func=cmd_artists)

    sp = sub.add_parser("enrich", help="fill BPM/key from GetSongBPM")
    _add_enrich_args(sp)
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("coverage", help="print the coverage report")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_coverage)

    sp = sub.add_parser(
        "crosscheck",
        help="ask a second source about tracks that already have data, and "
        "report where the two disagree. Writes nothing.",
    )
    sp.add_argument(
        "--against",
        default="deezer",
        choices=["deezer", "acousticbrainz", "getsongbpm"],
        help="the second opinion. deezer is the cheap one — it needs no API "
        "key and does not touch MusicBrainz's 1 req/s budget (default: deezer)",
    )
    sp.add_argument("--sample", type=int, default=150)
    sp.add_argument(
        "--tolerance",
        type=float,
        default=SAME_VALUE_TOLERANCE,
        help=f"relative BPM difference still counted as the same value "
        f"(default: {SAME_VALUE_TOLERANCE})",
    )
    sp.set_defaults(func=cmd_crosscheck)

    sp = sub.add_parser("report", help="sync + enrich + coverage (the step-3 gate)")
    _add_sync_args(sp)
    _add_enrich_args(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("generate", help="sequence a set and create the playlist")
    sp.add_argument("--playlist", action="append", help="source playlist id (repeatable)")
    sp.add_argument("--genre", action="append", help="genre filter (repeatable, OR)")
    sp.add_argument("--mode", choices=["bpm", "key", "bpm+key"], default="bpm+key")
    sp.add_argument("--tolerance", type=float, default=0.06, help="BPM tolerance 0.02-0.12")
    sp.add_argument("--tracks", type=int, help="target track count")
    sp.add_argument("--minutes", type=float, help="target duration instead of a count")
    sp.add_argument(
        "--all", action="store_true",
        help="reorder EVERY eligible track instead of picking a count",
    )
    sp.add_argument(
        "--track", action="append",
        help="restrict to specific spotify track ids (repeatable)",
    )
    sp.add_argument("--start-track", help="spotify track id to open with")
    sp.add_argument("--no-half-double", action="store_true", help="disable 70<->140 matching")
    sp.add_argument("--energy-boost", action="store_true", help="allow +7 Camelot moves")
    sp.add_argument("--name", help="playlist name (defaults to filter + mode + count)")
    sp.add_argument("--public", action="store_true", help="create it public (default private)")
    sp.add_argument("--dry-run", action="store_true", help="sequence only, create nothing")
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("tracks", help="list track ids for use with `generate --track`")
    sp.add_argument("--playlist", action="append", help="source playlist id (repeatable)")
    sp.add_argument("--search", help="filter by title or artist")
    sp.add_argument("--usable-only", action="store_true", help="only tracks with BPM+key")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_tracks)

    sp = sub.add_parser("exports", help="list playlists this app created")
    sp.set_defaults(func=cmd_exports)

    sp = sub.add_parser("ui", help="open the desktop window")
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("doctor", help="probe the API surface against the build brief")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("about", help="paths and required attribution")
    sp.set_defaults(func=cmd_about)

    sp = sub.add_parser("manual", help="hand-enter BPM/key for one track")
    sp.add_argument("track_id")
    sp.add_argument("--bpm", type=float)
    sp.add_argument("--key", help="'8A', 'Am', 'F#m', 'Db'…")
    sp.set_defaults(func=cmd_manual)

    return p


def _add_sync_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--playlist", action="append", help="playlist id (repeatable)")
    sp.add_argument("--no-liked", action="store_true", help="skip Liked Songs")
    sp.add_argument("--no-artists", action="store_true", help="skip artist genre fetch")
    sp.add_argument("--force", action="store_true", help="ignore snapshot_id / re-fetch")


def _add_enrich_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--limit", type=int, help="only process the first N tracks")
    sp.add_argument(
        "--sample",
        type=int,
        help="process a deterministic random sample of N tracks — use this to "
        "measure coverage, not --limit",
    )
    sp.add_argument(
        "--refresh",
        action="store_true",
        help="re-attempt everything, cached hits included — only useful when an "
        "existing source's data is itself suspect",
    )
    sp.add_argument(
        "--retry-misses",
        action="store_true",
        help="re-attempt tracks that failed before, keeping cached hits — this "
        "is the flag to use after registering a new source",
    )
    sp.add_argument("--no-deezer", action="store_true", help="disable the Deezer source")
    sp.add_argument(
        "--no-getsongbpm", action="store_true", help="disable the GetSongBPM source"
    )
    sp.add_argument(
        "--no-acousticbrainz",
        action="store_true",
        help="disable the AcousticBrainz source — it is rate-limited to 1 req/s "
        "by MusicBrainz, so it dominates the runtime of a full pass",
    )


def _force_utf8_console() -> None:
    """A music library is full of accents; a cp1252 console would raise
    UnicodeEncodeError mid-progress-bar. Never let output kill a long pass."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"\nConfiguration problem:\n{exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        close_client()


if __name__ == "__main__":
    raise SystemExit(main())
