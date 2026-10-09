# Tidesight PHP connector

A drop-in ingest endpoint for hosts that run PHP but not Python web apps. It speaks the same wire protocol as the reference Python collector (`docs/protocol.md`) and writes **exactly the same SQLite files and rows** (`events.db`, `replay.db`, the spool), so the Python jobs (`jobs.py enrich`, `rollup`, `load-spool`, …) and the dashboard work on its data unchanged. Both connectors can even write to the same data directory at the same time.

It is checked by the shared conformance suite:

```
TIDESIGHT_CONNECTORS=php python -m pytest server/python/tests/test_conformance.py -q
```

## Requirements

- PHP 7.4 or later, **64-bit** (timestamps are milliseconds). Tested with 7.4.33 and 8.4.
- PDO with `pdo_sqlite` (preferred) or the `SQLite3` extension; `zlib`, `json`, `pcre`, `hash` (all standard). No Composer packages.
- opcache, if the host offers it (it usually does): it removes the cost of compiling the connector on every request.
- Python 3.9+ somewhere you can run cron jobs, for the enrichment jobs and to generate the configuration. The PHP connector only ingests.

## Files

| File | Role |
|---|---|
| `index.php` | Entry point for Apache / shared hosting. |
| `.htaccess` | Routes `/collect`, `/chunk`, `/p.gif`, `/config.js`, `/config.json`, `/health` to `index.php`; denies `src/` and config files. |
| `.user.ini` | PHP settings for CGI/FastCGI hosts (keeps request bodies unparsed). |
| `router.php` | Router for PHP's built-in server (development, tests). |
| `tidesight.php` | Bootstrap: autoloads `src/` and runs a request. |
| `src/Collector.php` | The endpoints, mirroring `collect.py` handler by handler. |
| `src/Storage.php` | SQLite schema (same migrations, DDL verbatim), writes, rate limit, spool, fallback. |
| `src/Config.php` | Reads the resolved JSON configuration. |
| `src/Json.php`, `src/JsonParser.php` | JSON read and written exactly as Python does (see below). |
| `src/Util.php`, `src/Ip.php` | Python-compatible helpers: `parse_qs`, `fnmatch`, origin parsing, gzip, IP anonymisation. |

## Configuration

The PHP connector does not read the TOML file. It reads the **resolved configuration** that the Python code produces, with every default and per-site layer already applied, so both connectors always agree:

```
cd ~/tidesight/server/python
python3 -m tidesight.jobs --config ~/tidesight/tidesight.toml config-json --out ~/tidesight/tidesight.json
```

- Run it again after every change to the TOML file. PHP reads the JSON on every request: no restart is needed.
- The file contains the deployment secret (IP hashing, cookieless ids). `--out` creates it with mode 600. Keep it **outside the web root**.
- `data_dir` in it is an absolute path. The data directory must also be outside the web root and writable by the PHP process.

Tell PHP where the file is, in this order of precedence:

1. `define('TIDESIGHT_CONFIG', '/home/USER/tidesight/tidesight.json');` at the top of `index.php` (works everywhere);
2. the `TIDESIGHT_CONFIG` environment variable (e.g. `SetEnv TIDESIGHT_CONFIG …` in `.htaccess`, `env[TIDESIGHT_CONFIG]` in a PHP-FPM pool, or `fastcgi_param` in nginx);
3. a `tidesight.json` next to `index.php` (not recommended: it would sit in the web root; `.htaccess` denies it, but only where `mod_rewrite` works).

Without a configuration every endpoint answers `503 configuration error` and the reason is logged.

## Deploying on Apache shared hosting

1. Create a (sub)domain for the collector, e.g. `stats.example.com`, or use a sub-directory of an existing site.
2. Copy `index.php`, `.htaccess`, `.user.ini`, `tidesight.php` and `src/` into its document root (or sub-directory). To keep the library out of the web root, put `tidesight.php` and `src/` elsewhere and change the `require` in `index.php`.
3. Copy the tracker build (`tracker/build/ts.js` and `tracker/build/ts/`) into the same directory, so Apache serves it as static files. Alternatively set `[server] static_dir` and let `index.php` serve them.
4. Generate `tidesight.json` (above) and point `index.php` at it.
5. Check: `curl -i https://stats.example.com/health` → `200 {"status": "ok", …}`; `curl https://stats.example.com/config.js?site=YOURSITE`.
6. Add the Python cron jobs as usual (`jobs.py cron`, which also loads the spool when there is one).

Notes:

- **Without `mod_rewrite`**, use path info: point the tracker's `data-api` at `https://stats.example.com/index.php`; the connector routes `…/index.php/collect`, `…/index.php/p.gif`, and so on.
- In a sub-directory, if the rules do not match, uncomment `RewriteBase` in `.htaccess`.
- `.htaccess` passes the `Authorization` header on to PHP so that its presence is recorded (its value is always redacted, as in Python).
- **Permissions.** With PHP-FPM, suPHP or CGI (most shared hosts), PHP runs as your account: `chmod 700` the data directory. With `mod_php`, PHP runs as the web server user: the data directory (and the database files and their `-wal`/`-shm` companions) must be writable by both that user and the account running the cron jobs, e.g. a shared group with `chmod 2770` on the directory.
- `.user.ini` sets `enable_post_data_reading = Off` (CGI/FastCGI only). Without it PHP empties the raw body of `multipart/form-data` requests; the tracker never sends those. With `mod_php`, the equivalent is `php_flag enable_post_data_reading Off` in `.htaccess` (only if the host allows `php_flag`).

### nginx + PHP-FPM

```nginx
location ~ ^/(collect|chunk|p\.gif|config\.js|config\.json|health)$ {
    include fastcgi_params;
    fastcgi_param SCRIPT_FILENAME /var/www/stats/index.php;
    fastcgi_param SCRIPT_NAME /index.php;
    fastcgi_param TIDESIGHT_CONFIG /home/USER/tidesight/tidesight.json;
    fastcgi_pass unix:/run/php/php-fpm.sock;
}
```

### Built-in server (development only)

```
TIDESIGHT_CONFIG=/path/to/tidesight.json php -S 127.0.0.1:8080 server/php/router.php
```

Every request goes to the collector; nothing is served from the document root. On Windows builds without a `php.ini`, load the extensions explicitly: `php -d extension_dir=<php>/ext -d extension=pdo_sqlite -d extension=sqlite3 -S …`.

## Database access

The connector prefers **PDO SQLite with a persistent connection**: the connection stays open in each PHP worker (PHP-FPM, `mod_php`, the built-in server), just as the Python collector keeps one per process. With a fresh connection per request the closing connection is usually the last one, and SQLite then checkpoints the WAL into the database (with syncs) and deletes it on every request: measured here, that is ~5 ms of database work per request against ~0.05 ms with the persistent connection. Safeguards: a transaction interrupted by a fatal error is rolled back at shutdown (and defensively when the handle is next used), and the persistent handle is keyed by the database file's inode, so a replaced or restored `events.db` gets a new connection. Under plain CGI (one process per request) nothing persists and the behaviour is that of a normal connection.

When `pdo_sqlite` is missing the `SQLite3` extension is used (one connection per request). Force a driver with `TIDESIGHT_SQLITE_DRIVER=sqlite3` (environment variable or PHP constant).

As with the Python collector, restart PHP (e.g. reload PHP-FPM) after replacing the database files by hand.

## What is identical to the Python collector

Everything the conformance suite checks, plus what a differential test harness compared on tens of thousands of generated inputs (JSON documents and fuzzed variants, floats, query strings, URLs, IP addresses, glob patterns, gzip streams) and on a 143-request end-to-end corpus sent to both connectors (all stored columns, statuses and response headers):

- **Endpoints and responses:** status codes, CORS (`reflect`/`allowed`, the same `Origin` syntax check), preflight, `Accept-CH`, `Cache-Control`, `Retry-After`, the server cookie `ts_svid`, the pixel GIF, `config.js`/`config.json` (same JSON text, `cv` and `ETag`, `304`), `/health`, static tracker files, `405`/`404`.
- **Raw storage:** the same schema and migrations (`PRAGMA user_version`), one `BEGIN IMMEDIATE` transaction per request, the per-key rate limit (`rate_limit_key` header or `REMOTE_ADDR`) and the counters (`rate_limited`, `dropped_dnt`, `dropped_gpc`, `chunk_too_large`), `journal_mode`, `synchronous` and `busy_timeout` from the configuration, and the spool fallback with the same line format and per-process file layout (`load-spool` imports it into identical rows).
- **Headers:** lower-case names, values read as ISO-8859-1 like a WSGI server does, secrets redacted (`Authorization`, `Proxy-Authorization`, `secret_patterns` with Python's `fnmatch` semantics), `drop`, the `Cookie` header reduced to the kept `ts_*` values plus the count of the others, 8 KB value limit.
- **Bodies:** gzip sniffed by magic bytes or `Content-Encoding`, bounded decompression; `text/plain`, `application/json` and form-encoded `d`/`data`; the same error flags (`bad_json: <Python's message>`, `bad_utf8`, `not_an_object`, `bad_gzip: …`, `empty_body`, `too_large`, `unsupported_encoding`, `bad_version`, `no_events`, `too_many_events`, `bad_events:N`).
- **JSON:** parsed with Python's rules and error messages (a port of CPython's scanner handles everything PHP's `json_decode` cannot reproduce: error messages, lone surrogates, integers beyond 64 bits, nesting up to Python's limit), and written byte-for-byte as `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` (Python float repr, key order, escapes).
- **Typed columns:** the same truncation by code points, `_int` (including Unicode digits) and `_str` conversions; `name` only from a string `n`.
- **Privacy:** DNT/GPC drops, IP truncation (/24, /48, IPv4-mapped) and hashing (`h:` + 16 hex of HMAC-SHA256 with the secret), applied to `REMOTE_ADDR` and every IP-bearing header with Python's `ipaddress` rules; `drop_fingerprint`.
- **Chunks:** stored unparsed, gzip-compressed when sent plain (the same deflate bytes as Python's `gzip.compress`), `meta` with all query parameters, `bytes`, `sent_encoding` and user-agent/origin/referer.

The `server` column says `"connector": "php"` and also records the PHP version and SAPI.

## Differences from the Python collector

- **Corrupt deflate data:** the error reads `bad_gzip: Error -3 while decompressing data: data error`, where Python names zlib's internal reason (`invalid distance too far back`, …). Header, trailer, truncation and size errors are worded identically.
- **Rate limiting in spool mode** (`storage_mode = "spool"`, or while SQLite is failing): PHP keeps no memory between requests, so no limit applies. Python's spool limit is per process.
- **Pruning of `rate_limits`:** on about one request in a hundred instead of every ten minutes per process (same rows deleted).
- **No configuration file:** `503`; Python falls back to built-in defaults.
- Differences that come from the web server, not the collector: how duplicate headers are joined, whether surrounding whitespace is stripped from header values (Python's dev server strips Unicode whitespace, including NBSP), and that PHP's built-in server rejects raw non-ASCII bytes in the request line (browsers always percent-encode them).

## Performance

Measured on a Windows development machine with PHP 8.4 and the built-in server (server-side time per request, median): `POST /collect` with 1 event ≈ 2.0 ms, with 20 events ≈ 2.6 ms, `GET /p.gif` ≈ 2.7 ms, with opcache and the persistent connection. Without opcache the connector is recompiled on every request (≈ 11–15 ms there); with a per-request SQLite3 connection the WAL checkpoint adds ≈ 8–12 ms. Inside one PHP process (no HTTP), a request costs ≈ 0.6 ms for 1 event, 0.8 ms for 20 and 2.1 ms for 100.
