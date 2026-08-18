---
name: verify
description: How to build, launch and drive djset to observe a change at its surface. Use when verifying work in this repo.
---

# Verifying djset

Two surfaces: the CLI (`python -m djset.cli …`) and one PySide6 window.
There is no server and no browser.

## Never disturb the live library

`djset enrich` may be running for hours against the real database. Take a
consistent snapshot and point everything at it:

```bash
python -c "
import sqlite3, sys; sys.path.insert(0,'src')
from djset.config import db_path
src = sqlite3.connect(str(db_path())); dst = sqlite3.connect('/tmp/v.sqlite3')
src.backup(dst); dst.close(); src.close()"
export DJSET_DB_PATH=/tmp/v.sqlite3
```

`DJSET_DB_PATH` overrides all database resolution and is the only safe way to
drive without touching real data.

## Which database is real

Under **Microsoft Store Python** the OS redirects writes to `%LOCALAPPDATA%`
into `…\Packages\PythonSoftwareFoundation.Python*\LocalCache\Local\djset\`.
The sandboxed process reads back the path it asked for, so `djset about` looks
right while the packaged `.exe` sees an empty library. `config.db_path()`
resolves this by picking whichever candidate holds the most tracks. If a
verification shows an empty library, check there before believing it.

## Driving the CLI

Safe, no network: `coverage`, `generate --dry-run`, `exports`, `about`.
Network: `sync`, `artists`, `enrich`, `crosscheck`.

- `crosscheck --against deezer` needs no key and avoids MusicBrainz's 1 req/s.
- `crosscheck --against acousticbrainz` must **not** run while `enrich` is
  running — two processes sharing that budget risks a block.
- `generate` without `--dry-run` creates a real playlist in the account.

## Driving the window

Offscreen renders every glyph as tofu. For readable screenshots use the real
platform with the window kept off the desktop:

```python
os.environ.pop("QT_QPA_PLATFORM", None)          # real fonts
w = MainWindow()
w.setAttribute(Qt.WA_DontShowOnScreen, True)     # lays out, never displays
w.show(); app.processEvents()
w.grab().save("shot.png")
```

Stub these or the run hangs on a modal: `QMessageBox.information/warning/
critical/question/about`, `QMessageBox.exec`, `QDialog.exec`. `about` is the
easy one to forget and it blocks forever.

Replace `w.runner` and `w.export_runner` with a fake exposing `busy`, `start`
and `cancel`. `on_generate` builds the set and populates the result table
*before* the export job starts, so a stubbed runner still exercises the whole
sequencing path with nothing written to Spotify.

`isVisible()` is always False for a child of an unshown window — assert on
`isHidden()` instead, or the check passes no matter what the code does.

## Gotchas

- Scratch scripts need `sys.stdout.reconfigure(encoding="utf-8",
  errors="replace")`; the library is full of accents and the console is cp1252.
- Log output interleaves with the `\r` progress line; `tr '\r' '\n'` before
  grepping.
- PyInstaller: `python -m PyInstaller packaging/djset.spec --noconfirm`, ~100s.
  Verify the result by running it, not by the build exiting 0 — a bundle can
  build clean and still miss `schema.sql` or a keyring backend. The tell that
  it worked is a fresh database appearing at the path it reports.
