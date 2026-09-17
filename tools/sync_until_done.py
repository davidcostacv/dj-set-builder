"""Keep running `djset sync` until it actually finishes.

`djset sync` treats a long Retry-After as fatal and exits 4, which leaves the
library half-loaded and playlists missing from the picker. This sleeps the
limit off and resumes; sync itself is incremental, so each pass starts where
the last one stopped.

    python tools/sync_until_done.py
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROOT = Path(__file__).resolve().parents[1]
MAX_PASSES = 12


def log(m: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {m}")


def playlist_count() -> int:
    import sqlite3
    from djset.config import db_path
    try:
        con = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
        return con.execute("select count(*) from playlists_cache").fetchone()[0]
    except Exception:
        return -1


for attempt in range(1, MAX_PASSES + 1):
    before = playlist_count()
    log(f"pasada {attempt}: la app tiene {before} playlists — ejecutando djset sync")
    proc = subprocess.run(
        [sys.executable, "-m", "djset.cli", "sync"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
    )
    tail = (proc.stdout or "") + (proc.stderr or "")
    after = playlist_count()
    log(f"  terminó con código {proc.returncode}; ahora {after} playlists "
        f"(+{after - before})")

    if proc.returncode == 0:
        log("SYNC COMPLETA")
        break

    waits = [float(x) for x in re.findall(r"Retry-After (\d+)", tail)]
    wait = min(max(waits) if waits else 900, 3 * 3600)
    log(f"  límite de Spotify; durmiendo {wait/60:.0f} min y reintentando")
    time.sleep(wait + 30)
else:
    log("agotadas las pasadas; vuelve a lanzarlo si aún faltan playlists")

log(f"playlists finales en la app: {playlist_count()}")
