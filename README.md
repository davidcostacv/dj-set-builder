# djset — personal Spotify playlist & DJ set generator

Reads your Spotify library, optionally filters it by genre, sequences a
harmonically-mixed set, and creates the playlist directly in your Spotify
account. The output is a playlist link and nothing else — no file exports.

Stack: Python 3.11+, SQLite, `httpx`, and
[GetSongBPM](https://getsongbpm.com) for BPM and key data.

> **BPM and musical key data provided by [GetSongBPM](https://getsongbpm.com).**
> Spotify removed its `audio-features` endpoint in November 2024, so every tempo
> and key in this app comes from [https://getsongbpm.com](https://getsongbpm.com).

---

## Where this is going

**It is becoming a web app.** The desktop build works and is not going away
yet, but the interface is moving to the browser in two deliberate steps:

1. **Now — the web UI, served locally.** A small HTTP layer over the existing
   engine, with the four panes rebuilt in the browser at `localhost`. The Qt
   window stays usable until the browser version matches it.
2. **Then — hosted, off your machine.** The same server deployed, so nothing
   runs on your PC and you can reach it from a phone.

Doing it in that order is not caution for its own sake: the whole interface
gets rewritten and proven before hosting, secrets, and OAuth redirects are
added on top. Two hard problems, one at a time.

### What this costs, honestly

**The database does not disappear — it moves.** That is forced, not a
preference. Spotify has not returned BPM or key since November 2024, so every
value comes from GetSongBPM, AcousticBrainz and Deezer, and AcousticBrainz runs
through MusicBrainz at one request per second. Without a cache, a 20-track set
would mean minutes of live lookups and would re-trigger the rate limits that
have already cost this project a nine-hour lockout. Hosted, the file simply sits
on the server instead of in `%LOCALAPPDATA%`.

Hosting also **fixes** something. A full enrichment pass takes about eight
hours; on a server it runs to completion instead of dying whenever the machine
sleeps or a shell tears it down.

Two constraints come with going hosted, both worth knowing before that step:
the Spotify client ID and GetSongBPM key move onto a server, and this app is
registered in Spotify **development mode**, which caps it at roughly 25 users
and requires the owner to hold Premium. Fine for personal use; it cannot be
shared publicly without Spotify's extension approval.

### Why the move is cheap

Nothing outside `ui/` knows Qt exists — layer 3 never imports layer 1, which
the brief required from the start and which turns out to be exactly what makes
this affordable:

| | lines | fate |
|---|---:|---|
| `ui/` (PySide6) | 2,028 | replaced by the browser front end |
| everything else | 5,509 | carries over untouched |

The sequencer, Camelot conversion, genre filter, Spotify client, all three
enrichment sources and the database layer move across as they are. The
rewrite is a new front end, not a new program.

---

## Status

All nine build-order steps of the original desktop brief are implemented.
**453 tests, no network calls.** The web interface is in progress.

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

And the web build, in order:

| Step | | |
|---|---|---|
| W1 | HTTP layer over the existing engine (`djset serve`) | done |
| W2 | Browser front end: the four panes | done |
| W3 | Generate + save, driven from the browser | done |
| W4 | Sync and enrich as background jobs with live progress | done |
| W5 | OAuth against a non-loopback redirect | |
| W6 | Deploy, with the database and secrets on the server | |

W5 and W6 are deliberately last. Everything before them runs at `localhost`
with the loopback redirect that already works, so the interface can be
finished and used before hosting is introduced.

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
| DSP (40) | BPM + key | analyses the audio | **100%** of anything with a preview |

Spotify's own `/audio-features` and `/audio-analysis` are **not** an option:
both return `403` for all applications since November 2024. Verified against a
live token; the bare 403 with no message body is the deprecation signature, not
a scope problem.

**Catalogue coverage decays with release date, and that is structural.**
Resolution rate by year, measured across this library:

```
pre-2015  89%   2018  77%   2021  42%   2024   2%
2015      88%   2019  68%   2022  17%   2025   1%
2016      84%   2020  52%   2023   0%   2026   1%
2017      76%
```

The cliff is February 2022, when AcousticBrainz stopped accepting submissions.
It is a frozen archive, so the gap widens with every release: a library of
classics resolves around 85-90%, a library of current music around 5%. Nothing
about a user's setup explains that — it is purely a function of when their
music came out.

`DSPSource` is the answer, and the only one. It fetches the 30-second preview
and *measures* the audio, so it always produces an answer and its coverage does
not decay. On 14 post-2023 tracks no catalogue could resolve, it answered 14.

It is off by default (`djset enrich --dsp`) because it downloads audio and
costs about 2.5 seconds of CPU per track — enabling it changes what a run costs,
not just what it finds. Measured against GetSongBPM as curated truth on 22
tracks: BPM usable 77% (41% exact, 18% inside the sequencer's tolerance, 18%
half/double, which the sequencer absorbs anyway); of the keys it reported, 74%
were exact or adjacent on the Camelot wheel, and adjacent still mixes. A key
whose winning profile does not clearly beat the runner-up is dropped rather
than guessed, so the tempo is returned alone. Audio is streamed to a temp file,
measured, and deleted — the preview is a means of measurement, not a download.

AcousticBrainz stopped accepting submissions in 2022 but still serves its
dataset. It is the only free source found that supplies harmonic data on an
exact join. Because it needs two hops and MusicBrainz enforces one request per
second, it dominates the runtime of a full pass — `djset enrich
--no-acousticbrainz` skips it. Its key is only trusted above an Essentia
`key_strength` of 0.5; below that the tempo is kept and the key dropped, since
a missing key costs one track while a wrong key corrupts every transition it
takes part in. That floor is measured: over 26 analyses the median strength was
0.64 and only 11.5% fell below it.

**Spotify has removed artist genres — settled 19 Aug 2026.** This sat open for
most of the project because two explanations fitted the evidence equally well:
the field was gone for everyone, or the request was wrong. They called for
opposite responses, so it was left alone until it could be checked properly.

It is gone. The artist object no longer *contains* the field — not an empty
array, absent. Across five artists including Lana Del Rey the response carried
only:

```
external_urls, href, id, images, name, type, uri
```

So the genre filter and Split-by-genre have no data source, and no amount of
syncing will produce one. The code degrades cleanly — everything falls into the
`Unknown` bucket and "no filter" stays the default path — and the pane now says
the field was removed rather than suggesting a fetch that cannot work.

GetSongBPM's artist search does return genres and is the natural substitute;
`GetSongBPMSource.artist_genres()` already parses them and is not yet wired in.

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

**GetSongBPM's rate limit is unknown, and `GETSONGBPM_RATE_PER_HOUR=2400` is a
guess.** It is the largest single spacing in the enrichment chain at 1.5s per
track, so it dominates the runtime of a full pass and is the obvious thing to
raise — but there is nothing to raise it *against*. Successful responses carry
no `X-RateLimit-*`, no quota and no `Retry-After` header, and `getsongbpm.com/api`
returns `403` to any programmatic fetch, so the published policy is not readable
either. The remaining option is probing by burst until it refuses, which risks
the key for a number that only buys wall-clock time. Left conservative
deliberately; raise it via the env var if you learn the real figure.

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
djset ui         # open the desktop window
```

`djset enrich` is the slow one — roughly eight hours for a 10,000-track
library, dominated by MusicBrainz's one-request-per-second ceiling. It commits
every result as it lands, so Ctrl+C is safe and re-running resumes rather than
restarts. Run it in your own terminal: it is long enough that anything which
reaps background processes will interrupt it.

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

Produces `dist/djset/djset.exe` — a 13.9 MB launcher beside a 158 MB
`_internal`. One-folder rather than one-file: a one-file build unpacks Qt to a
temp directory on every launch, which is slow and trips some antivirus. The
database lives in the OS app-data directory, so the program folder stays
read-only and portable.

Verified end to end rather than by the build exiting 0 — the packaged binary
launches, survives, writes nothing to stderr, and creates a fresh schema from
the bundled `schema.sql`, which is the part a broken bundle gets wrong:

```bash
QT_QPA_PLATFORM=offscreen DJSET_DB_PATH=/tmp/probe.sqlite3 dist/djset/djset.exe
```

The spec earns its keep in three places, all of which the build confirms:
`schema.sql` is read at runtime rather than imported, so it needs an explicit
`datas` entry; `keyring` resolves its backend at runtime and needs
`hiddenimports`; and PySide6 ships far more than this app uses, so the bundle
carries only QtCore, QtGui, QtNetwork and QtWidgets.

### If the build fails at the final EXE step

Seen once on this machine, and not since:

```
FileNotFoundError: [WinError 2] ... build\djset\djset.exe
```

`djset.pkg` (~13 MB) gets written but `djset.exe` is created and then vanishes.
That is antivirus quarantining the PyInstaller bootloader — a very common false
positive, because the bootloader is the same stub a lot of real malware uses.
Avast is active here and is already known to intercept HTTPS on this machine.

If it recurs, the fix is an antivirus exclusion for the build output folders
(`dj-set-builder\build` and `dj-set-builder\dist`). Packaging is in any case
optional: `djset ui` runs the app directly from source, which is the normal way
to use it during development.

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
| `djset crosscheck` | ask a second source about tracks that already have data and report disagreements. Writes nothing. `--against deezer` (default) needs no key and avoids MusicBrainz's 1 req/s; `--against acousticbrainz` compares keys but must not run alongside a full `enrich`. `--tolerance` is the BPM tolerance *you* build sets at, since that is what decides which disagreements can actually damage a set |
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
