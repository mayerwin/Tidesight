-- Tidesight events.db, migration 1: the statements of EVENTS_MIGRATIONS[0] in
-- server/python/tidesight/storage.py, verbatim. Do not edit here: change storage.py, then copy.
-- Apply with `wrangler d1 migrations apply` (see ../../README.md). The database's schema version
-- is the number of the last file applied (PRAGMA user_version 1 in a SQLite export).

CREATE TABLE IF NOT EXISTS raw_requests (
            id            INTEGER PRIMARY KEY,
            received_at   INTEGER NOT NULL,
            site_id       TEXT,
            site_status   TEXT NOT NULL,
            endpoint      TEXT NOT NULL,
            method        TEXT NOT NULL,
            ip            TEXT,
            headers       TEXT NOT NULL,
            cookies       TEXT,
            query         TEXT,
            content_type  TEXT,
            body_bytes    INTEGER NOT NULL DEFAULT 0,
            body_encoding TEXT,
            envelope      TEXT,
            body          BLOB,
            error         TEXT,
            proto_v       INTEGER,
            event_count   INTEGER NOT NULL DEFAULT 0,
            server        TEXT
        );

CREATE INDEX IF NOT EXISTS raw_requests_time ON raw_requests(received_at);

CREATE TABLE IF NOT EXISTS raw_hits (
            id          INTEGER PRIMARY KEY,
            request_id  INTEGER NOT NULL,
            seq         INTEGER NOT NULL,
            received_at INTEGER NOT NULL,
            site_id     TEXT,
            site_ok     INTEGER NOT NULL,
            type        TEXT,
            name        TEXT,
            client_ts   INTEGER,
            event_id    TEXT,
            visitor_id  TEXT,
            session_id  TEXT,
            page_id     TEXT,
            user_id     TEXT,
            url         TEXT,
            data        TEXT NOT NULL
        );

CREATE INDEX IF NOT EXISTS raw_hits_site_time ON raw_hits(site_id, received_at);

CREATE INDEX IF NOT EXISTS raw_hits_request ON raw_hits(request_id);

CREATE TABLE IF NOT EXISTS rate_limits (
            key    TEXT NOT NULL,
            minute INTEGER NOT NULL,
            hits   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key, minute)
        ) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS counters (
            day     TEXT NOT NULL,
            site_id TEXT NOT NULL,
            name    TEXT NOT NULL,
            n       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, site_id, name)
        ) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS spool_progress (
            path   TEXT PRIMARY KEY,
            offset INTEGER NOT NULL,
            done   INTEGER NOT NULL DEFAULT 0
        );

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

