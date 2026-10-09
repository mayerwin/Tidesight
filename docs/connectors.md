# Connectors

A **connector** is the server-side ingest script: it implements the wire protocol (`docs/protocol.md`) and writes the SQLite schema of `events.db` and `replay.db` (`docs/schema.md`). Every connector produces exactly the same rows for the same requests, so the enrichment jobs, the API and the dashboard (all in Python) work on data from any of them.

| Connector | Where | Runs on | Storage |
|---|---|---|---|
| Python (reference) | `server/python/tidesight/collect.py` | Passenger WSGI, CGI, any WSGI server | SQLite (stdlib `sqlite3`), spool fallback |
| PHP | `server/php/` | Apache or nginx with PHP 7.4+, 64-bit (any shared host) | PDO SQLite or SQLite3, spool fallback |
| Node | `server/node/` | Node 22.13+ (built-in `node:sqlite`) | SQLite, spool fallback |
| Cloudflare Worker | `server/worker/` | Cloudflare Workers | D1 (SQLite-compatible), same schema; no spool |

Each folder has a README with deployment steps and the few documented differences from the Python collector. The Node connector and the Worker share their protocol code (`server/node/core/`).

## What a connector must do

The Python collector is the reference; when in doubt, mirror it. In short:

- **Endpoints:** `POST /collect`, `POST /chunk`, `GET|HEAD /p.gif`, `GET /config.js` and `/config.json`, `GET /health`, `OPTIONS` preflight.
- **Bodies:** bounded reads (`max_payload_kb`, `max_chunk_kb`), gzip detected by magic bytes or `Content-Encoding`, bounded decompression (`max_decompressed_kb`), `text/plain`, `application/json` and form-urlencoded `d=` bodies.
- **Never drop, flag instead:** malformed bodies are stored with `error` and the raw `body`; unknown sites, wrong or missing origins are stored with their `site_status`; invalid events become `_invalid` hits. The only refusals are oversize bodies (metadata stored, `413`) and the per-key rate limit (counted, `429`, also while writing to the spool fallback).
- **Storage failures** answer `503` with `Retry-After: 60`, with or without a spool fallback, so the tracker keeps the batch and retries.
- **Text:** lone UTF-16 surrogates, which JSON can carry as escapes (text cut in the middle of an emoji) but UTF-8 cannot, become U+FFFD in every stored string, keys included.
- **Headers:** all of them, lower-case names, values as received, except: `Cookie` → the `cookies` column (`ts_*` values kept, others counted), `Authorization`/`Proxy-Authorization` and names matching `secret_patterns` → `[redacted]`, names in `headers.drop` removed.
- **Typed columns** exactly as in `docs/schema.md`, including the truncation limits and the rule that `name` is filled only from a string `n`.
- **Privacy switches:** IP truncation (/24, /48) and hashing (`h:` + the first 16 hex digits of HMAC-SHA256 with the secret), applied to `ip` and to IP-bearing headers; DNT/GPC drops with counters; fingerprint drop.
- **One transaction per request** (`BEGIN IMMEDIATE`, rate-limit update and inserts, `COMMIT`), and the migrations with the same `PRAGMA user_version`.
- **Responses:** status codes, `Cache-Control: no-store`, CORS (reflect or allowed origins), `Accept-CH`, and the optional `ts_svid` server cookie.

## Configuration: the resolved JSON

Only the Python side parses TOML and applies defaults and per-site layering. Other connectors read the **resolved configuration**:

```sh
python -m tidesight.jobs --config tidesight.toml config-json --out tidesight.json
```

`tidesight.json` (format `tidesight-resolved-config/1`) contains `server` (with the secret), `headers`, `privacy`, `sites` (origins, time zone, server cookie domain, cookie lifetime, merged privacy), `default_site`, and `public` / `public_default`: the exact objects `/config.js` and `/config.json` must return. Regenerate it whenever the TOML changes. It holds the secret: keep it outside the web root, mode 600.

## The conformance suite

`server/python/tests/test_conformance.py` starts each available connector as an HTTP server on a free port, with the same configuration, sends identical requests and inspects the resulting databases: schema identical to Python's, typed columns, flags, redaction, IP anonymisation, chunks, pixel, `config.js`, health, CORS and rate limiting.

```sh
# every connector whose runtime and code are present
python -m pytest server/python/tests/test_conformance.py
# one connector
TIDESIGHT_CONNECTORS=php python -m pytest server/python/tests/test_conformance.py
```

On Windows, PHP builds without a `php.ini` lack SQLite; the suite loads the bundled `sqlite3` and `pdo_sqlite` extensions automatically.

## Adding a connector

1. Implement the endpoints above against the resolved JSON configuration.
2. Create the schema with the exact DDL of `storage.py` (every migration) and set `user_version`. A database without it (such as a D1 export) still migrates: `ADD COLUMN` steps whose column exists are skipped.
3. Add a launcher for it in `_launchers()` of `test_conformance.py` and make the whole suite pass.
4. Document deployment in its folder's README and list it in the table above.
