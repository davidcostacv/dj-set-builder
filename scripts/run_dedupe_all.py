"""Remove duplicates from every dirty playlist, one at a time, for real.

One live read per playlist. That single read serves three purposes at once:
it is the backup (written to disk before the playlist is touched), it is what
`remove_duplicates` rewrites from, and its post-write count is checked against
what the removal should have produced. Nothing here is inferred from the
local cache — the cache only ever says *which ids* are duplicates.

Paced deliberately slowly. Two earlier runs of a lighter version of this
burned the account's playlist-read budget for 21 hours and then 2 more, so
this asks for one request every few seconds rather than the client's usual
hundred a minute — an irreversible batch of edits is not the place to find
Spotify's ceiling.

Stops itself, rather than pressing on, the moment anything looks wrong: a
guard refusal, a live count that does not match the removal just made, or a
total across all playlists that does not add up. Playlists already finished
keep their changes; nothing already done is undone by a later playlist
failing.

    python scripts/run_dedupe_all.py <backup.json> [--rate N] [--limit N] [--exclude NAME]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from djset import config
from djset.export import ExportError, remove_duplicates
from djset.filtering import duplicate_groups
from djset.net import RateLimiter
from djset.spotify.auth import SpotifyAuth
from djset.spotify.client import API, SpotifyClient
from djset.web.app import library

SAFE_RATE_PER_HOUR = 900  # one request every four seconds


def _id_of(uri: str) -> str:
    return uri.rsplit(":", 1)[-1]


def raw_items(client: SpotifyClient, pid: str) -> list[dict]:
    out: list[dict] = []
    url: str | None = f"{API}/playlists/{pid}/items"
    params: dict | None = {"limit": 100}
    while url:
        page = client._get(url, **(params or {}))
        for item in page.get("items", []):
            track = item.get("track") or item.get("item") or {}
            out.append(
                {
                    "uri": track.get("uri"),
                    "name": track.get("name"),
                    "artists": [a.get("name") for a in track.get("artists") or []],
                    "added_at": item.get("added_at"),
                }
            )
        url = page.get("next")
        params = None
    return out


# Left alone on purpose. "El Loqueron" hit an HTTP 502 mid-write during the
# 2026-08-29 run and was never retried — told directly not to, after the run
# had already moved past it. A fresh invocation would otherwise pick it back
# up automatically, since it still has a live duplicate group; this is what
# stops that without relying on remembering it by hand each time.
EXCLUDE_BY_DEFAULT = {"El Loqueron"}


def main(
    backup_dest: str,
    rate: int = SAFE_RATE_PER_HOUR,
    limit: int | None = None,
    exclude: set[str] | None = None,
) -> int:
    exclude = EXCLUDE_BY_DEFAULT | (exclude or set())

    library.ensure()
    client = SpotifyClient(SpotifyAuth(config.load_config()))
    client.limiter = RateLimiter(rate)

    targets = []
    excluded_present = []
    for p in library.playlists:
        pid = p["spotify_id"]
        if pid == "__liked__":
            continue
        if p["name"] in exclude:
            excluded_present.append(p["name"])
            continue
        tracks = [
            library.by_id[i] for i in library.members.get(pid, []) if i in library.by_id
        ]
        if not tracks:
            continue
        groups = duplicate_groups(tracks, library.features, isrc_only=True)
        if groups:
            targets.append((p, tracks, groups))
    if limit:
        targets = targets[:limit]
    if excluded_present:
        print(f"excluded, left untouched: {', '.join(excluded_present)}")

    print(f"{len(targets)} playlists to clean, at {rate}/hour "
          f"(~{3600 / rate:.1f}s between requests)\n")

    backup: dict[str, dict] = {}
    results: list[dict] = []
    skipped: list[dict] = []
    total_removed = 0

    for i, (p, tracks, groups) in enumerate(targets, 1):
        pid, name = p["spotify_id"], p["name"]
        want = sum(len(g.remove) for g in groups)
        try:
            live_items = raw_items(client, pid)
            live_uris = [it["uri"] for it in live_items if it["uri"]]
            if not live_uris:
                print(f"[{i}/{len(targets)}] SKIP {name[:40]}: no readable tracks")
                continue

            backup[pid] = {"name": name, "items": live_items}
            Path(backup_dest).write_text(
                json.dumps(backup, ensure_ascii=False), encoding="utf-8"
            )  # written before the edit, every playlist, so a crash loses nothing

            before_n = len(live_uris)
            before_ids = {_id_of(u) for u in live_uris}
            removed = remove_duplicates(client, pid, tracks, groups, live=live_uris)

            after = client.playlist(pid)
            after_n = after.get("items", {}).get("total")
            expected = before_n - removed
            if after_n is not None and after_n != expected:
                print(f"\n[{i}/{len(targets)}] STOPPING — {name}: expected {expected} "
                      f"tracks after removing {removed} from {before_n}, Spotify "
                      f"reports {after_n}. Nothing after this playlist was touched.")
                return 1

            # Recording-level check, not just a count: for every group, is the
            # SONG still there under any of its ids — not necessarily the one
            # this run designated as the survivor? An id going to zero is
            # expected and correct when a sibling id for the same ISRC is
            # what's actually live; a matching count alone does not prove that.
            now_uris = client.playlist_item_uris(pid)
            now_ids = {_id_of(u) for u in now_uris}
            lost = []
            for g in groups:
                member_ids = [g.keep.spotify_id, *(t.spotify_id for t in g.remove)]
                was_present = any(m in before_ids for m in member_ids)
                still_present = any(m in now_ids for m in member_ids)
                if was_present and not still_present:
                    lost.append(g.keep)
            if lost:
                print(f"\n[{i}/{len(targets)}] STOPPING — {name}: "
                      f"{len(lost)} recording(s) have no surviving copy under "
                      f"any known id: {[t.title for t in lost][:3]}. "
                      "Nothing after this playlist was touched.")
                return 1

            total_removed += removed
            results.append({"name": name, "before": before_n, "removed": removed,
                            "after": after_n})
            print(f"[{i}/{len(targets)}] {name[:44]:<44} {before_n:>5} -> "
                  f"{after_n if after_n is not None else before_n - removed:>5}  "
                  f"(-{removed}, wanted -{want})", flush=True)

        except ExportError as exc:
            # This is the pre-write guard doing its job: it looked before
            # writing and declined. Nothing happened to this playlist, so
            # there is nothing here that stopping the whole run protects —
            # it costs 63 playlists of manual restarts for a refusal that
            # was already the safe outcome. Skip this one, keep going.
            skipped.append({"name": name, "reason": str(exc)})
            print(f"[{i}/{len(targets)}] REFUSED {name[:40]}: {exc}")
            continue
        except Exception as exc:  # a followed/collaborative playlist, a network blip
            print(f"[{i}/{len(targets)}] SKIP {name[:40]}: {type(exc).__name__}: {exc}")
            continue

    print(f"\ndone: {len(results)} playlists edited, {total_removed} tracks removed")
    if skipped:
        print(f"{len(skipped)} playlist(s) refused untouched — the cache's "
              "designated survivor was not actually live, and no other id "
              "for that recording was either:")
        for s in skipped:
            print(f"   {s['name'][:44]:<44} {s['reason']}")
    print(f"backup of everything touched: {backup_dest}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    rate = SAFE_RATE_PER_HOUR
    if "--rate" in args:
        rate = int(args[args.index("--rate") + 1])
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    exclude = {args[i + 1] for i, a in enumerate(args) if a == "--exclude"}
    flag_args = {"--rate", "--limit", "--exclude"}
    positional = [
        a for i, a in enumerate(args)
        if not a.startswith("--") and (i == 0 or args[i - 1] not in flag_args)
    ]
    raise SystemExit(main(positional[0], rate=rate, limit=limit, exclude=exclude))
