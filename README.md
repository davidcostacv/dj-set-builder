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

**Enrichment coverage is partial, and every source was measured before it was
added.** GetSongBPM's catalogue is thin on post-2015 hip-hop, reggaetón and
Latin pop — verified, not guessed: ten missed tracks by well-known artists were
re-queried under every search formulation the API supports and none were
recoverable, while the artists themselves *are* present. Tracks without BPM and
key can still be filtered and split; they just cannot take part in harmonic
sequencing.

Two further sources were added to close the gap, each measured on the tracks
its predecessors had already failed on:

| Source | Gives | Match | Marginal gain on prior misses |
|---|---|---|---|
| GetSongBPM (20) | BPM + key | fuzzy artist/title | baseline, ~35% of library |
| AcousticBrainz (25) | BPM + key | exact, ISRC → MBID | **38%** with key, 5% BPM-only |
| Deezer (30) | BPM only | exact, ISRC | ~20% |

Spotify's own `/audio-features` and `/audio-analysis` are **not** an option:
both return `403` for all applications since November 2024. Verified against a
live token; the bare 403 with no message body is the deprecation signature, not
a scope problem.

AcousticBrainz stopped accepting submissions in 2022 but still serves its
dataset. It is the only free source found that supplies harmonic data on an
exact join. Because it needs two hops and MusicBrainz enforces one request per
second, it dominates the runtime of a full pass — `djset enrich
--no-acousticbrainz` skips it. Its key is only trusted above an Essentia
`key_strength` of 0.5; below that the tempo is kept and the key dropped, since
a missing key costs one track while a wrong key corrupts every transition it
takes part in. That floor is measured: over 26 analyses the median strength was
0.64 and only 11.5% fell below it.

**Spotify may no longer return artist genres — still unresolved.** Every artist
fetched so far came back with an empty `genres` array, across 600+ artists. Two
explanations fit that equally well: the field is gone for everyone, or the
request is wrong. They call for opposite responses, so it should not be acted
on until it is settled, and it has not been.

If the removal is real, the genre filter and Split-by-genre have no data source.
The code degrades cleanly — everything falls into the `Unknown` bucket and "no
filter" remains the default path — but the feature would be empty. GetSongBPM's
artist search returns genres and is the natural substitute.

The pane says which of the three cases it is in rather than just going blank:
tags not fetched, fetched-and-empty, or fetched-and-empty-so-far with some
artists still outstanding. That last distinction matters here — an interrupted
sync left 600 of 4,615 artists fetched, so claiming the other 4,015 are
untagged would assert something nobody has checked. It says so on the collapsed
button too, since the pane is collapsed by default and an explanation only
visible after expanding is one most people never see. Every wording states that
Generate is unaffected, because a filter that looks broken invites the reader to
assume it blocks something.

Confirming it is blocked on quota. Worth recording precisely, because it is not
what "rate limited" usually means: **the penalty is scoped per endpoint, not per
token.** With the same credentials in the same second, `GET /v1/me` answers
`200` while `GET /v1/artists/{id}` answers `429` with `Retry-After: 32028`
(~8.9 hours). So a probe against a convenient endpoint proves nothing about the
one you actually need — check the endpoint in question, and only once, since
each attempt while penalised appears to extend the window.

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

### Three ways to choose what goes in

| | |
|---|---|
| **A set of N tracks** | Target `tracks` or `minutes`. Picks the best path it can find and tells you if it fell short. |
| **Reorder a whole playlist** | Target `whole selection`. Every eligible track gets a place, ordered as well as the data allows. |
| **Hand-pick tracks** | **Choose tracks…** in pane 1 opens a searchable picker. Sequence just those. |

### Filling in what no source knows

Coverage has a ceiling no amount of API work removes, so the picker lets you
type a BPM and key yourself. Set its filter to **Missing BPM or key**, select a
track, and press **Set BPM / key…** (or double-click it). The key field takes
any notation the converter understands — `8A`, `Am`, `F#m`, `Db`, `C minor`,
`9m` — and echoes back the Camelot code it resolved to.

A hand-typed value is priority 0: the highest trust there is, never overwritten
by an automated source. Fields left blank are *filled in* from what is already
on file rather than clearing it, so supplying only the key keeps a BPM that
Deezer found. Same thing headless: `djset manual <track-id> --bpm 128 --key Am`.

"Reorder a whole playlist" is a different job from "build a set", and it behaves
differently on purpose. A strict BPM+key chain through several hundred tracks
rarely exists, so leftovers are placed at the gentlest available seam rather
than dropped — and the count of forced seams is reported, never hidden. A
numeric target keeps the stricter behaviour: it returns a short set and names
the limiting factor instead of padding.

**From the CLI**, the same three:

```bash
djset generate --mode bpm+key --tracks 24 --dry-run
```

```bash
djset generate --playlist <id> --all --dry-run
```

```bash
djset tracks --search "mac miller" --usable-only
```

then feed specific ids to `djset generate --track <id> --track <id> --all`.
Drop `--dry-run` to create the playlist for real.

### Duplicates

The same recording routinely appears as an album cut, a single and a
compilation, each with its own Spotify id — and sometimes with *different*
ISRCs after a re-release. Sets are deduplicated on ISRC first and normalised
artist+title second. Remixes, extended mixes and live versions are deliberately
kept distinct: they are different recordings at different tempos, and a DJ
wants both available.

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
| `djset enrich` | fill BPM/key from the source chain. `--sample N` for a representative random sample (use this to measure coverage); `--limit N` takes the first N in storage order and is biased. `--refresh` re-attempts everything after adding a source. `--no-acousticbrainz` / `--no-deezer` / `--no-getsongbpm` disable one source |
| `djset artists` | fetch artist genres only (slow: one request per artist) |
| `djset coverage` | print the coverage report (`--json` for machine-readable) |
| `djset crosscheck` | ask a second source about tracks that already have data and report disagreements. Writes nothing. `--against deezer` (default) needs no key and avoids MusicBrainz's 1 req/s; `--against acousticbrainz` compares keys but must not run alongside a full `enrich` |
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

Registered today: `GetSongBPMSource` (20), `AcousticBrainzSource` (25),
`DeezerSource` (30). The order encodes what each is worth — curated values beat
estimates, and a source carrying key beats one carrying only tempo.
`RekordboxXMLSource` is priority 10 and is a deliberate stub; registering it
later automatically supersedes API data on conflict with no other code
changing. `tests/test_source_priority.py` proves that ordering works today,
against two fake sources.

Adding a source is a registration, not a refactor — Deezer and AcousticBrainz
were both added without touching the sequencer, the filter, or the UI.

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
  enrichment/     FeatureSource protocol, resolver, normalizer,
                  GetSongBPM + AcousticBrainz + Deezer, Rekordbox stub
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
