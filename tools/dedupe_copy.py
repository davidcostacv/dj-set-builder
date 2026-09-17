"""De-duplicate playlists without destroying anything by default.

Policy, per playlist:

* **Nobody else saved it** (``followers.total == 0``) — rewrite it in place.
  It is yours alone, so tidying it affects no one else.
* **Somebody saved it** — leave the original completely untouched and write a
  new playlist, ``"<name> (sin duplicados)"``, holding the same order minus the
  extra copies. Other people's libraries keep pointing at what they saved.

A track with no ISRC is never removed: without the recording's own identifier
there is no proof two entries are the same recording.

Run with ``--plan`` to see what it would do and write nothing at all.

    python tools/dedupe_copy.py --plan
    python tools/dedupe_copy.py
"""
from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from djset.config import load_config  # noqa: E402
from djset.net import RateLimited  # noqa: E402
from djset.spotify.auth import SpotifyAuth  # noqa: E402
from djset.spotify.client import SpotifyClient  # noqa: E402

HOME = Path(r"C:\Users\David\djset")
TARGETS = HOME / "dedupe-targets.json"
SNAPSHOT = HOME / "snapshot-real-full.json"
STATE = HOME / "dedupe-copy-state.json"
SUFFIX = " (sin duplicados)"
CHUNK = 100
PLAN_ONLY = "--plan" in sys.argv


def log(m: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {m}")


def load(p: Path, d):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return d


def save(p: Path, data) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def patient(fn, *a, **kw):
    while True:
        try:
            return fn(*a, **kw)
        except RateLimited as exc:
            wait = min(float(getattr(exc, "retry_after", 900) or 900), 24 * 3600)
            # Spotify hands back multi-hour waits to apps in development
            # mode; honour them rather than waking early to be refused again.
            log(f"    rate limit — durmiendo {wait/60:.0f} min")
            time.sleep(wait + 15)


targets = load(TARGETS, [])
snapshot = load(SNAPSHOT, {})
state = load(STATE, {})
client = SpotifyClient(SpotifyAuth(load_config()))

log(f"{len(targets)} playlists con duplicados; modo "
    f"{'PLAN (no escribe nada)' if PLAN_ONLY else 'EJECUCIÓN'}")
log("")

created = in_place = skipped = 0
removed_total = 0

for i, t in enumerate(targets, 1):
    pid, name = t["playlist_id"], t["name"]
    if state.get(pid, {}).get("done"):
        continue

    try:
        meta = patient(client.playlist, pid)
        pairs = [(tr.uri, tr.isrc) for tr, _ in patient(
            lambda p: list(client.playlist_items(p)), pid)]
    except Exception as exc:  # noqa: BLE001
        log(f"[{i}/{len(targets)}] {name[:36]}: no accesible ({type(exc).__name__})")
        state[pid] = {"done": False, "error": type(exc).__name__}
        save(STATE, state)
        continue

    if not pairs:
        log(f"[{i}/{len(targets)}] {name[:36]}: vacía, omitida")
        continue

    followers = ((meta.get("followers") or {}).get("total")) or 0

    seen: set[str] = set()
    keep: list[str] = []
    for uri, isrc in pairs:
        if isrc:
            if isrc in seen:
                continue
            seen.add(isrc)
        keep.append(uri)
    removed = len(pairs) - len(keep)

    if removed == 0:
        log(f"[{i}/{len(targets)}] {name[:36]}: sin duplicados reales, omitida")
        state[pid] = {"done": True, "removed": 0}
        save(STATE, state)
        continue

    mode = "in-place" if followers == 0 else "copia"
    log(f"[{i}/{len(targets)}] {name[:36]}: {len(pairs)} -> {len(keep)} "
        f"(quita {removed}) · seguidores={followers} · {mode}")
    removed_total += removed

    if PLAN_ONLY:
        if followers == 0:
            in_place += 1
        else:
            created += 1
        continue

    # Never rewrite in place without a restorable snapshot of that playlist.
    if followers == 0 and not snapshot.get(pid, {}).get("uris"):
        snapshot[pid] = {"name": name, "count": len(pairs),
                         "uris": [u for u, _ in pairs]}
        save(SNAPSHOT, snapshot)

    try:
        if followers == 0:
            patient(client.replace_items, pid, keep[:CHUNK])
            for s in range(CHUNK, len(keep), CHUNK):
                patient(client.add_items, pid, keep[s:s + CHUNK])
            after = len(patient(client.playlist_item_uris, pid))
            if after != len(keep):
                log(f"    DESCUADRE: esperaba {len(keep)}, hay {after} — restaurando")
                orig = snapshot[pid]["uris"]
                patient(client.replace_items, pid, orig[:CHUNK])
                for s in range(CHUNK, len(orig), CHUNK):
                    patient(client.add_items, pid, orig[s:s + CHUNK])
                log("    restaurada; me detengo")
                break
            in_place += 1
            state[pid] = {"done": True, "mode": "in-place",
                          "before": len(pairs), "after": after, "removed": removed}
        else:
            new = patient(client.create_playlist, f"{name}{SUFFIX}",
                          f"Copia de «{name}» sin copias repetidas. Original intacto.")
            nid = new["id"]
            for s in range(0, len(keep), CHUNK):
                patient(client.add_items, nid, keep[s:s + CHUNK])
            after = len(patient(client.playlist_item_uris, nid))
            created += 1
            state[pid] = {"done": True, "mode": "copia", "new_id": nid,
                          "new_name": f"{name}{SUFFIX}", "after": after,
                          "removed": removed}
            log(f"    creada «{name[:30]}{SUFFIX}» con {after} pistas")
    except Exception as exc:  # noqa: BLE001
        log(f"    FALLÓ: {type(exc).__name__}: {exc}")
        state[pid] = {"done": False, "error": str(exc)[:200]}
        save(STATE, state)
        continue

    save(STATE, state)

log("")
log(f"{'PLAN' if PLAN_ONLY else 'HECHO'}: {in_place} limpiadas in-place, "
    f"{created} copias nuevas, {removed_total} copias repetidas en total")
