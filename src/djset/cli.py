"""Headless CLI. Everything through build-order step 6 is driven from here;
the Qt window does not exist yet.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import db
from .config import ConfigError, app_data_dir, db_path, load_config, log_path
from .enrichment import ATTRIBUTION_TEXT, GetSongBPMSource, Resolver, enrich_tracks
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


def cmd_enrich(args: argparse.Namespace) -> int:
    cfg = load_config(require_getsongbpm=True)
    resolver = Resolver([GetSongBPMSource(cfg.getsongbpm_api_key, cfg.getsongbpm_rate_per_hour)])

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

        print(f"Enriching {scope} tracks via GetSongBPM…")
        print("(Ctrl+C is safe — every result is committed as it lands.)\n")
        try:
            stats = enrich_tracks(conn, resolver, tracks, progress=_enrich_progress)
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

    sp = sub.add_parser("enrich", help="fill BPM/key from GetSongBPM")
    _add_enrich_args(sp)
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("coverage", help="print the coverage report")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_coverage)

    sp = sub.add_parser("report", help="sync + enrich + coverage (the step-3 gate)")
    _add_sync_args(sp)
    _add_enrich_args(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_report)

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
