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

## Status — stopped at the build-order step 3 checkpoint

The brief says: *"Do not open a Qt window until step 7. Stop after step 3 and
show me the coverage report."* That is exactly where this is.

| Step | | |
|---|---|---|
| 1 | Auth + token persistence + `/me` smoke test | done |
| 2 | Playlist sync into SQLite (respects `snapshot_id`) | done |
| 3 | `FeatureSource` protocol + `GetSongBPMSource` + normalizer + coverage CLI | done — **awaiting `GETSONGBPM_API_KEY`** |
| 4 | Genre filter logic + alias collapsing | not started |
| 5 | Camelot conversion + sequencing engine | conversion done, engine gated on step 3 |
| 6 | Playlist creation + idempotency guard | not started |
| 7 | PySide6 UI | not started |
| 8 | Split by genre | not started |
| 9 | PyInstaller packaging | not started |

Camelot conversion landed early because `audio_features.key_camelot` has to be
populated at enrichment time. The sequencing engine that consumes it is
deliberately not built yet.

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
djset login
djset report
```

`report` = sync + enrich + coverage. It is the step-3 gate.

---

## Commands

| | |
|---|---|
| `djset login` | PKCE flow; refresh token goes to the OS keyring. `--forget` clears it |
| `djset whoami` | `GET /me` smoke test |
| `djset sources` | list playlists + Liked Songs |
| `djset sync` | pull playlists and artist genres into SQLite |
| `djset enrich` | fill BPM/key from GetSongBPM. `--sample N` for a representative random sample (use this to measure coverage); `--limit N` takes the first N in storage order and is biased |
| `djset coverage` | print the coverage report (`--json` for machine-readable) |
| `djset report` | sync + enrich + coverage in one go |
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
