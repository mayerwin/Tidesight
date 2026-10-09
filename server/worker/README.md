# Tidesight collector for Cloudflare Workers + D1

The ingest endpoints of [`docs/protocol.md`](../../docs/protocol.md) as a Cloudflare Worker. Raw hits go to two D1 databases with **exactly the schema of the Python collector**: `EVENTS` (events.db: `raw_requests`, `raw_hits`, `rate_limits`, `counters`, …) and `REPLAY` (replay.db: `chunks`). The request handling is the same runtime-neutral core as the Node connector (`../node/core/`), so the rows are the ones the Python collector would write.

## Deploy

You need a Cloudflare account, Node.js and [wrangler](https://developers.cloudflare.com/workers/wrangler/) (`npx wrangler …`). Run the commands from `server/worker/`.

1. **Create the databases** and put their ids in `wrangler.toml`:

   ```sh
   cp wrangler.toml.example wrangler.toml
   npx wrangler d1 create tidesight-events
   npx wrangler d1 create tidesight-replay
   ```

2. **Create the schema.** `migrations/events/` and `migrations/replay/` hold the statements of `EVENTS_MIGRATIONS` and `REPLAY_MIGRATIONS` from `server/python/tidesight/storage.py`, verbatim, one numbered file per migration:

   ```sh
   npx wrangler d1 migrations apply tidesight-events --remote
   npx wrangler d1 migrations apply tidesight-replay --remote
   ```

   `migrations apply` records what it ran in a `d1_migrations` table and applies only new files, so run it again after updating Tidesight. Without it, run each file once, in order: `npx wrangler d1 execute tidesight-events --remote --file migrations/events/0001_raw_requests_and_hits.sql`, then `0002_…`. The Worker does not create tables itself; until the migrations are applied it answers 503 and logs `no such table`.

3. **Configure.** Generate the resolved configuration from your TOML and store it as a secret, because it contains the deployment secret:

   ```sh
   python -m tidesight.jobs --config tidesight.toml config-json --out tidesight.json   # from server/python
   npx wrangler secret put TIDESIGHT_CONFIG < tidesight.json
   ```

   `server.data_dir`, `storage_mode`, `journal_mode` and the spool settings do not apply to D1. To bundle the configuration instead of using a secret, import its text with a wrangler `Text` rule and build the Worker with `createWorker({ config: text })` from `src/index.mjs`.

4. **Route and deploy.** Set `routes` in `wrangler.toml`: a custom domain such as `stats.example.com`, or a path of the site itself with `TIDESIGHT_BASE_PATH`. Then run `npx wrangler deploy`. Check `https://stats.example.com/health`.

5. **Tracker files.** Serve `tracker/build` with Workers static assets (the `[assets]` block in the example) or from any static host on the same domain. Without assets, `/ts.js` answers 404.

## Client IP: `CF-Connecting-IP`

Cloudflare terminates the visitor's TCP connection. The Worker never sees a socket, but Cloudflare sets `CF-Connecting-IP` to the address of the peer that connected to it, and a client cannot forge it. That is exactly what `REMOTE_ADDR` means for the other connectors, so it fills the `ip` column and is the `remote_addr` client key of the allowances (an IPv6 client counts as its `rate_limit_ipv6_prefix` network, /64 by default). Keep `rate_limit_key = "remote_addr"`: another header would be believed only if the visitor's own address were in `rate_limit_proxies`. The header is also stored, with every other header (`X-Forwarded-For`, `CF-IPCountry`, …), and nothing is trusted beyond that: enrichment chooses the visitor address as usual. As in every connector, secret headers are redacted and the `Referer`'s URL secrets are replaced by `redacted`, with the site's `redact_params` and `redact_paths`. The `server` column records Cloudflare's view of the connection: `server_protocol`, the TLS version and cipher, and a `cf` object with the colo, country, ASN and RTT. Nothing finer than the country is kept, since the precise location can be derived from the stored IP.

**Pseudo IPv4.** With the zone's Pseudo IPv4 setting on "Overwrite headers", Cloudflare replaces an IPv6 visitor's address in `CF-Connecting-IP` with a stand-in from the reserved 240.0.0.0/4 range, and puts the real one in `CF-Connecting-IPv6`. The Worker then uses `CF-Connecting-IPv6`, so such visitors keep their own address and allowance (otherwise many of them could share a stand-in). This follows Cloudflare's documentation; it has not been tested on a zone with the setting on.

## Limits

D1's limits (<https://developers.cloudflare.com/d1/platform/limits/>) shape the Worker:

- **Database size: 10 GB** per database on the Paid plan, 500 MB on Free. Raw data only grows, so plan exports and retention. `replay.db` is separate so that heavy chunks do not fill `events.db`. A site's `max_hits_per_day` (off by default) bounds what a flood can add, at the price of refusing real visitors once a flood has used it up; consider it on the Free plan.
- **2 MB per row, string or BLOB.** The Worker caps `max_payload_kb`, `max_decompressed_kb` and `max_chunk_kb` at 1800 KB and logs a warning when the configuration asks for more; `/config.js` then announces the capped `max_kb` (with its own `cv`), so trackers keep their requests under it. A larger body is flagged like any oversize request (`413`, or `bad_gzip: … exceeds …` with the raw body kept). A row that would still exceed 2 MB (an envelope or event that grows when re-serialised, raw bodies kept with `store_raw_body`) is never sent to D1: the request is stored as its metadata only, like an oversize body (`site_status` and `error` `too_large`, no body, envelope or hits), charged to its allowances as it was, and answered `413`, which the tracker does not retry. A chunk row that would not fit is answered `413` and counted as `chunk_too_large`. With the default allowances this is rare: such a request is charged more kilobytes than `rate_limit_kb_per_minute` allows, so it is refused first (`429`).
- **100 bound parameters and 100 KB of SQL per statement.** The events of a batch are therefore passed as one JSON array expanded with `json_each`, split into groups below 1 MB. A request costs about ten statements whatever its size, far below the per-invocation query limit (50 on Free, 1,000 on Paid).
- **CPU time.** Measured on a desktop CPU, the collector spends about 0.3 ms on a typical batch (up to 50 events), 1 to 3 ms on 100 to 250 KB of text-heavy JSON, and about 18 ms on the largest batch the default `max_decompressed_kb` (256 KB) admits when it is dense (numbers and small objects; 650 KB of such JSON takes about 90 ms). The Free plan allows 10 ms per request: keep `max_decompressed_kb` at its default or lower there; a batch over the limit fails and the tracker retries it. On the Paid plan, cap the CPU time a crafted body can use with `[limits] cpu_ms` (see `wrangler.toml.example`). A client over its allowance is refused before its body is decompressed or parsed (see below), so repeating such a body costs little.

### Limits and abuse protection

The allowances, quota and flags of [`docs/configuration.md`](../../docs/configuration.md) ("Limits and abuse protection") apply exactly as in the Python collector, with the same counters, status codes and charged sizes: per-client requests and kilobytes per minute (separately for `/chunk`), the flagged-traffic allowances (per client and for everyone), early refusal of a client already over its allowance (before its body is parsed), counter-only requests (DNT/GPC drops, oversize chunks) within the client's request allowance, each site's daily `max_hits_per_day` (off by default), chunk kinds gated by the site's features, `max_events_per_request` and the 64-level JSON nesting limit. The allowance keys hold the client hashed with the deployment secret (`r:h:3f2a…`), never its address. `min_free_disk_mb` does not apply: D1 manages its own storage.

**Why the allowances use D1 rows.** A Worker runs in many isolates in many locations, and an isolate's memory is neither shared nor durable, so in-memory counters alone (the Python spool's approach) would limit almost nothing. The schema's `rate_limits` and `counters` tables give one global count per key and minute, and per site and day. D1 has no interactive transactions, so the decisions Python takes inside its transaction are written into the SQL of the request's single batch: one upsert per allowance (`r:`, `kb:`, `f:` and `*f` keys, so each has its own row), each only while every earlier one is still within its limit (Python stops at the first one exceeded); a `SELECT` of the refusal (the first allowance over its limit, else the quota); then the refusal counter, the request and hit rows and the `hits` counter, each conditional on that same expression, which the batch's later statements do not change. A counter-only request is the same: the `r:` upsert and the counter, written only within it. Each batch is atomic and needs no extra service (Durable Object, KV or the rate-limiting binding). If a batch fails, its allowances are charged by a batch of their own before the Worker answers `503`, so a request D1 fails to store still counts against them. A refused chunk is counted in `EVENTS` after the `REPLAY` batch, as Python counts it in events.db after its replay.db transaction. Old minutes are pruned at most once a minute per database and isolate, 20,000 rows at a time.

**Refusals cost no D1 writes.** Each batch also returns the counts it saw, and the isolate remembers them for the current minute, with each site's stored hits for the day. A request those numbers already refuse (a client known to be over an allowance, a site known to be over its quota) is refused without D1: it is charged in memory only, and the early refusal works from the same numbers. Refusals that D1 finds are counted in their batch. Refusals found in memory are written at once for the first 5 of each client (or of each site's quota) and 50 in all per isolate and minute, so that small numbers stay exact; beyond that they are summed in memory and written at most once a minute, 20 counters at a time. A flood from one client therefore costs a few row writes per minute and isolate instead of two per request; an isolate that is shut down loses the sums it had not written yet.

**The Free plan's daily writes.** The Free plan allows 100,000 rows written per day, and a stored request writes about five (the request, its hits, two to four allowance rows and the `hits` counter). Refusals no longer use them up, but a flood that stays within each address's allowances, spread over many addresses, still can, and ingest then stops for every site until the next UTC day. On the Free plan, keep the allowances modest and set `max_hits_per_day` for each site; for real traffic, use the Paid plan.

### No spool fallback

A Worker has no filesystem, so there is no spool. A request is stored by one D1 batch, which is a transaction: entirely or not at all. When D1 fails or is unreachable, the Worker answers **503 with `Retry-After: 60`**. The tracker keeps the batch in `localStorage` and retries with backoff, so a D1 outage delays data instead of losing it. The exceptions are the no-JS pixel (`/p.gif`, which cannot be retried, as with the Python collector when its spool also fails) and visitors who leave before a retry.

## Enrichment and the dashboard

The Python jobs read SQLite files. Export the D1 databases and load them into a data directory:

```sh
npx wrangler d1 export tidesight-events --remote --output events.sql
npx wrangler d1 export tidesight-replay --remote --output replay.sql
sqlite3 data/events.db < events.sql && sqlite3 data/events.db "PRAGMA user_version = 3"
sqlite3 data/replay.db < replay.sql && sqlite3 data/replay.db "PRAGMA user_version = 2"
python -m tidesight.jobs --config tidesight.toml cron
```

Set `user_version` to the number of the last migration file applied (3 for events, 2 for replay today). `PRAGMA user_version` is not among the PRAGMAs D1 supports, so the migration files cannot set it, and an export does not carry it. The Python tools also cope without it (they re-run the migrations and skip an `ADD COLUMN` whose column exists), but setting it saves that step. Each export is a full copy, so for large databases export during quiet hours. An incremental sync tool is a natural next step.

## Local testing without wrangler

`test/serve.mjs` runs the Worker's fetch handler inside a Node HTTP server, with D1 emulated on `node:sqlite` (`test/d1-shim.mjs`). The shim enforces D1's parameter, statement and value limits, transactions only through `batch()`, and D1's PRAGMA list, so the Worker does not rely on anything D1 refuses.

```sh
node test/serve.mjs --port 8787 --config tidesight.json --data ./data
```

`DIR/events.db` and `DIR/replay.db` stand in for the two databases. The migration files are applied at start-up, and the last one applied is recorded as `PRAGMA user_version`, like the export above. The socket address is passed as `CF-Connecting-IP`. The conformance suite uses this server:

```sh
TIDESIGHT_CONNECTORS=worker python -m pytest server/python/tests/test_conformance.py -q   # from the repository root
node --test server/worker/test/redact.test.mjs server/worker/test/d1-store.test.mjs   # also part of the Node connector's npm test
```

`redact.test.mjs` runs the shared URL redaction vectors; `d1-store.test.mjs` checks that refusals cost no D1 writes, the early refusal, rows D1 cannot hold, failed batches and the Pseudo IPv4 address.

## Differences from the Python collector

The Node connector's list applies where it concerns the shared core (see `../node/README.md`: HTTP details, nesting). In addition:

- **`/health`** reports `"storage": "d1"`.
- **Path and query come from Cloudflare's URL.** A query character that the URL standard percent-encodes (a raw space, `"`, `<`, `>`) is stored encoded, and headers arrive combined and lower-cased as Cloudflare delivers them.
- **Gzip** is decoded by the platform's `DecompressionStream`, not zlib as in Python and the Node connector: a body of several gzip members, or with bytes after its member, is decoded as the platform decodes it (Python decodes the first member and ignores the rest), and errors inside the deflate data read `Error -3 while decompressing data: invalid compressed data` instead of zlib's specific text. Header errors and `truncated gzip data` match. The tracker always sends a single member.
- **Refusals made in memory** (above) are not charged to D1's `rate_limits` rows, so those rows are lower than Python's for a client refused that way, and a later isolate can allow such a client a little more. The refusal counts are the same once the summed ones are written. Each isolate's early refusal works from what that isolate has seen, so the first request over the allowance that an isolate sees is still parsed.
- **Rows D1 cannot hold** (2 MB) are stored as metadata and answered `413` (above); Python stores them.
- **Size caps.** The D1 caps on body sizes above apply, and `/config.js` announces the capped `max_kb`.
- **No spool.** Storage failures answer 503 (see above), and `storage_mode` is ignored.
- **No disk guard.** `min_free_disk_mb` is ignored; a full D1 database answers 503.
- **Tracker files** are served only through an `ASSETS` binding.
- **`rate_limits` pruning** runs per database and isolate, at most once a minute (Python's timer is per process, shared by both databases).
