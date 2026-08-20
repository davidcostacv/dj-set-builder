PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tracks (
  spotify_id   TEXT PRIMARY KEY,
  uri          TEXT NOT NULL,        -- from the playlist-read response, NOT from search
  isrc         TEXT,                 -- primary join key for enrichment
  title        TEXT NOT NULL,
  artist       TEXT NOT NULL,        -- display form, joined with ", "
  artist_ids   TEXT,                 -- JSON array
  artist_names TEXT,                 -- JSON array, parallel to artist_ids
  album        TEXT,
  duration_ms  INTEGER,
  added_at     TEXT
);

CREATE TABLE IF NOT EXISTS artists (
  spotify_id  TEXT PRIMARY KEY,
  name        TEXT,
  genres      TEXT,                  -- JSON array; artist-level only
  fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS genre_aliases (
  raw         TEXT PRIMARY KEY,      -- 'trap latino', 'reggaeton urbano'
  canonical   TEXT NOT NULL          -- 'reggaeton'
);

CREATE TABLE IF NOT EXISTS audio_features (
  spotify_id   TEXT PRIMARY KEY REFERENCES tracks(spotify_id),
  bpm          REAL,
  key_camelot  TEXT,                 -- '8A', '11B', etc.
  key_open     TEXT,                 -- GetSongBPM open key notation
  energy       REAL,                 -- 0-1, nullable, source-dependent
  source       TEXT NOT NULL,        -- 'rekordbox' | 'getsongbpm' | 'manual' | 'dsp'
  key_source   TEXT,                 -- where key_camelot came from, when not `source`
  confidence   REAL,
  fetched_at   TEXT
);

CREATE TABLE IF NOT EXISTS enrichment_misses (
  spotify_id  TEXT PRIMARY KEY,
  attempts    INTEGER DEFAULT 1,
  last_try    TEXT,
  reason      TEXT
);

CREATE TABLE IF NOT EXISTS playlists_cache (
  spotify_id  TEXT PRIMARY KEY,
  name        TEXT,
  snapshot_id TEXT,                  -- skip re-syncing if unchanged
  track_count INTEGER,
  synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS exports (
  id           INTEGER PRIMARY KEY,
  playlist_id  TEXT,
  content_hash TEXT,                 -- SHA256 of ordered URI list
  name         TEXT,
  created_at   TEXT,
  -- Identity is the set *and* the name. The hash alone stops a double-click
  -- creating two identical playlists, which is what the guard is for -- but it
  -- also silently returned the old playlist when someone rebuilt the same set
  -- and gave it a real name, so the name they typed was discarded. A different
  -- name is a different intent.
  UNIQUE (content_hash, name)
);

-- Membership: which tracks belong to which source playlist. Not in the spec's
-- table list, but pane 1 is a multi-select over playlists and the eligible set
-- is the union of the selected ones, so the join has to live somewhere.
CREATE TABLE IF NOT EXISTS playlist_tracks (
  playlist_id  TEXT NOT NULL,
  spotify_id   TEXT NOT NULL REFERENCES tracks(spotify_id),
  position     INTEGER,
  added_at     TEXT,
  PRIMARY KEY (playlist_id, spotify_id)
);

CREATE INDEX IF NOT EXISTS idx_audio_features_bpm  ON audio_features(bpm);
CREATE INDEX IF NOT EXISTS idx_audio_features_key  ON audio_features(key_camelot);
CREATE INDEX IF NOT EXISTS idx_tracks_isrc         ON tracks(isrc);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_pid ON playlist_tracks(playlist_id);
