# Connectors

A **connector** is the server-side ingest script: it implements the wire protocol (`docs/protocol.md`) and writes the SQLite schema of `events.db` and `replay.db` (`docs/schema.md`). Every connector produces exactly the same rows for the same requests, so the enrichment jobs, the API and the dashboard (all in Python) work on data from any of them.

| Connector | Where | Runs on | Storage |
|---|---|---|---|
| Python (reference) | `server/python/tidesight/collect.py` | FastCGI (`tidesight.fcgi`), CGI, Passenger WSGI, any WSGI server | SQLite (stdlib `sqlite3`), spool fallback |
| PHP | `server/php/` | Apache or nginx with PHP 7.4+, 64-bit (any shared host) | PDO SQLite or SQLite3, spool fallback |
| Node | `server/node/` | Node 22.13+ (built-in `node:sqlite`) | SQLite, spool fallback |
| Cloudflare Worker | `server/worker/` | Cloudflare Workers | D1 (SQLite-compatible), same schema; no spool |

Each folder has a README with deployment steps and the few documented differences from the Python collector. The Node connector and the Worker share their protocol code (`server/node/core/`).

## What a connector must do

The Python collector is the reference; when in doubt, mirror it. In short:

- **Endpoints:** `POST /collect`, `POST /chunk`, `GET|HEAD /p.gif`, `GET /config.js` and `/config.json`, `GET /health`, `OPTIONS` preflight.
- **Bodies:** bounded reads (`max_payload_kb`, `max_chunk_kb`), gzip detected by magic bytes or `Content-Encoding`, bounded decompression (`max_decompressed_kb`), `text/plain`, `application/json` and form-urlencoded `d=` bodies.
- **Never drop, flag instead:** malformed bodies are stored with `error` and the raw `body`; unknown sites, wrong or missing origins are stored with their `site_status`; invalid events become `_invalid` hits. The only refusals are oversize bodies (metadata stored, `413`), the allowances below (counted, `429`, also while writing to the spool fallback) and low disk space (`503`).
- **Storage failures** answer `503` with `Retry-After: 60`, with or without a spool fallback, so the tracker keeps the batch and retries.
- **Limits and abuse protection** exactly as in `docs/configuration.md` ("Limits and abuse protection"); `collect.py` (`client_key`, `_limits`, `_count`, `is_flagged` in `storage.py`) is the reference:
  - **Client key:** the remote address, or the **last** comma-separated value of the `rate_limit_key` header (stripped, first 64 characters), believed only when the remote address is in `rate_limit_proxies` (CIDRs in the resolved JSON, `cloudflare` already expanded) and the value is an IP address; a port is ignored (`1.2.3.4:5678`, `[2001:db8::1]:443`); an IPv6 address becomes its `rate_limit_ipv6_prefix` network written as Python's `ipaddress` does (`2001:db8:1:2::/64`), an IPv4-mapped address its IPv4 form; any other value is kept as it is.
  - **Allowances**, in `rate_limits`, checked in this order in the write transaction, the first one exceeded refusing the record: `r:<id>` (1 per request; `rate_limit_per_minute`, or `chunk_rate_limit_per_minute` on `/chunk`), `kb:<id>` (the KB rounded up of what the request stores; `rate_limit_kb_per_minute` or `chunk_kb_per_minute`), and for flagged requests `f:<id>` (`flagged_per_minute`) and `*f` (`flagged_global_per_minute`). `<id>` is the client key hashed as privacy mode hashes an IP: `h:` + the first 16 hex digits of HMAC-SHA256(secret, client key), so the table never holds an address. What a request stores: on `/collect`, the larger of the bytes received, the bytes after decompression, 256 × its hits and the length of its hits' stored `data` (Python `len`), plus its query string, its stored headers and kept cookies (names and values); for a body over the limit, `max_payload_kb` × 1024 + 1 instead of the body; on `/p.gif`, the larger of the query string and 256, plus headers and cookies; on `/chunk`, the bytes received plus the query string plus the user-agent, origin and referer kept in `meta`.
  - **Early refusal:** after reading the body of `/collect` (or, on `/chunk`, after the privacy drop, the 413 and the low-disk checks) and unless disk space is low, a client whose `r:` or `kb:` usage for the current minute is already at or over its limit is refused before anything is decompressed, parsed or compressed: the budgets `r:` (1) then `kb:` (the bytes received plus the query string, in KB) are charged as usual, the exceeded one counted (`rate_limited`, `chunk_rate_limited`) under the configured site named by the query string's `site` (else `""`), and the answer is `429` with `Retry-After: 60` and the CORS headers of that site. A failing check never refuses.
  - **Flagged:** `site_status` not `ok`, an unreadable body, `no_events` or `too_many_events` (a pixel only by its `site_status`); a batch with some invalid events or a bad version is not flagged.
  - **Quota:** for a configured site with `max_hits_per_day` > 0, a non-flagged request whose hits would take the day's (UTC) `hits` counter over it is refused (`over_quota`). Every stored non-flagged request of a configured site adds its hits to that counter.
  - **Refusals** answer `429` + `Retry-After: 60`, unless the request would answer `400` or `413` (except early refusals, which never look at the body); they are counted (`rate_limited`, `chunk_rate_limited`, `flagged_limited`, `over_quota`) under the configured site or `""`, never under a site id from the request. Requests that only count something (`dropped_dnt`, `dropped_gpc`, `chunk_too_large`) write their counter only within `r:<client>` (`rate_limit_per_minute`, in `events.db`).
  - **Also:** chunk kinds gated by the site's features (`feature_off`, stored with `site_ok = 0`); `max_events_per_request` (first events stored, raw body kept, `400`, `too_many_events:<count>`); `min_free_disk_mb` (`503`, `Retry-After: 300`; the pixel still answers its GIF); a declared `Content-Length` above 2^53 − 1 recorded as that.
  - The resolved JSON gives each site's `features` and `max_hits_per_day`.
- **Nesting:** a body whose objects and arrays nest deeper than 64 levels (the top level is 1) is unreadable: `400`, stored flagged with `error` `bad_json: nested deeper than 64 levels` and the raw body.
- **Numbers:** a number with a fraction or an exponent that overflows a double (`1e999`) makes the body unreadable the same way, with `error` `bad_json: number out of range` (stored, it would be `Infinity`, which is not JSON). Integers are kept exact, up to Python's 4,300-digit limit.
- **IP-bearing headers** (privacy mode): the known names (including `via`) plus any header whose lower-case name contains `ip`, `forwarded`, `client` or `addr`; only tokens that parse as IP addresses change. Truncation keeps an IPv4 /24 and an IPv6 /48, but a 6to4 address (`2002::/16`, which embeds an IPv4 address) its /40.
- **Text:** lone UTF-16 surrogates, which JSON can carry as escapes (text cut in the middle of an emoji) but UTF-8 cannot, become U+FFFD in every stored string, keys included.
- **Headers:** all of them, lower-case names, values as received, except: `Cookie` → the `cookies` column (`ts_*` values kept, cut at their first `,` and right-stripped, then at 512 characters; others counted), `Authorization`/`Proxy-Authorization` and names matching `secret_patterns` → `[redacted]`, names in `headers.drop` removed, and `Referer` stored with its URL secrets redacted (below), after the privacy-mode anonymization. The pixel's page URL is its `u` (or `url`) parameter, else that redacted `Referer`.
- **URL redaction** of the stored `Referer`: exactly `server/python/tidesight/redact.py` (`redact_url`), with the patterns of the site's public document (`public[site].redact_params` and `redact_paths`, `public_default` for an unknown site; non-string items ignored). The test vectors in `server/python/tests/redact_vectors.json` fix the behaviour; the tracker implements the same rules for every URL it sends. The essentials:
  - Linear time: never build a regular expression from the patterns. A glob (only `*` is special) is split on `*`; the first and last pieces must be a prefix and a suffix (not overlapping), the pieces between are found left to right at their first occurrence.
  - Case-insensitive for ASCII letters only (`A`–`Z` ≡ `a`–`z`), so positions never shift.
  - A base (the URL before `?` and `#`) whose first `:` follows a scheme other than `http`, `https`, `ws` or `wss` is left as it is; `//host` is protocol-relative; non-empty credentials before the host become `redacted@`.
  - Parameters (`name=value` split on `&`, on `;` for path parameters): the name is percent-decoded as `decodeURIComponent` does after `+` → space (strict: two hex digits after each `%`, valid UTF-8, else the raw name), and a name over 256 characters always matches (both only when `redact_params` is not empty). A matching name keeps its `=` and gets the value `redacted`. They are redacted in the query, in path parameters of each segment, in a fragment that is a list of parameters (no `?`, an `=` after its first character) and in the query of a fragment.
  - A value (decoded the same way) that these rules would change as a URL, or, with no `?` or `#` but an `=`, as a `&` query, is replaced whole by `redacted`: two levels deep at most (the URL itself is level 0; values are looked into below level 2).
  - The path, and a hash route's path (fragment starting with `/` or `!/`, up to its `?`), are compared with `redact_paths`: the first matching pattern applies, each non-empty part a `*` matched becoming `redacted`.
- **Typed columns** exactly as in `docs/schema.md`, including the truncation limits and the rule that `name` is filled only from a string `n`.
- **Privacy switches:** IP truncation (/24, /48) and hashing (`h:` + the first 16 hex digits of HMAC-SHA256 with the secret), applied to `ip` and to IP-bearing headers; DNT/GPC drops with counters; fingerprint drop.
- **One transaction per request** (`BEGIN IMMEDIATE`, rate-limit update and inserts, `COMMIT`), and the migrations with the same `PRAGMA user_version`.
- **Responses:** status codes, `Cache-Control: no-store`, CORS (reflect or allowed origins), `Accept-CH`, and the optional `ts_svid` server cookie. `/config.js` is `(typeof window.tidesight=="function"?window.tidesight:window.tidesight=function(){(tidesight.q=tidesight.q||[]).push(arguments)})("_config",<json>);` followed by a newline, `<json>` being the public document serialized as Python's `json.dumps(…, separators=(",", ":"), sort_keys=True)`.
- **Spool mode:** allowances are kept in memory per process, separately for the events and the chunks streams (as `events.db` and `replay.db` keep theirs); refusal counters are written summed, at most once a minute.
- **Web root:** unless `allow_data_in_web_root`, a connector that sees a `DOCUMENT_ROOT` (PHP, Python under FastCGI, CGI or Passenger) refuses every request with `503` when the real path of its data directory, configuration file or log file is inside it.
- **Migrations:** `events.db` version 3 and `replay.db` version 2 index `rate_limits(minute)`, so old minutes are deleted without a full scan; delete them in bounded batches.
- **Logging:** full details for the first 5 errors of each minute (per process, or per data directory where each request is its own process), then only a count: a flood of failing requests must not fill a small host's disk.

## Configuration: the resolved JSON

Only the Python side parses TOML and applies defaults and per-site layering. Other connectors read the **resolved configuration**:

```sh
python -m tidesight.jobs --config tidesight.toml config-json --out tidesight.json
```

`tidesight.json` (format `tidesight-resolved-config/1`) contains `server` (with the secret), `headers`, `privacy`, `sites` (origins, time zone, server cookie domain, cookie lifetime, merged privacy), `default_site`, and `public` / `public_default`: the exact objects `/config.js` and `/config.json` must return. Regenerate it whenever the TOML changes, and after properties are added or changed in the dashboard (they are part of it): with `[server] resolved_config_file` set, `jobs.py cron` does so whenever something changed. It holds the secret: keep it outside the web root, mode 600. Every connector keeps `ts_*` cookie values except `ts_login`, the dashboard's sign-in cookie.

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
