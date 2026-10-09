# Tidesight PHP connector

A drop-in ingest endpoint for hosts that run PHP but not Python web apps. It speaks the same wire protocol as the reference Python collector (`docs/protocol.md`) and writes **exactly the same SQLite files and rows** (`events.db`, `replay.db`, the spool), so the Python jobs (`jobs.py enrich`, `rollup`, `load-spool`, …) and the dashboard work on its data unchanged. Both connectors can even write to the same data directory at the same time.

It is checked by the shared conformance suite:

```
TIDESIGHT_CONNECTORS=php python -m pytest server/python/tests/test_conformance.py -q
```

## Requirements

- PHP 7.4 or later, **64-bit** (timestamps are milliseconds). Tested with 7.4.33 and 8.4. On 32-bit PHP, or with `mbstring.func_overload` on (PHP 7 only), the connector refuses to run: every endpoint answers `503 configuration error` and the reason is logged.
- PDO with `pdo_sqlite` (preferred) or the `SQLite3` extension; `zlib`, `json`, `pcre`, `hash` (all standard). No Composer packages.
- Functions that shared hosts often disable (`ini_set`, `ini_get`, `ignore_user_abort`, `header_remove`, `gethostname`, `php_uname`, `getmypid`, `getenv`, `umask`, `realpath`, `fileinode`, `disk_free_space`, `touch`, `flock`, `error_log`, `random_bytes`, `register_shutdown_function`) are optional: each one has a fallback (see "Differences" below).
- opcache, if the host offers it (it usually does): it removes the cost of compiling the connector on every request.
- Python 3.9+ somewhere you can run cron jobs, for the enrichment jobs and to generate the configuration. The PHP connector only ingests.

## Files

| File | Role |
|---|---|
| `index.php` | Entry point for Apache / shared hosting. |
| `.htaccess` | Routes `/collect`, `/chunk`, `/p.gif`, `/config.js`, `/config.json`, `/health` to `index.php`; denies `src/`, configuration files, databases, spool files, logs and Tidesight's dot-files (even without `mod_rewrite`); no directory listings; PHP settings for `mod_php`. |
| `.user.ini` | PHP settings for CGI/FastCGI hosts (request bodies left unparsed, no diagnostics in responses). |
| `router.php` | Router for PHP's built-in server (development, tests). |
| `tidesight.php` | Bootstrap: autoloads `src/` and runs a request. |
| `src/Collector.php` | The endpoints, mirroring `collect.py` handler by handler. |
| `src/Storage.php` | SQLite schema (same migrations, DDL verbatim), writes, allowances and quotas, spool, fallback. |
| `src/Config.php` | Reads the resolved JSON configuration. |
| `src/Json.php`, `src/JsonParser.php` | JSON read and written exactly as Python does (see below). |
| `src/Util.php`, `src/Ip.php` | Python-compatible helpers: `parse_qs`, `fnmatch`, origin parsing, gzip, IP anonymisation, client keys. |
| `src/Redact.php` | URL redaction of the stored Referer (a port of `redact.py`). |

## Configuration

The PHP connector does not read the TOML file. It reads the **resolved configuration** that the Python code produces, with every default and per-site layer already applied, so both connectors always agree:

```
cd ~/tidesight/server/python
python3 -m tidesight.jobs --config ~/tidesight/tidesight.toml config-json --out ~/tidesight/tidesight.json
```

- Run it again after every change to the TOML file, and after upgrading Tidesight. PHP reads the JSON on every request: no restart is needed. A file written by an older version still works (settings it lacks take their defaults), but it carries no URL redaction patterns, so the Referer is stored unredacted until the file is regenerated.
- The file contains the deployment secret (IP hashing, cookieless ids). `--out` creates it with mode 600. Keep it **outside the web root**.
- `data_dir` in it is an absolute path. The data directory must also be outside the web root and writable by the PHP process.

Tell PHP where the file is, in this order of precedence:

1. `define('TIDESIGHT_CONFIG', '/home/USER/tidesight/tidesight.json');` at the top of `index.php` (works everywhere);
2. the `TIDESIGHT_CONFIG` environment variable (e.g. `SetEnv TIDESIGHT_CONFIG …` in `.htaccess`, `env[TIDESIGHT_CONFIG]` in a PHP-FPM pool, or `fastcgi_param` in nginx), read with `getenv()` or, where the host disables it, from `$_SERVER` and `$_ENV`;
3. a `tidesight.json` next to `index.php` (in the web root: development only, see below), or next to `tidesight.php` when the library lives outside the web root.

Without a configuration every endpoint answers `503 configuration error` and the reason is logged.

**Nothing of Tidesight's may lie in the web root.** As the Python collector does, the connector refuses to run (`503 configuration error`, the reason logged) when the data directory, the configuration file or the log file is inside the web server's document root (`DOCUMENT_ROOT`), where anyone could download the databases or the secret. Move them; only if the web server is set up to deny those files, set `[server] allow_data_in_web_root = true`.

## Deploying on Apache shared hosting

1. Create a (sub)domain for the collector, e.g. `stats.example.com`, or use a sub-directory of an existing site.
2. Copy `index.php`, `.htaccess`, `.user.ini`, `tidesight.php` and `src/` into its document root (or sub-directory). To keep the library out of the web root, put `tidesight.php` and `src/` elsewhere and change the `require` in `index.php`.
3. Copy the tracker build (`tracker/build/ts.js` and `tracker/build/ts/`) into the same directory, so Apache serves it as static files. Alternatively set `[server] static_dir` to a copy of the build and let `index.php` serve it. Without `static_dir`, `index.php` serves no file (`404`): unlike the Python collector, it has no default location, because one relative to the code would point outside the deployment (on shared hosting, possibly into a directory nobody meant to publish). Files are served only from inside `static_dir` (a symbolic link out of it answers `404`).
4. Generate `tidesight.json` (above) and point `index.php` at it.
5. Check: `curl -i https://stats.example.com/health` → `200 {"status": "ok", …}`; `curl https://stats.example.com/config.js?site=YOURSITE`.
6. Add the Python cron jobs as usual (`jobs.py cron`, which also loads the spool when there is one).

Notes:

- **Without `mod_rewrite`**, use path info: point the tracker's `data-api` at `https://stats.example.com/index.php`; the connector routes `…/index.php/collect`, `…/index.php/p.gif`, and so on.
- In a sub-directory, if the rules do not match, uncomment `RewriteBase` in `.htaccess`.
- `.htaccess` passes the `Authorization` header on to PHP so that its presence is recorded (its value is always redacted, as in Python).
- `.htaccess` also denies, by file name and even without `mod_rewrite`, what must never be downloaded should it end up in the web root: `*.json` (except the `/config.json` endpoint), `*.toml`, `*.db`, `*.sqlite`, `-wal`/`-shm`/`-journal` files, `*.jsonl` spool files, logs (`*.log`, `error_log`), `.secret`, `.error-log`, `.sqlite-down`, `*.ini`, `*.md` and editor backups; and it turns directory listings off (`Options -Indexes`; if the host forbids `Options` in `.htaccess`, every request answers `500`: remove that line).
- **Permissions.** The connector creates its directories and files private, whatever the host's umask: `0700`/`0600` with PHP-FPM, suPHP or CGI (most shared hosts), where PHP runs as your account. With `mod_php`, PHP runs as the web server user, so they are created `0770`/`0660`: the data directory (and the database files and their `-wal`/`-shm` companions) must be writable by both that user and the account running the cron jobs, e.g. a shared group with `chmod 2770` on the directory. Nobody else ever gets access.
- `.user.ini` sets `enable_post_data_reading = Off` and `display_errors = Off` (CGI/FastCGI only). Without the first, PHP empties the raw body of `multipart/form-data` requests (the tracker never sends those). `mod_php` ignores `.user.ini`: `.htaccess` sets both with `php_flag` there (hosts that forbid `php_flag` answer `500`: remove those lines and ask the host to turn `display_errors` off). The connector turns `display_errors` off itself, but only once it runs: warnings PHP emits before (an oversize body, too many form fields) would otherwise be printed into the response.
- Where the host allows it, `X-Powered-By` is removed from the responses (`header_remove`); otherwise set `expose_php = Off` in `php.ini`.

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

Everything the conformance suite checks, plus what a differential test harness compared on tens of thousands of generated inputs (JSON documents and fuzzed variants, floats, query strings, form bodies, URLs and their redaction, IP addresses and networks, client keys, glob patterns, gzip streams, whitespace stripping, nesting depth) and on a 424-request end-to-end corpus sent to both connectors, in SQLite, spool and fallback modes and with several limit and proxy settings (all stored columns, counters, `rate_limits` rows, statuses and response headers):

- **Endpoints and responses:** status codes, CORS (`reflect`/`allowed`, the same `Origin` syntax check), preflight, `Accept-CH`, `Cache-Control`, `Retry-After`, the server cookie `ts_svid`, the pixel GIF, `config.js`/`config.json` (same JSON text, `cv` and `ETag`, `304`), `/health`, static tracker files, `405`/`404`.
- **Raw storage:** the same schema and migrations (`PRAGMA user_version`), one `BEGIN IMMEDIATE` transaction per request with its allowances and quota (below), `journal_mode`, `synchronous` and `busy_timeout` from the configuration, and the spool fallback with the same line format and per-process file layout (`load-spool` imports it into identical rows).
- **Limits and abuse protection** (`docs/configuration.md`): the same `rate_limits` rows and `counters`, checked in the same order. See below.
- **Headers:** lower-case names, values read as ISO-8859-1 like a WSGI server does, secrets redacted (`Authorization`, `Proxy-Authorization`, and every name matching `secret_patterns` with Python's `fnmatch` semantics: by default `*token*`, `*key*`, `*secret*`, `*password*`, `*auth*`, `*session*`, `*jwt*`, `*assertion*`, `*credential*`, `*signature*`, `*cookie*`), `drop`, the `Cookie` header reduced to the kept `ts_*` values (at most 20; each cut at its first comma and right-stripped, so a header the web server joined from several cannot carry other cookies into a kept value) plus the count of the others, 8 KB value limit. The `Referer`, which can hold the full page URL, is stored with its secrets redacted by the site's `redact_params` and `redact_paths` (the default ones for an unknown site), exactly as `redact.py` does (all its test vectors pass): `http`, `https`, `ws` and `wss` URLs; credentials; query, fragment, hash-route and `;` path parameters; paths; parameter values that are themselves URLs or queries looked into two levels deep; in linear time; the pixel's URL, when no `u` or `url` parameter gives it, is that redacted Referer.
- **Bodies:** gzip sniffed by magic bytes or `Content-Encoding`, bounded decompression (`max_decompressed_kb`, 256 by default); `text/plain`, `application/json` and form-encoded `d`/`data`; the same error flags (`bad_json: <Python's message>`, `bad_json: number out of range` for a number too large for a float, `bad_json: nested deeper than 64 levels`, `bad_utf8`, `not_an_object`, `bad_gzip: …`, `empty_body`, `too_large`, `unsupported_encoding`, `bad_version`, `no_events`, `too_many_events:<count>`, `bad_events:N`).
- **JSON:** parsed with Python's rules and error messages (a port of CPython's scanner handles everything PHP's `json_decode` cannot reproduce: error messages, lone surrogates, integers beyond 64 bits, nesting up to Python's limit), and written byte-for-byte as `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` (Python float repr, key order, escapes).
- **Typed columns:** the same truncation by code points, `_int` (including Unicode digits) and `_str` conversions; `name` only from a string `n`.
- **Privacy:** DNT/GPC drops, IP truncation (/24, /48, /40 for a 6to4 address, IPv4-mapped as IPv4) and hashing (`h:` + 16 hex of HMAC-SHA256 with the secret), applied to `REMOTE_ADDR` and every IP-bearing header (the known names, `Via` included, and any name containing `ip`, `forwarded`, `client` or `addr`) with Python's `ipaddress` rules; `drop_fingerprint`.
- **Chunks:** stored unparsed, gzip-compressed when sent plain (the same deflate bytes as Python's `gzip.compress`), `meta` with all query parameters, `bytes`, `sent_encoding` and user-agent/origin/referer.

The `server` column says `"connector": "php"` and also records the PHP version and SAPI.

## Limits and abuse protection

Every layer of `docs/configuration.md` ("Limits and abuse protection") works as in the Python collector, from the same `[server]` keys and each site's `max_hits_per_day` and `features` in the resolved JSON:

- **The client** is `REMOTE_ADDR`, or the last comma-separated value of the `rate_limit_key` header, believed only when `REMOTE_ADDR` is in `rate_limit_proxies` (CIDRs; Python writes `cloudflare` into the resolved JSON as Cloudflare's ranges) and the value is an IP address. A port is ignored; an IPv6 address becomes its `rate_limit_ipv6_prefix` network written as Python's `ipaddress` does (`2001:db8:1:2::/64`), an IPv4-mapped address its IPv4 form. In the allowance keys the client is hashed with the secret as `hash_ip` does (`h:` + 16 hex), so `rate_limits` (and its backups) never holds an address, whatever the privacy settings.
- **Per client and clock minute**, in `rate_limits`: requests (`r:<client>`) and kilobytes (`kb:<client>`), separately for `/chunk` (in `replay.db`). The kilobytes are what the request stores: on `/collect` the largest of the body received, the body decompressed, 256 bytes per hit and the JSON of its events (`max_payload_kb` × 1024 + 1 for an oversize body), plus the query string, the stored headers and the kept cookies; on `/p.gif` the query string (at least 256 bytes) plus headers and cookies; on `/chunk` the body, the query string and the user-agent, origin and referer kept in `meta`.
- **Early refusal:** a client already over its request or kilobyte allowance this minute is refused before its body is decompressed, parsed or compressed (the costly part of a request): charged `r:` 1 and `kb:` the body received plus the query string, and counted like any refusal, under the site the query string names if it is configured.
- **Flagged traffic** (a `site_status` other than `ok`, an unreadable body, `no_events` or `too_many_events`; a pixel only by its status; a chunk of a kind the site does not record is stored as `feature_off`): `f:<client>` and, for everyone, `*f`. A batch with a few invalid events or a bad version is not flagged.
- **Daily quota** per site (`max_hits_per_day`, off by default; UTC day): stored hits are counted as `hits` in `counters`.
- **Refusals** answer `429` with `Retry-After: 60` (a `400` or `413` keeps its status; the pixel always answers its GIF) and are counted as `rate_limited`, `chunk_rate_limited`, `flagged_limited` or `over_quota`. Requests that only count something (`dropped_dnt`, `dropped_gpc`, `chunk_too_large`) write their counter only within `r:<client>`. Every counter goes under the configured site or `""`, never under a site id taken from the request.
- **Bodies:** the first `max_events_per_request` events become hits and the raw body keeps the batch (`400`); objects and arrays nested deeper than 64 levels make a body unreadable (`400`); a declared `Content-Length` counts as at most 2^53 − 1.
- **Disk space:** below `min_free_disk_mb` free, `/collect` and `/chunk` answer `503` with `Retry-After: 300` and the pixel stores nothing.
- **Old allowance rows** are deleted at most 20,000 at a time, through the `rate_limits_minute` index (migrations 3 of `events.db` and 2 of `replay.db`, as in Python).

Behind a proxy or Cloudflare, set `rate_limit_key` to the header carrying the visitor's address (e.g. `cf-connecting-ip`) and list the proxy in `rate_limit_proxies` (`["cloudflare"]` in the TOML), otherwise all visitors share the proxy's allowance.

## Differences from the Python collector

- **Corrupt deflate data:** the error reads `bad_gzip: Error -3 while decompressing data: data error`, where Python names zlib's internal reason (`invalid distance too far back`, …). Header, trailer, truncation and size errors are worded identically.
- **Allowances in spool mode** (`storage_mode = "spool"`, or while SQLite is failing) are counted per process, as in Python, separately for requests and chunks as in SQLite mode. PHP forgets its variables after each request, so the counts live in an in-memory SQLite database on a persistent PDO connection, which survives between requests in a PHP-FPM, `mod_php`, FastCGI or built-in-server worker. Under plain CGI (`php-cgi` started for each request, as with suPHP) they live in `<data_dir>/spool-allowances.db`, a small SQLite file every process shares (without syncs: the counts are disposable), so there the allowances and quotas hold for all processes together. Without `pdo_sqlite` no allowance or quota applies in spool mode; the size limits, the event cap, chunk kinds and the disk check still do. Counting that fails never refuses a record.
- **Spool files under plain CGI:** like Python's, the spool writes one file per process and day (`<host>-<pid>.jsonl`, appended under a lock). Under plain CGI every request is a new process, so spool mode creates a small file per request (and `load-spool` a `spool_progress` row per file) until `load-spool` deletes the files two days after loading them. On a host with an inode quota, keep SQLite mode (the default, which spools only while SQLite is failing) or use FastCGI.
- **Refusal counts in spool mode** are summed and written at most once a minute per process, as in Python. Python also writes the pending ones when its store is closed, a moment a PHP worker does not have: the first request that spools a minute after the last write writes them instead, and counts still pending when a worker exits are lost. (Under plain CGI they wait in `spool-allowances.db` for any later process.)
- **After a SQLite failure** the spool takes every write for 30 s, as with Python's `FallbackStore`, and `SQLite failed (…); using the spool for 30 s` is logged. Python keeps that moment per process; PHP keeps it as the modification time of `<data_dir>/.sqlite-down`, so it holds for all workers together. Any failure of the SQLite store counts, not only database errors.
- **Free disk space** is read with `disk_free_space()` on each request that writes (Python caches it for 10 s per process); where a host disables that function, the check is off. The low-disk warning is logged for about one refused request in a hundred, within the error budget below (Python: once a minute per process).
- **Pruning of `rate_limits`:** on about one request in a hundred instead of at most once a minute per process (the same bounded batch).
- **Error logging** has a budget, as in Python: the first five errors of each minute are logged in full, then only a count (`N more errors in the previous minute (not logged)`, written when a later minute's first error arrives), so a flood of failing requests cannot fill the account's disk with log lines. Python counts per process; PHP, which keeps nothing between requests, keeps the minute and the count in `<data_dir>/.error-log`, under a file lock, so the budget holds for all workers together, CGI included. It covers everything a request can log: storage unavailable, `SQLite failed`, unexpected exceptions, counters, spool allowances, the low-disk and `journal_mode` warnings, a secret that cannot be saved, a data directory in the web root, an unreadable configuration and an unsupported PHP (for these two no data directory is known: the state file goes next to the configuration file, if there is one). When that file cannot be used, every error is logged.
- **No configuration file:** `503`; Python falls back to built-in defaults.
- **Static files** are served only from `static_dir` (Python defaults to the repository's `tracker/build`), and only from inside it (see "Deploying").
- **Colliding JSON keys.** PHP's arrays hash string keys without a per-process seed, so a JSON object whose keys are crafted to collide takes time quadratic in their number to decode (Python randomises its string hashes). The decompressed size bounds it: with the default `max_decompressed_kb = 256` the worst body measured costs ≈ 0.09 s (with `json_decode`) or ≈ 0.21 s (with the exact parser, used when a body holds integers beyond 64 bits), against 1.3–2.9 s at 1024. Raise `max_decompressed_kb` with that in mind. Form bodies (`d=`/`data=`) are read without building an array, so field names cost nothing.
- **Functions the host disables** fall back as follows: without `ini_set`, PHP's `display_errors` and `memory_limit` stay as configured (set them in `php.ini` or `.user.ini`; bodies above 1 MB, possible only with `max_payload_kb` or `max_decompressed_kb` above 1024, may need more memory) and floats are written through a slower search for the shortest form; without `ignore_user_abort`, PHP may stop a request whose client has gone once it sends the response (the record is stored before); without `umask`, new files get the host's default mode, but the directories the connector creates are still private (`mkdir` is given the mode) and the generated secret is made private with `chmod`; without `gethostname`/`php_uname` the spool files are named `host-<pid>.jsonl`, and without `getmypid` every process appends to `<host>-php.jsonl` (under a lock); without `getenv`, `TIDESIGHT_CONFIG` is read from `$_SERVER` and `$_ENV`; without `realpath`, static files are not served and the web-root check compares paths as written; without `fileinode`, a replaced `events.db` needs a PHP restart; without `flock`, the error budget is off; without `header_remove`, `X-Powered-By` stays (set `expose_php = Off`); without `error_log` and no `log_file`, nothing is logged; without `random_bytes`, `openssl_random_pseudo_bytes` or `random_int` generates a missing secret (with none of them, set `[server] secret`); without `register_shutdown_function`, a transaction left open by a fatal error is rolled back when the persistent handle is next used.
- Differences that come from the web server, not the collector: how duplicate headers are joined, whether surrounding whitespace is stripped from header values (Python's dev server strips Unicode whitespace, including NBSP), which headers it passes on (Python's dev server adds `content-type: text/plain` to a request without one), and so the bytes charged for the stored headers, and that PHP's built-in server rejects raw non-ASCII bytes in the request line (browsers always percent-encode them).

## Performance

Measured on a Windows development machine with PHP 8.4 and the built-in server (server-side time per request, median, allowance checks included): `POST /collect` with 1 event ≈ 1.7 ms, with 20 events ≈ 2.2 ms, `GET /p.gif` ≈ 1.7 ms, with opcache and the persistent connection. Without opcache the connector is recompiled on every request (≈ 8 ms there); with a per-request SQLite3 connection the WAL checkpoint adds ≈ 5–6 ms. Inside one PHP process (no HTTP), a request costs ≈ 0.7 ms for 1 event, 0.9 ms for 20 and 2.0 ms for 100.
