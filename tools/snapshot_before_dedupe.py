"""Full, restorable snapshot of every playlist about to be de-duplicated.

Writes the complete ordered URI list per playlist, so any playlist can be put
back exactly as it was. Saves incrementally: a rate-limit or a crash costs the
playlist in flight, never the ones already captured. Re-running resumes.

    python tools/snapshot_before_dedupe.py targets.json snapshot.json
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from djset.config import load_config  # noqa: E402
from djset.spotify.auth import SpotifyAuth  # noqa: E402
from djset.spotify.client import SpotifyClient  # noqa: E402

targets_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])

targets = json.loads(targets_path.read_text(encoding="utf-8"))
snapshot: dict[str, dict] = {}
if out_path.exists():
    try:
        snapshot = json.loads(out_path.read_text(encoding="utf-8"))
        print(f"reanudando: {len(snapshot)} playlists ya capturadas")
    except Exception:
        snapshot = {}

client = SpotifyClient(SpotifyAuth(load_config()))


def save() -> None:
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)


total = len(targets)
for i, t in enumerate(targets, 1):
    pid = t["playlist_id"]
    name = t["name"]
    if pid in snapshot and snapshot[pid].get("uris"):
        print(f"[{i}/{total}] {name[:40]}: ya capturada ({len(snapshot[pid]['uris'])})")
        continue
    try:
        meta = client.playlist(pid)
        uris = client.playlist_item_uris(pid)
    except Exception as exc:  # noqa: BLE001
        print(f"[{i}/{total}] {name[:40]}: FALLO -> {type(exc).__name__}: {exc}")
        save()
        continue

    if not uris:
        print(f"[{i}/{total}] {name[:40]}: VACÍA segun Spotify, se omite")
        continue

    snapshot[pid] = {
        "name": name,
        "snapshot_id": meta.get("snapshot_id"),
        "owner": (meta.get("owner") or {}).get("id"),
        "count": len(uris),
        "uris": uris,
    }
    save()
    print(f"[{i}/{total}] {name[:40]}: {len(uris)} pistas guardadas")

save()
captured = sum(v["count"] for v in snapshot.values())
print()
print(f"RESPALDO COMPLETO: {len(snapshot)} playlists, {captured} pistas en total")
print(f"archivo: {out_path}  ({out_path.stat().st_size:,} bytes)")
if not snapshot or captured == 0:
    print("*** RESPALDO VACÍO — NO CONTINUAR ***")
    raise SystemExit(1)
