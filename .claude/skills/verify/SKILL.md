---
name: verify
description: How to build, launch and drive djset to observe a change at its surface. Use when verifying work in this repo.
---

# Verifying djset

Three surfaces: the CLI (`python -m djset.cli …`), a PySide6 window
(`djset ui`), and a web app (`djset serve`, then http://127.0.0.1:8000).

The web app is where the project is heading; the Qt window still works. Both
read the same SQLite library, so a change to the engine shows up in both.

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

**Take it back off before starting anything that produces results worth
keeping.** It is inherited by every child process, including a `serve` started
for a screenshot and any enrichment launched from that shell. An eleven-hour
`--dsp` pass once wrote all 4,486 of its results into a scratch copy while the
real library kept none of them, and the app — started from the same shell —
reported the good numbers back, so nothing looked wrong from the browser.
`/api/health` prints the open database precisely so this is checkable: read it
before believing a coverage figure, and make sure a long run is pointed at
`config.db_path()` rather than whatever the last verification set.

## Which database is real

Under **Microsoft Store Python** the OS redirects writes to `%LOCALAPPDATA%`
into `…\Packages\PythonSoftwareFoundation.Python*\LocalCache\Local\djset\`.
The sandboxed process reads back the path it asked for, so `djset about` looks
right while the packaged `.exe` sees an empty library. `config.db_path()`
resolves this by picking whichever candidate holds the most tracks. If a
verification shows an empty library, check there before believing it.

Since 20 Aug 2026 this is settled by a **user-level `DJSET_DB_PATH`** set
outside `%LOCALAPPDATA%`, and so outside the redirection entirely. Ask the
machine where that is rather than assuming — `config.db_path()` prints it, and
so does `/api/health` on a running server:

```bash
python -c "from djset import config; print(config.db_path())"
```

It wins over all of the above and is inherited by every process, which is what
makes the warning further up matter: override it for a verification, forget to
take it back off, and real work goes somewhere temporary. Copies left behind by
earlier consolidations are parked beside their old locations as
`djset.sqlite3.superseded-*`.

## Driving the CLI

Safe, no network: `coverage`, `generate --dry-run`, `exports`, `about`.
Network: `sync`, `artists`, `enrich`, `crosscheck`.

- `crosscheck --against deezer` needs no key and avoids MusicBrainz's 1 req/s.
- `crosscheck --against acousticbrainz` must **not** run while `enrich` is
  running — two processes sharing that budget risks a block.
- `generate` without `--dry-run` creates a real playlist in the account.

## Driving the web app

`djset serve --port <n>` then drive the page with javascript_tool — it is far
more precise than screenshots for reading state:

```js
document.getElementById('sources-summary').textContent
[...document.querySelectorAll('#result tbody tr')].length
```

`preview_start` resolves `.claude/launch.json` against the *session* working
directory, not this repo — if the session is rooted elsewhere it will start
that project instead. Start the server yourself and open it with
`preview_start {url}`.

Routes worth driving: `/api/health` (proves which database is open),
`/api/selection`, `/api/generate` (creates nothing), `/api/duplicates`
(reports only), `/api/job`.

**Three routes write to the Spotify account**: `/api/export`,
`/api/reorder/{id}` and `/api/dedupe` — the last one *deletes* tracks from a
real playlist. Drive the page with a fetch guard installed so a stray click
cannot reach any of them:

```js
const real = window.fetch.bind(window);
window.fetch = async (u, o) => {
  if (/\/api\/(export|dedupe|reorder)/.test(String(u))) throw new Error("BLOCKED");
  return real(u, o);
};
```

Override `window.confirm` to return false, then true, to check a destructive
button is actually gated rather than only appearing to be.

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
