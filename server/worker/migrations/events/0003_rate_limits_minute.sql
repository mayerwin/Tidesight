-- Tidesight events.db, migration 3: the statements of EVENTS_MIGRATIONS[2] in
-- server/python/tidesight/storage.py, verbatim. Do not edit here: change storage.py, then copy.
-- Apply with `wrangler d1 migrations apply` (see ../../README.md). The database's schema version
-- is the number of the last file applied (PRAGMA user_version 3 in a SQLite export).
-- Old allowance minutes are deleted by minute: without an index, a flood's rows made that a full scan.

CREATE INDEX IF NOT EXISTS rate_limits_minute ON rate_limits(minute);
