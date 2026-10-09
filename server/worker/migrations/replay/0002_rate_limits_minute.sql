-- Tidesight replay.db, migration 2: the statements of REPLAY_MIGRATIONS[1] in
-- server/python/tidesight/storage.py, verbatim. Do not edit here: change storage.py, then copy.
-- Apply with `wrangler d1 migrations apply` (see ../../README.md). The database's schema version
-- is the number of the last file applied (PRAGMA user_version 2 in a SQLite export).
-- Old allowance minutes are deleted by minute: without an index, a flood's rows made that a full scan.

CREATE INDEX IF NOT EXISTS rate_limits_minute ON rate_limits(minute);
