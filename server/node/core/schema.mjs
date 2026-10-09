/**
 * The raw-storage schema: a verbatim copy of EVENTS_MIGRATIONS and REPLAY_MIGRATIONS in
 * server/python/tidesight/storage.py. Each entry is [version, statements]; a database's PRAGMA user_version
 * records the last migration applied. The Worker's D1 migration files (server/worker/migrations/) hold the
 * same statements. Change the schema in storage.py first, then here and there (the conformance suite compares).
 */

export const EVENTS_MIGRATIONS = [
  [1, [
    `CREATE TABLE IF NOT EXISTS raw_requests (
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
        )`,
    "CREATE INDEX IF NOT EXISTS raw_requests_time ON raw_requests(received_at)",
    `CREATE TABLE IF NOT EXISTS raw_hits (
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
        )`,
    "CREATE INDEX IF NOT EXISTS raw_hits_site_time ON raw_hits(site_id, received_at)",
    "CREATE INDEX IF NOT EXISTS raw_hits_request ON raw_hits(request_id)",
    `CREATE TABLE IF NOT EXISTS rate_limits (
            key    TEXT NOT NULL,
            minute INTEGER NOT NULL,
            hits   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key, minute)
        ) WITHOUT ROWID`,
    `CREATE TABLE IF NOT EXISTS counters (
            day     TEXT NOT NULL,
            site_id TEXT NOT NULL,
            name    TEXT NOT NULL,
            n       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, site_id, name)
        ) WITHOUT ROWID`,
    `CREATE TABLE IF NOT EXISTS spool_progress (
            path   TEXT PRIMARY KEY,
            offset INTEGER NOT NULL,
            done   INTEGER NOT NULL DEFAULT 0
        )`,
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
  ]],
  // Privacy annotations: the geolocation derived before an IP was anonymised (JSON {provider: record}).
  [2, [
    "ALTER TABLE raw_requests ADD COLUMN geo TEXT",
  ]],
];

export const REPLAY_MIGRATIONS = [
  [1, [
    `CREATE TABLE IF NOT EXISTS chunks (
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
        )`,
    "CREATE INDEX IF NOT EXISTS chunks_session ON chunks(site_id, session_id, page_id, seq)",
    "CREATE INDEX IF NOT EXISTS chunks_time ON chunks(received_at)",
    `CREATE TABLE IF NOT EXISTS rate_limits (
            key    TEXT NOT NULL,
            minute INTEGER NOT NULL,
            hits   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key, minute)
        ) WITHOUT ROWID`,
    `CREATE TABLE IF NOT EXISTS spool_progress (
            path   TEXT PRIMARY KEY,
            offset INTEGER NOT NULL,
            done   INTEGER NOT NULL DEFAULT 0
        )`,
  ]],
];

// The write statements shared by the SQLite and D1 stores.
export const INSERT_REQUEST_COLUMNS = "received_at, site_id, site_status, endpoint, method, ip, headers, cookies, " +
  "query, content_type, body_bytes, body_encoding, envelope, body, error, proto_v, event_count, server";
export const INSERT_HIT_COLUMNS = "request_id, seq, received_at, site_id, site_ok, type, name, client_ts, " +
  "event_id, visitor_id, session_id, page_id, user_id, url, data";
export const INSERT_CHUNK_COLUMNS = "received_at, site_id, site_ok, kind, visitor_id, session_id, page_id, seq, " +
  "client_ts, url, ip, meta, encoding, payload, error";
