# Configuration

One TOML file configures the collector, the jobs and the dashboard. Start from `config/tidesight.example.toml`. Every key is optional; defaults are defined in `server/python/tidesight/config.py` (the single source of truth) and listed below.

**Where it is read from:** `--config <file>` (jobs, dev server), else `$TIDESIGHT_CONFIG`, else `./tidesight.toml`, `~/tidesight/tidesight.toml` or `<repo>/config/tidesight.toml`. Relative paths inside the file (`data_dir`, `geo_dir`, `bot_list`, `static_dir`, `dashboard_dir`, `log_file`) are relative to the file. The collector re-reads the file within 5 seconds of a change; an invalid file is reported in the log and the previous configuration stays in use.

**Validation:** wrong types and unknown values are errors; unknown keys are warnings (logged, and shown by `jobs.py status`).

**Other connectors** (PHP, Node, Worker) read a resolved JSON version of this file: `python -m tidesight.jobs --config tidesight.toml config-json --out tidesight.json` (it contains the secret: mode 600, outside the web root).

## `[server]`

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | `"data"` | Databases, spool, backups, generated secret. **Keep it outside the web root.** |
| `storage_mode` | `"sqlite"` | `"sqlite"`, or `"spool"`: append-only files loaded into SQLite by `jobs.py load-spool` (choose from the Phase 0 test). |
| `journal_mode` | `"wal"` | `"wal"` or `"delete"` (`delete` on network filesystems). |
| `synchronous` | `"normal"` | `"normal"` or `"full"`. |
| `busy_timeout_ms` | `10000` | How long a writer waits for the database lock. |
| `max_payload_kb` | `64` | Largest `/collect` body on the wire. |
| `max_chunk_kb` | `1024` | Largest `/chunk` body. |
| `max_decompressed_kb` | `4096` | Largest body after gzip decompression. |
| `max_events_per_request` | `500` | Larger batches are stored but flagged. |
| `rate_limit_per_minute` | `600` | Requests per key per minute; `0` disables. |
| `rate_limit_key` | `"remote_addr"` | Or a request header such as `"cf-connecting-ip"` when behind a proxy. |
| `cors` | `"reflect"` | `"reflect"` any `Origin` on ingest endpoints, or `"allowed"` origins only. |
| `accept_ch` | `true` | Send `Accept-CH` for Client Hints. |
| `public_url` | `""` | Public base URL of the collector (informational). |
| `secret` | `""` | Long random string for IP hashing, cookieless hashes and tokens. Empty: generated once in `data_dir/.secret`. Never change it casually: cookieless hashes and hashed IPs depend on it. |
| `static_dir` | `""` | Folder of tracker files served at `/ts.js` and `/ts/…` (default: the repository's `tracker/build`). |
| `dashboard_dir` | `""` | Folder of dashboard files served at `/dashboard/` (default: the repository's `dashboard`). |
| `store_raw_body` | `false` | Also keep the raw body of requests that parsed fine. |
| `spool_fallback` | `true` | When SQLite fails, write the request to the spool instead of answering 503. |
| `log_file` | `""` | Error log; stderr (the host's error log) when empty. |

## `[headers]`

| Key | Default | Meaning |
|---|---|---|
| `secret_patterns` | `["*token*", "*key*", "*secret*", "*password*"]` | Header names (glob, case-insensitive) whose values are replaced by `[redacted]`. `Authorization` and `Proxy-Authorization` always are. |
| `drop` | `[]` | Header names never stored. |
| `cookie_names` | `["ts_*"]` | Cookie names whose values are kept; other cookies are only counted. |

## `[privacy]`

All off by default. Each key can be overridden per site (`[sites.<id>.privacy]`). See `docs/privacy.md`.

| Key | Meaning |
|---|---|
| `hash_ip` | Store a keyed hash of the IP (and of IPs in forwarding headers) instead of the address. |
| `truncate_ip` | Store only the /24 (IPv4) or /48 (IPv6) network. |
| `honour_dnt` | Visitors sending Do Not Track are not tracked (tracker silent, collector drops and counts). |
| `honour_gpc` | Same for Global Privacy Control. |
| `drop_fingerprint` | Disable every fingerprint feature and drop `fp` events at ingest. |

## `[retention]`

Days to keep, applied by `jobs.py privacy-clean --retention --apply`; `0` keeps forever. Daily aggregates are kept.

| Key | Applies to |
|---|---|
| `raw_days` | Raw requests and hits (and, after the rebuild, their sessions). |
| `replay_days` | Replay chunks and DOM snapshots. |
| `mouse_days` | Mouse-movement chunks. |

## `[enrich]`

| Key | Default | Meaning |
|---|---|---|
| `geo_dir` | `""` → `data_dir/geo` | Folder with `GeoLite2-City.mmdb`, `GeoLite2-ASN.mmdb`, `dbip-city-lite*.mmdb`, `dbip-asn-lite*.mmdb`, `ipinfo_lite.mmdb`… (`jobs.py geo-update` fills it). |
| `ip_header` | `"auto"` | Which address is the visitor's: `auto` (REMOTE_ADDR, unless it is a trusted or private proxy, or a Cloudflare edge sending `CF-Connecting-IP`), `remote_addr`, `x-forwarded-for`, `x-real-ip`, `cf-connecting-ip`, `true-client-ip`, `forwarded`. |
| `trusted_proxies` | `[]` | CIDRs whose forwarding headers are trusted with `auto`. |
| `bot_list` | `""` | Optional `crawler-user-agents.json` with extra bot patterns. |
| `internal_ips` | `[]` | CIDRs flagged as internal traffic (excluded from reports). |
| `maxmind_account_id`, `maxmind_license_key` | `""` | For `jobs.py geo-update --source maxmind` (free GeoLite2 account). |
| `batch_size` | `5000` | Raw hits per enrichment transaction. |

## `[dashboard]`

| Key | Default | Meaning |
|---|---|---|
| `users` | `{}` | `{name = "pbkdf2_sha256$…"}` for HTTP Basic auth. The dashboard is off until at least one user exists. Hash a password with `python -m tidesight.jobs hash-password --user NAME`. |
| `realtime_minutes` | `30` | Window of the realtime view. |

## Sites

`[defaults]` applies to every site; `[sites.<id>]` overrides it. The table name is the site id used by the tracker (`data-site="<id>"`). Sites not in the file are still recorded, flagged `unknown_site`.

| Key | Default | Meaning |
|---|---|---|
| `name` | the id | Display name. |
| `origins` | `[]` | Allowed origins, e.g. `["https://example.com", "https://*.example.com"]`. Empty: any origin. Hits from other origins are stored but flagged. |
| `timezone` | `"UTC"` | IANA zone: days, hours and the midnight session split use it. |
| `session_timeout_min` | `30` | Idle time that ends a session. |
| `split_session_at_midnight` | `true` | Start a new session at midnight in `timezone`. |
| `cookie_domain` | `"auto"` | `auto` (registrable domain, so subdomains share the visitor), `""` (current host only) or a domain. |
| `cookie_days` | `400` | Lifetime of the visitor cookie (browsers cap at 400 days). |
| `server_cookie_domain` | `""` | When set (e.g. `".example.com"`), the collector also sets `ts_svid` on that domain: a longer-lived backup of the visitor id when the collector shares the site's domain. |
| `hash_mode` | `"route"` | Which `#hash` changes are pageviews: `route` (`#/…` and `#!…`), `all`, `off`. |
| `spa_replace` | `"path"` | `history.replaceState` counts as a pageview when the `path` changes, `all`, or `off`. |
| `heartbeat_sec` | `30` | Engagement heartbeat while the page is in use; `0` disables. |
| `idle_sec` | `300` | Engagement time stops after this long without input. |
| `flush_sec` | `5` | How often queued events are sent. |
| `search_params` | `["q", "s", "search", "query", "keyword"]` | URL parameters read as site-search queries. |
| `exclude_paths` | `[]` | Glob patterns where the tracker sends nothing, e.g. `["/admin/*"]`. |
| `download_extensions` | (36 extensions) | File extensions counted as downloads. |
| `public` | `false` | Publish a read-only stats page at `/share/<id>/`. |
| `public_metrics` | `["visitors", "pageviews", "pages", "sources", "countries", "devices"]` | What the public page shows (`events` and `realtime` can be added). |
| `share_token` | `""` | When set, the public page requires `?t=<token>`. |

### Features

`features.<name> = true|false`, in `[defaults]` or per site. Defaults are data-rich; heavy features are off or sampled.

| Feature | Default | | Feature | Default |
|---|---|---|---|---|
| `pageviews` | on | | `scroll` | on |
| `spa` | on | | `forms` | on |
| `engagement` | on | | `vitals` | on |
| `context` | on | | `navigation_timing` | on |
| `bot_signals` | on | | `long_tasks` | off |
| `adblock` | off | | `errors` | on |
| `fingerprint` | on | | `resource_errors` | on |
| `canvas_fingerprint` | on | | `resize` | on |
| `webgl_fingerprint` | on | | `visibility` | on |
| `audio_fingerprint` | on | | `copy_paste` | off |
| `font_fingerprint` | off | | `text_selection` | off |
| `clicks` | on | | `site_search` | on |
| `outbound` | on | | `not_found` | on |
| `downloads` | on | | `mouse_moves` | off |
| `heatmap_clicks` | on | | `replay` | off |
| `rage_clicks` | on | | `dom_snapshots` | off |
| `dead_clicks` | on | | | |
| `error_clicks` | on | | | |

### Feature options

A table named after the feature, e.g. `[sites.example.replay]` or `replay.sample_rate = 0.2` inside `[sites.example]`. Every feature accepts `sample_rate` (0–1, decided per session so a session is entirely in or out; default 1).

| Feature | Options (defaults) |
|---|---|
| `replay` | `sample_rate` 0.1, `mask_all_inputs` true (password fields are always masked), `mask_text_class` "ts-mask", `block_class` "ts-block", `record_canvas` false, `max_minutes` 30 |
| `mouse_moves` | `sample_rate` 0.1, `interval_ms` 100, `max_points` 3000 (per page) |
| `dom_snapshots` | `sample_rate` 0.01 |
| `rage_clicks` | `count` 3, `window_ms` 1000, `radius_px` 30 |
| `dead_clicks` | `wait_ms` 1000 |
| `errors` | `max_per_page` 25 |
| `scroll` | `marks` [25, 50, 75, 90, 100] |
| `clicks` | `text_max` 100 |
