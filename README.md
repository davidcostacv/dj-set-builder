# djset — personal Spotify playlist & DJ set generator

Single-user, local-only desktop app. Reads your Spotify library, optionally
filters it by genre, sequences a harmonically-mixed set, and creates the
playlist directly in your Spotify account. The output is a playlist link and
nothing else — no file exports.

Stack: Python 3.11+, PySide6, SQLite, `httpx`, and
[GetSongBPM](https://getsongbpm.com) for BPM and key data.

> **BPM and musical key data provided by [GetSongBPM](https://getsongbpm.com).**
> Spotify removed its `audio-features` endpoint in November 2024, so every tempo
> and key in this app comes from [https://getsongbpm.com](https://getsongbpm.com).

---

## Status

All nine build-order steps are implemented. **283 tests, no network calls.**

| Step | | |
|---|---|---|
| 1 | Auth + token persistence + `/me` smoke test | done |
| 2 | Playlist sync into SQLite (respects `snapshot_id`) | done |
| 3 | `FeatureSource` protocol + `GetSongBPMSource` + normalizer + coverage CLI | done |
| 4 | Genre filter logic + alias collapsing | done |
| 5 | Camelot conversion + sequencing engine | done |
| 6 | Playlist creation + idempotency guard | done |
| 7 | PySide6 UI, panes 1→4 | done |
| 8 | Split by genre | done |
| 9 | PyInstaller packaging | done |

### Known limitations, measured rather than assumed

**Enrichment coverage is ~29%.** GetSongBPM's catalogue is thin on post-2015
hip-hop, reggaetón and Latin pop. This was verified, not guessed: ten missed
tracks by well-known artists were re-queried under every search formulation the
API supports and none were recoverable, while the artists themselves *are*
present. Tracks without BPM and key can still be filtered and split; they just
cannot take part in harmonic sequencing.

**Spotify may no longer return artist genres.** Every artist fetched so far came
back with an empty `genres` array. If that is a permanent removal, the genre
filter and Split-by-genre have no data source — the code degrades cleanly
(everything falls into the `Unknown` bucket and "no filter" remains the default
path), but the feature would be empty. GetSongBPM's artist search returns genres
and would be the natural substitute.

---

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env
```

Fill in `.env`:

- `SPOTIFY_CLIENT_ID` — create an app at
  https://developer.spotify.com/dashboard and add
  `http://127.0.0.1:8888/callback` to its redirect URIs. PKCE means there is no
  client secret. Development mode requires the app owner to hold active Spotify
  Premium.
- `GETSONGBPM_API_KEY` — free key from https://getsongbpm.com/api.

Then:

```bash
djset login      # authorize once
djset sync       # pull playlists into SQLite
djset enrich     # fill BPM/key (long; Ctrl+C is safe, it resumes)
djset ui         # open the window
```

---

## Using it

**The window.** Panes run left to right: pick sources, optionally filter by
genre, set the mode and length, press **Generate & Save to Spotify**. That one
button sequences the set *and* creates the playlist in your account — there is
no separate export step. The result pane shows the link plus the ordered table,
which you can drag to reorder; **Update playlist** pushes edits back.

**From the CLI**, the same flow without the window:

```bash
djset generate --mode bpm+key --tracks 24 --dry-run
```

Drop `--dry-run` to create the playlist for real.

---

## Packaging

```bash
pyinstaller packaging/djset.spec --noconfirm
```

Produces `dist/djset/djset.exe`. One-folder rather than one-file: a one-file
build unpacks Qt to a temp directory on every launch, which is slow and trips
some antivirus. The database lives in the OS app-data directory, so the program
folder stays read-only and portable.

### If the build fails at the final EXE step

On this machine it stops with:

```
FileNotFoundError: [WinError 2] ... build\djset\djset.exe
```

`djset.pkg` (~13 MB) gets written but `djset.exe` is created and then vanishes.
That is antivirus quarantining the PyInstaller bootloader — a very common false
positive, because the bootloader is the same stub used by a lot of real malware.
Avast is active on this machine and is already known to intercept HTTPS here.

The fix is an antivirus exclusion for the build output folder
(`dj-set-builder\build` and `dj-set-builder\dist`), added in Avast's settings.
**Everything else works without packaging** — `djset ui` runs the app directly
from source, which is the normal way to use it during development.

---

## Commands

| | |
|---|---|
| `djset login` | PKCE flow; refresh token goes to the OS keyring. `--forget` clears it |
| `djset whoami` | `GET /me` smoke test |
| `djset sources` | list playlists + Liked Songs |
| `djset sync` | pull playlists and artist genres into SQLite |
| `djset enrich` | fill BPM/key from GetSongBPM. `--sample N` for a representative random sample (use this to measure coverage); `--limit N` takes the first N in storage order and is biased |
| `djset artists` | fetch artist genres only (slow: one request per artist) |
| `djset coverage` | print the coverage report (`--json` for machine-readable) |
| `djset report` | sync + enrich + coverage in one go |
| `djset generate` | sequence a set and create the playlist (`--dry-run` to preview) |
| `djset exports` | list playlists this app has created in your account |
| `djset ui` | open the desktop window |
| `djset doctor` | probe the live API against the build brief and report drift |
| `djset manual <id> --bpm 128 --key 8A` | hand-enter a straggler; highest trust |
| `djset about` | paths and the required GetSongBPM attribution |

`enrich` is interruptible. Ctrl+C at any point is safe — every hit and every
miss is committed as it lands, so a cancelled pass resumes rather than restarts.

---

## Architecture

Three layers, and **layer 3 never imports layer 1**:

1. `djset.spotify` — what music exists; writes finished playlists back.
2. `djset.enrichment` — the local BPM/key database Spotify cannot provide.
3. filter + matching — pure functions over SQLite, zero network calls.

Enrichment resolves through an ordered chain, stopping at the first hit: local
cache → registered sources by ascending `priority` → manual entry (highest
trust, never overwritten by an automated source).

`GetSongBPMSource` is priority 20. `RekordboxXMLSource` is priority 10 and is a
deliberate stub — registering it later automatically supersedes API data on
conflict with no other code changing. `tests/test_source_priority.py` proves
that ordering works today, against two fake sources.

### Files

```
src/djset/
  config.py       .env + OS app-data paths
  db.py           all SQLite access
  schema.sql      the data model
  net.py          shared httpx client, retry/backoff, Retry-After, rate limiter
  camelot.py      key -> Camelot wheel (lookup table, not arithmetic)
  report.py       the coverage report
  doctor.py       API-surface probe
  cli.py          headless entry point
  spotify/        auth (PKCE + keyring), client, sync
  enrichment/     FeatureSource protocol, resolver, normalizer, GetSongBPM, Rekordbox stub
```

---

## API notes

Built against the 2026 Spotify surface described in the brief:
`/playlists/{id}/items` (not `/tracks`), `POST /me/playlists`, single-entity
fetches only, and no `audio-features` / `recommendations` / `related-artists`
anywhere. `popularity` and `followers` are never referenced.

Per the brief's instruction to report drift rather than work around it, nothing
in the client silently falls back to an older path. `djset doctor` probes each
endpoint and prints what actually happens, so any divergence surfaces as a
finding instead of a hidden workaround.

---

## Environment notes

**Development mode can only read playlists you own.** Followed playlists return
403. On this library that is 63 of 274 sources; the sync reports them and moves
on rather than failing.

**TLS interception.** Consumer antivirus (Avast, here) and corporate proxies
man-in-the-middle HTTPS with a locally generated root CA. That root lives in the
OS trust store but not in certifi's, so certifi-based verification fails on
every request — Avast's root additionally has a non-critical Basic Constraints
extension that OpenSSL 3.x rejects. `djset.net` verifies against the OS trust
store via `truststore` to handle this. **Verification is never disabled.** If
you would rather not carry the dependency, turn off Avast's HTTPS scanning
(Settings → Protection → Core Shields → Web Shield) instead.

---

## Attribution

BPM and key data by **[GetSongBPM](https://getsongbpm.com)** —
[https://getsongbpm.com](https://getsongbpm.com)

Their terms require this backlink to be visible in the app. It is printed by
`djset about` and will be in the About pane of the UI. Do not remove it — they
suspend accounts without notice.

---

## Tests

```bash
pytest -q
```

Covers Camelot conversion, artist/title normalization, source priority
resolution, the enrichment runner's cache/miss/cancel behaviour, and the DB and
coverage layers. Tests for BPM tolerance, genre alias collapsing, graph path
construction, and the idempotency hash arrive with steps 4–6.

Nothing here makes a network call.
