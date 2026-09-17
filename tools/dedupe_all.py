"""Unattended: snapshot everything, de-duplicate everything, verify, self-heal.

Safety rules, in order of importance:

1. A playlist is never rewritten unless its full ordered URI list is already
   captured in the snapshot file. No snapshot, no touch.
2. After every rewrite the live track count is checked against
   ``before - removed``. A mismatch means the rewrite went wrong — most likely
   truncated between the replace and the follow-up adds — and the playlist is
   restored from the snapshot immediately, then the run stops.
3. Rate limits are waited out rather than treated as failures. Spotify hands
   back a Retry-After; we sleep it off and carry on.

    python tools/dedupe_all.py
"""
from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from djset.config import load_config  # noqa: E402
from djset.net import RateLimited  # noqa: E402
from djset.spotify.auth import SpotifyAuth  # noqa: E402
from djset.spotify.client import SpotifyClient  # noqa: E402

HOME = Path(r"C:\Users\David\djset")
API = "http://127.0.0.1:8078"
TARGETS = HOME / "dedupe-targets.json"
SNAPSHOT = HOME / "snapshot-real-full.json"
RESULTS = HOME / "dedupe-results.json"
CHUNK = 100
MAX_WAIT = 3 * 60 * 60


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {msg}")


def load(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save(p: Path, data) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def patient(fn, *a, **kw):
    """Call fn, sleeping through rate limits instead of failing."""
    while True:
        try:
            return fn(*a, **kw)
        except RateLimited as exc:
            wait = min(getattr(exc, "retry_after", None) or 900, MAX_WAIT)
            log(f"  rate limit: durmiendo {wait/60:.0f} min")
            time.sleep(wait + 15)


targets = load(TARGETS, [])
by_id = {t["playlist_id"]: t for t in targets}
snapshot = load(SNAPSHOT, {})

# Seed from any earlier partial snapshot files.
for old in sorted(HOME.glob("snapshot-real-2*.json")):
    for pid, v in load(old, {}).items():
        if v.get("uris") and pid not in snapshot:
            snapshot[pid] = v
save(SNAPSHOT, snapshot)
log(f"respaldo inicial: {len(snapshot)}/{len(targets)} playlists")

client = SpotifyClient(SpotifyAuth(load_config()))

# ---------------------------------------------------------------- snapshot
for i, t in enumerate(targets, 1):
    pid, name = t["playlist_id"], t["name"]
    if snapshot.get(pid, {}).get("uris"):
        continue
    try:
        meta = patient(client.playlist, pid)
        uris = patient(client.playlist_item_uris, pid)
    except Exception as exc:  # noqa: BLE001
        log(f"[{i}/{len(targets)}] {name[:38]}: sin respaldo ({type(exc).__name__})")
        continue
    if not uris:
        continue
    snapshot[pid] = {"name": name, "snapshot_id": meta.get("snapshot_id"),
                     "count": len(uris), "uris": uris}
    save(SNAPSHOT, snapshot)
    log(f"[{i}/{len(targets)}] {name[:38]}: respaldadas {len(uris)}")

log(f"RESPALDO LISTO: {len(snapshot)} playlists, "
    f"{sum(v['count'] for v in snapshot.values())} pistas")

# ---------------------------------------------------------------- restore
def restore(pid: str) -> bool:
    uris = snapshot[pid]["uris"]
    log(f"  RESTAURANDO {snapshot[pid]['name'][:40]} ({len(uris)} pistas)…")
    try:
        patient(client.replace_items, pid, uris[:CHUNK])
        for s in range(CHUNK, len(uris), CHUNK):
            patient(client.add_items, pid, uris[s:s + CHUNK])
        log("  restaurada")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"  *** FALLÓ LA RESTAURACIÓN: {exc} — respaldo en {SNAPSHOT}")
        return False


# ---------------------------------------------------------------- dedupe
results = load(RESULTS, [])
done = {r["id"] for r in results if r.get("ok")}
order = sorted((p for p in snapshot if p not in done),
               key=lambda p: snapshot[p]["count"])
log(f"a procesar: {len(order)} playlists")

for n, pid in enumerate(order, 1):
    snap = snapshot[pid]
    name, before = snap["name"], snap["count"]
    while True:
        try:
            r = httpx.post(f"{API}/api/dedupe", json={"playlist_id": pid}, timeout=600.0)
        except Exception as exc:  # noqa: BLE001
            log(f"[{n}] {name[:38]}: servidor caído ({type(exc).__name__}); paro")
            save(RESULTS, results)
            raise SystemExit(1)
        if r.status_code == 200:
            break
        if "Rate limited" in r.text or "rate limit" in r.text.lower():
            log(f"[{n}] {name[:38]}: rate limit; durmiendo 20 min")
            time.sleep(20 * 60)
            continue
        log(f"[{n}] {name[:38]}: HTTP {r.status_code} {r.text[:120]}")
        results.append({"id": pid, "name": name, "ok": False, "error": r.text[:200]})
        save(RESULTS, results)
        break
    else:
        continue

    if r.status_code != 200:
        continue

    removed = r.json().get("removed", 0)
    live_now = patient(client.playlist_item_uris, pid)
    after = len(live_now)
    ok = after == before - removed

    results.append({"id": pid, "name": name, "before": before, "removed": removed,
                    "after": after, "ok": ok})
    save(RESULTS, results)
    log(f"[{n}/{len(order)}] {name[:38]}: {before} -> {after} (quitó {removed}) "
        f"{'OK' if ok else 'DESCUADRE'}")

    if not ok:
        log(f"  esperaba {before - removed}, Spotify dice {after}")
        restore(pid)
        log("*** Me detengo tras el descuadre. ***")
        break

ok_rows = [r for r in results if r.get("ok")]
log("")
log(f"TERMINADO: {len(ok_rows)} playlists limpiadas, "
    f"{sum(r['removed'] for r in ok_rows)} pistas duplicadas eliminadas")
log(f"respaldo completo en {SNAPSHOT}")
