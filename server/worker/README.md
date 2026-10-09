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

   `migrations apply` records what it ran in a `d1_migrations` table and applies only new files. Without it, run each file once, in order: `npx wrangler d1 execute tidesight-events --remote --file migrations/events/0001_raw_requests_and_hits.sql`, then `0002_…`. The Worker does not create tables itself; until the migrations are applied it answers 503 and logs `no such table`.

3. **Configure.** Generate the resolved configuration from your TOML and store it as a secret, because it contains the deployment secret:

   ```sh
   python -m tidesight.jobs --config tidesight.toml config-json --out tidesight.json   # from server/python
   npx wrangler secret put TIDESIGHT_CONFIG < tidesight.json
   ```

   `server.data_dir`, `storage_mode`, `journal_mode` and the spool settings do not apply to D1. To bundle the configuration instead of using a secret, import its text with a wrangler `Text` rule and build the Worker with `createWorker({ config: text })` from `src/index.mjs`.

4. **Route and deploy.** Set `routes` in `wrangler.toml`: a custom domain such as `stats.example.com`, or a path of the site itself with `TIDESIGHT_BASE_PATH`. Then run `npx wrangler deploy`. Check `https://stats.example.com/health`.

5. **Tracker files.** Serve `tracker/build` with Workers static assets (the `[assets]` block in the example) or from any static host on the same domain. Without assets, `/ts.js` answers 404.

## Client IP: `CF-Connecting-IP`

Cloudflare terminates the visitor's TCP connection. The Worker never sees a socket, but Cloudflare sets `CF-Connecting-IP` to the address of the peer that connected to it, and a client cannot forge it. That is exactly what `REMOTE_ADDR` means for the other connectors, so it fills the `ip` column and is the `remote_addr` rate-limit key. The header is also stored, with every other header (`X-Forwarded-For`, `CF-IPCountry`, …), and nothing is trusted beyond that: enrichment chooses the visitor address as usual. The `server` column records Cloudflare's view of the connection: `server_protocol`, the TLS version and cipher, and a `cf` object with the colo, country, ASN and RTT. Nothing finer than the country is kept, since the precise location can be derived from the stored IP.

## Limits

D1's limits (<https://developers.cloudflare.com/d1/platform/limits/>) shape the Worker:

- **Database size: 10 GB** per database on the Paid plan, 500 MB on Free. Raw data only grows, so plan exports and retention. `replay.db` is separate so that heavy chunks do not fill `events.db`.
- **2 MB per row, string or BLOB.** The Worker caps `max_payload_kb`, `max_decompressed_kb` and `max_chunk_kb` at 1800 KB and logs a warning when the configuration asks for more. A larger body is then flagged like any oversize request (`413`, or `bad_gzip: … exceeds …` with the raw body kept). With `store_raw_body = true`, keep `max_payload_kb` under about 900 so that body and envelope fit one row. A row that still exceeds 2 MB (a pathological event) cannot be stored: 503.
- **100 bound parameters and 100 KB of SQL per statement.** The events of a batch are therefore passed as one JSON array expanded with `json_each`, split into groups below 1 MB. A batch costs a handful of statements whatever its size, far below the per-invocation query limit (50 on Free, 1,000 on Paid).
- **CPU time.** Measured on a desktop CPU, the collector spends about 0.15 ms on a typical 3-event batch, 0.4 ms on 50 events, 2.6 ms on 100 KB of JSON and 17 ms on 650 KB. A default `max_payload_kb` of 64 KB of gzip can expand to more than the Free plan's 10 ms allows. On the Free plan, lower `max_decompressed_kb` (around 300); the Paid plan allows 30 s.

### Why rate limiting uses D1 rows

A Worker runs in many isolates in many locations, and an isolate's memory is neither shared nor durable, so an in-memory counter (the Python spool's approach) would limit almost nothing. The `rate_limits` table that the schema already has gives one global count per key and minute. It is updated in the same atomic batch as the insert: the batch upserts the counter, returns the new count, and inserts the hit rows only `WHERE` that count is within the limit, or else counts `rate_limited`. The result matches the Python collector's transaction, needs no extra service (Durable Object, KV or the rate-limiting binding) and costs one extra row write per request. Old minutes are pruned at most every 10 minutes per isolate.

### No spool fallback

A Worker has no filesystem, so there is no spool. Every request is one D1 batch, which is a transaction: it is stored entirely or not at all. When D1 fails or is unreachable, the Worker answers **503 with `Retry-After: 60`**. The tracker keeps the batch in `localStorage` and retries with backoff, so a D1 outage delays data instead of losing it. The exceptions are the no-JS pixel (`/p.gif`, which cannot be retried, as with the Python collector when its spool also fails) and visitors who leave before a retry.

## Enrichment and the dashboard

The Python jobs read SQLite files. Export the D1 databases and load them into a data directory:

```sh
npx wrangler d1 export tidesight-events --remote --output events.sql
npx wrangler d1 export tidesight-replay --remote --output replay.sql
sqlite3 data/events.db < events.sql && sqlite3 data/events.db "PRAGMA user_version = 2"
sqlite3 data/replay.db < replay.sql && sqlite3 data/replay.db "PRAGMA user_version = 1"
python -m tidesight.jobs --config tidesight.toml cron
```

Set `user_version` to the number of the last migration file applied (2 for events, 1 for replay today). `PRAGMA user_version` is not among the PRAGMAs D1 supports, so the migration files cannot set it, and an export does not carry it. The Python tools also cope without it (they re-run the migrations and skip an `ADD COLUMN` whose column exists), but setting it saves that step. Each export is a full copy, so for large databases export during quiet hours. An incremental sync tool is a natural next step.

## Local testing without wrangler

`test/serve.mjs` runs the Worker's fetch handler inside a Node HTTP server, with D1 emulated on `node:sqlite` (`test/d1-shim.mjs`). The shim enforces D1's parameter, statement and value limits, transactions only through `batch()`, and D1's PRAGMA list, so the Worker does not rely on anything D1 refuses.

```sh
node test/serve.mjs --port 8787 --config tidesight.json --data ./data
```

`DIR/events.db` and `DIR/replay.db` stand in for the two databases. The migration files are applied at start-up, and the last one applied is recorded as `PRAGMA user_version`, like the export above. The socket address is passed as `CF-Connecting-IP`. The conformance suite uses this server:

```sh
TIDESIGHT_CONNECTORS=worker python -m pytest server/python/tests/test_conformance.py -q   # from the repository root
```

## Differences from the Python collector

The Node connector's list applies (see `../node/README.md`: gzip edge cases, HTTP details, nesting). In addition:

- **`/health`** reports `"storage": "d1"`.
- **Path and query come from Cloudflare's URL.** A query character that the URL standard percent-encodes (a raw space, `"`, `<`, `>`) is stored encoded, and headers arrive combined and lower-cased as Cloudflare delivers them.
- **Size caps.** The D1 caps on body sizes above apply.
- **No spool.** Storage failures answer 503 (see above), and `storage_mode` is ignored.
- **Tracker files** are served only through an `ASSETS` binding.
