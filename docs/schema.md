# Database schema

Tidesight keeps four SQLite files in `server.data_dir`. Their schemas are created and upgraded by numbered migrations, applied automatically and idempotently, and tracked with `PRAGMA user_version`. Every connector creates exactly the same `events.db` and `replay.db` schema (checked by the conformance suite).

| File | Written by | Purpose | Migrations |
|---|---|---|---|
| `events.db` | collector, `load-spool` | Raw requests and hits: the source of truth. | `EVENTS_MIGRATIONS` in `server/python/tidesight/storage.py` |
| `replay.db` | collector, `load-spool` | Heavy chunks: session replay, mouse movement, DOM snapshots. | `REPLAY_MIGRATIONS` in `storage.py` |
| `derived.db` | `jobs.py enrich` / `rollup` | Everything computed. Disposable: delete it and run `jobs.py enrich --rebuild`. | `DERIVED_MIGRATIONS` in `server/python/tidesight/enrich/db.py` |
| `web.db` | the dashboard | Sign-in sessions, the sign-in throttle, segments, annotations, the purge queue. Deleting it signs everyone out and forgets segments and notes. | `MIGRATIONS` in `server/python/tidesight/webdb.py` |

Conventions: timestamps are UTC milliseconds (integers); JSON is stored as text; booleans are 0/1 integers.

## events.db

### `raw_requests` — one row per HTTP request to `/collect` or `/p.gif`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `received_at` | INTEGER | Server receive time. |
| `site_id` | TEXT | Site id claimed by the request (even when unknown). |
| `site_status` | TEXT | `ok`, `unknown_site`, `origin_mismatch`, `no_origin`, `missing_site`, `invalid` (unparseable), `too_large`. Only `ok` traffic appears in reports. |
| `endpoint` | TEXT | `collect`, `pixel`, or `spool` for corrupt spool lines. |
| `method` | TEXT | HTTP method. |
| `ip` | TEXT | `REMOTE_ADDR` as received; truncated or `h:`-hashed when a privacy switch says so. |
| `headers` | TEXT | JSON object of all request headers, lower-case names; secrets redacted (`[redacted]`), `Cookie` removed (see `cookies`), `referer` with the secrets in its URL redacted (`redact_params`, `redact_paths`). |
| `cookies` | TEXT | JSON `{"kept": {ts_* cookies}, "others": count}`. |
| `query` | TEXT | Raw query string. |
| `content_type` | TEXT | |
| `body_bytes` | INTEGER | Body size on the wire (declared size when too large). |
| `body_encoding` | TEXT | `identity` or `gzip`. |
| `envelope` | TEXT | JSON payload without `events`. |
| `body` | BLOB | Raw body, only when it could not be parsed (or `server.store_raw_body`). |
| `error` | TEXT | Why the request is flagged (`bad_json: …`, `bad_gzip: …`, `too_large`, `bad_events:N`, …); NULL when fine. |
| `proto_v` | INTEGER | Payload `v`. |
| `event_count` | INTEGER | Hits stored for this request. |
| `server` | TEXT | JSON: connector name and version, scheme, path, server protocol and TLS variables when exposed. |
| `geo` | TEXT | *(migration 2)* Privacy annotation: the geolocation derived before the IP was anonymised, `{provider: record}`. |

Indexes: `received_at`.

### `raw_hits` — one row per event of a batch

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Monotonic: enrichment's watermark. |
| `request_id` | INTEGER | → `raw_requests.id`. |
| `seq` | INTEGER | Position in the batch. |
| `received_at` | INTEGER | Copy of the request's receive time. |
| `site_id` | TEXT | Copy of the request's site id. |
| `site_ok` | INTEGER | 1 when the request's `site_status` is `ok`. |
| `type` | TEXT | Event `t` (≤ 32 chars), `_invalid` when not an object or without a type. |
| `name` | TEXT | Event `n` when it is a string (≤ 200 chars), else NULL. |
| `client_ts` | INTEGER | Event `ts`. |
| `event_id` | TEXT | Event `id` (≤ 64): retries are de-duplicated on it at enrichment. |
| `visitor_id`, `session_id` | TEXT | Event `vid`/`sid`, else the envelope's. |
| `page_id` | TEXT | Event `pid`. |
| `user_id` | TEXT | Event `uid`, else the envelope's. |
| `url` | TEXT | Event `url` (≤ 2048; the full value stays in `data`). |
| `data` | TEXT | The event exactly as received (JSON). |

Indexes: `(site_id, received_at)`, `request_id`.

### Other tables

| Table | Purpose |
|---|---|
| `rate_limits(key, minute, hits)` | Per-minute allowances used (pruned after an hour, in batches; *migration 3* indexes `minute`): `r:<id>` requests, `kb:<id>` kilobytes, `f:<id>` and `*f` flagged requests (`docs/configuration.md`, "Limits and abuse protection"). `<id>` is a keyed hash of the client (`h:` + 16 hex digits), never its address. |
| `counters(day, site_id, name, n)` | Operational counters per UTC day, under a configured site or `""`: `hits` (stored), `rate_limited`, `chunk_rate_limited`, `flagged_limited`, `over_quota`, `chunk_too_large`, `dropped_dnt`, `dropped_gpc`. |
| `spool_progress(path, offset, done)` | How far each spool file has been loaded. |
| `meta(key, value)` | Reserved. |

## replay.db

### `chunks`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `received_at` | INTEGER | |
| `site_id`, `site_ok` | TEXT, INTEGER | As for hits. |
| `kind` | TEXT | `replay`, `mouse`, `snapshot`; `_invalid` for an unknown `k`. |
| `visitor_id`, `session_id`, `page_id` | TEXT | From the query string. |
| `seq`, `client_ts` | INTEGER | From the query string. |
| `url` | TEXT | Page URL (≤ 2048). |
| `ip` | TEXT | As for requests (anonymised when configured). |
| `meta` | TEXT | JSON: every query parameter (including `rec`), `bytes`, `sent_encoding`, user agent, origin, referer. |
| `encoding` | TEXT | Always `gzip` (bodies sent uncompressed are compressed at ingest; lossless). |
| `payload` | BLOB | The chunk body. |
| `error` | TEXT | `unknown_kind`, `empty_body`. |

Indexes: `(site_id, session_id, page_id, seq)`, `received_at`. Also `rate_limits` and `spool_progress` as in events.db (*migration 2* indexes `rate_limits(minute)`).

## derived.db

Rebuilt from the raw data whenever the pipeline version changes (parsers, geo databases, algorithms or the site settings that affect them): a new file is built and swapped in, carrying over the daily rollups of days whose raw data was deleted by retention.

| Table | Contents |
|---|---|
| `job_state(job, version, last_id, updated_at, info)` | Enrichment watermark (last processed `raw_hits.id`) and version. |
| `ua` | One row per distinct user agent + Client Hints: browser, OS, device type/brand/model, webview, in-app, bot UA. |
| `ip_geo(ip, provider, …)` | Geolocation per IP and provider (MaxMind, DB-IP, IPinfo): country, region, city, coordinates, time zone, ASN, network. |
| `requests` | Per raw request: decided client IP and its source header, UA id, cookieless daily hash, clock skew, retry count, internal flag. |
| `hits` | Per raw hit: session key, page key, effective time (client time corrected for clock skew), duplicate flag, and the fields sessions are built from. |
| `event_ids`, `px_sessions` | De-duplication of retried events; pixel-hit sessions. |
| `sessions` | One row per visit: timing, engagement, entry/exit, attribution (source, medium, campaign, GA4 channel, referrer, category), geo, device, screen, language, bot verdict and reasons, internal flag, identity keys, replay/error/rage counts, `day` and `hour` in the site's time zone. A session of more than 20,000 hits is no person's visit: it is built from its first 20,000 hits, marked as a bot (`too_many_hits`) and not rebuilt again, so a flood cannot make enrichment slow. |
| `pageviews` | One row per pageview: path, title, referrer, navigation type, status, engaged time, scroll depth, time on page, entry/exit. |
| `events`, `clicks`, `vitals`, `perf`, `errors`, `forms`, `searches`, `misc` | Typed copies of the corresponding hits (non-duplicates), with `day` and `path`. Re-sent vitals keep the last value per page and metric; error count updates merge into the first occurrence. |
| `id_edges`, `vid_map`, `visitors` | Identity stitching: evidence linking visitor ids, the visitor key and confidence of each id, and per-visitor summaries. |
| `dirty_days`, `daily`, `daily_dim` | Rollups: days to recompute; daily totals and per-dimension aggregates of clean traffic (configured site, allowed origin, not a bot, not internal). |

Migration 2 adds `session_key` indexes on the typed tables and `pageviews(site_id, path, day)`. Migration 3 adds `vitals(page_key, metric)` and `errors(page_key, grp)`, used when a re-sent vital or an error count updates its page's row.

## web.db

The dashboard's own state, apart from the analytics data (only the Python app reads and writes it).

| Table | Contents |
|---|---|
| `sessions(token_hash, user, pw_tag, created, expires)` | Sign-in sessions: the SHA-256 of the `ts_login` cookie (never the token), the user, a digest of the user's password hash at sign-in (the session ends when it changes), creation and expiry. At most 100 per user. |
| `throttle(client, failures, since)` | Failed sign-ins and share-token guesses per client key (the same keyed hash as the collector's allowances), within the 10-minute window; bounded to 20,000 rows. |
| `segments(id, site_id, name, filters, created_by, created_at)` | Saved filter sets; `site_id` NULL for every property. `filters` is the API's JSON list. |
| `annotations(id, site_id, day, text, created_by, created_at)` | Notes shown on a property's charts. |
| `purges(site_id, requested_by, requested_at)` | *(migration 2)* Properties deleted with their data: `jobs.py cron` deletes everything stored under the id (raw and archived hits, recordings, derived rows, segments, notes), then the row. |

Properties added in the dashboard are not in a database: they are `data_dir/properties.json` (`{"format": "tidesight-properties/1", "sites": {id: table}}`), loaded with the configuration file.

## Adding a migration

1. Append `(N, [statements])` to the relevant list; never edit a migration that may have run somewhere.
2. Keep statements idempotent where SQLite allows (`IF NOT EXISTS`). `ALTER TABLE … ADD COLUMN` cannot say that, so the runner skips it when the column exists: a database whose `user_version` was lost (a Cloudflare D1 export) migrates cleanly.
3. For `events.db`/`replay.db`, update the PHP, Node and Worker connectors identically; the conformance suite compares their schema with Python's.
4. Document the change here.
