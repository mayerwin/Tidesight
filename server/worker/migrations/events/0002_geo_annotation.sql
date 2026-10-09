-- Tidesight events.db, migration 2: the statements of EVENTS_MIGRATIONS[1] in
-- server/python/tidesight/storage.py, verbatim. Do not edit here: change storage.py, then copy.
-- Apply with `wrangler d1 migrations apply` (see ../../README.md). The database's schema version
-- is the number of the last file applied (PRAGMA user_version 2 in a SQLite export).

ALTER TABLE raw_requests ADD COLUMN geo TEXT;

