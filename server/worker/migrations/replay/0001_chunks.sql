-- Tidesight replay.db, migration 1: the statements of REPLAY_MIGRATIONS[0] in
-- server/python/tidesight/storage.py, verbatim. Do not edit here: change storage.py, then copy.
-- Apply with `wrangler d1 migrations apply` (see ../../README.md). The database's schema version
-- is the number of the last file applied (PRAGMA user_version 1 in a SQLite export).

CREATE TABLE IF NOT EXISTS chunks (
            id          INTEGER PRIMARY KEY,
            received_at INTEGER NOT NULL,
            site_id     TEXT,
            site_ok     INTEGER NOT NULL,
            kind        TEXT NOT NULL,
            visitor_id  TEXT,
            session_id  TEXT,
            page_id     TEXT,
            seq         INTEGER,
            client_ts   INTEGER,
            url         TEXT,
            ip          TEXT,
            meta        TEXT NOT NULL,
            encoding    TEXT NOT NULL,
            payload     BLOB NOT NULL,
            error       TEXT
        );

CREATE INDEX IF NOT EXISTS chunks_session ON chunks(site_id, session_id, page_id, seq);

CREATE INDEX IF NOT EXISTS chunks_time ON chunks(received_at);

CREATE TABLE IF NOT EXISTS rate_limits (
            key    TEXT NOT NULL,
            minute INTEGER NOT NULL,
            hits   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key, minute)
        ) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS spool_progress (
            path   TEXT PRIMARY KEY,
            offset INTEGER NOT NULL,
            done   INTEGER NOT NULL DEFAULT 0
        );

