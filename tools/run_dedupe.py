"""Run the de-duplication over playlists that have a verified snapshot.

Refuses to touch any playlist that is not in the snapshot file, so there is
always a way back. Verifies the live track count after each rewrite and stops
on the first surprise.

    python tools/run_dedupe.py snapshot.json targets.json [--limit N]
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from djset.config import load_config  # noqa: E402
from djset.spotify.auth import SpotifyAuth  # noqa: E402
from djset.spotify.client import SpotifyClient  # noqa: E402

API = "http://127.0.0.1:8078"

snapshot = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
targets = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 999

by_id = {t["playlist_id"]: t for t in targets}
# Only what is provably restorable, smallest rewrite first.
safe = [pid for pid in snapshot if snapshot[pid].get("uris")]
safe.sort(key=lambda p: snapshot[p]["count"])

client = SpotifyClient(SpotifyAuth(load_config()))
results = []
print(f"{'playlist':<34}{'antes':>7}{'quitó':>7}{'ahora':>7}  estado")
print("-" * 72)

for pid in safe[:limit]:
    snap = snapshot[pid]
    name = snap["name"]
    expect = by_id.get(pid, {}).get("extra_copies", 0)
    before = snap["count"]
    try:
        r = httpx.post(f"{API}/api/dedupe", json={"playlist_id": pid}, timeout=300.0)
        if r.status_code != 200:
            print(f"{name[:33]:<34}{before:>7}{'-':>7}{'-':>7}  HTTP {r.status_code} {r.text[:60]}")
            results.append({"id": pid, "name": name, "ok": False, "error": r.text[:200]})
            break
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"{name[:33]:<34}{before:>7}{'-':>7}{'-':>7}  {type(exc).__name__}: {exc}")
        results.append({"id": pid, "name": name, "ok": False, "error": str(exc)[:200]})
        break

    removed = data.get("removed", 0)
    # Independent check: ask Spotify what the playlist holds now.
    try:
        live_now = len(client.playlist_item_uris(pid))
    except Exception as exc:  # noqa: BLE001
        live_now = -1
        print(f"  (no pude verificar en vivo: {type(exc).__name__})")

    ok = live_now == before - removed if live_now >= 0 else None
    state = "OK" if ok else ("SIN VERIFICAR" if ok is None else f"¡DESCUADRE! esperaba {before-removed}")
    print(f"{name[:33]:<34}{before:>7}{removed:>7}{live_now:>7}  {state}")
    results.append({"id": pid, "name": name, "before": before, "removed": removed,
                    "after": live_now, "expected_removals": expect, "ok": bool(ok)})
    if ok is False:
        print("\n*** Descuadre: me detengo. El respaldo permite restaurar esta playlist. ***")
        break

Path("dedupe-results.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
done = [r for r in results if r.get("ok")]
print("-" * 72)
print(f"completadas: {len(done)}   pistas eliminadas: {sum(r['removed'] for r in done)}")
