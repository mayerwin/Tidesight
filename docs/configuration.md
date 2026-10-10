# Configuration

One TOML file configures the collector, the jobs and the dashboard. Start from `config/tidesight.example.toml`. Every key is optional; defaults are defined in `server/python/tidesight/config.py` (the single source of truth) and listed below.

**Where it is read from:** `--config <file>` (jobs, dev server), else `$TIDESIGHT_CONFIG`, else `./tidesight.toml`, `~/tidesight/tidesight.toml`, `tidesight.toml` beside the folder of the code (Tidesight in one folder, the code in `app/`: docs/deploy-dreamhost.md) or `<repo>/config/tidesight.toml`. Relative paths inside the file (`data_dir`, `geo_dir`, `bot_list`, `static_dir`, `dashboard_dir`, `log_file`) are relative to the file. The collector re-reads the file within 5 seconds of a change; an invalid file is reported in the log and the previous configuration stays in use.

**Validation:** wrong types and unknown values are errors; unknown keys are warnings (logged, and shown by `jobs.py status`).

**Properties added in the dashboard** live in `data_dir/properties.json` (written by the dashboard, in the shape of `[sites.<id>]` tables) and are merged with this file's sites when it loads: a site defined here wins, and stays read-only in the dashboard. The collector picks a change up within 5 seconds (at once in the process that made it; under CGI, at the next request). A broken entry is reported among the warnings and skipped.

**Other connectors** (PHP, Node, Worker) read a resolved JSON version of this file (properties included): `python -m tidesight.jobs --config tidesight.toml config-json --out tidesight.json` (it contains the secret: mode 600, out of the web's reach). Set `[server] resolved_config_file` and `jobs.py cron` rewrites it whenever the configuration or the properties change.

## `[server]`

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | `"data"` | Databases, spool, backups, generated secret, users. Inside the website's folder only with Tidesight's `.htaccess` (`server/python/web.htaccess`), which sends every request to the app: Tidesight then protects the folder and checks from the outside that it cannot be downloaded (see `allow_data_in_web_root`). |
| `storage_mode` | `"sqlite"` | `"sqlite"`, or `"spool"`: append-only files loaded into SQLite by `jobs.py load-spool` (choose from the Phase 0 test). |
| `journal_mode` | `"wal"` | `"wal"` or `"delete"` (`delete` on network filesystems). |
| `synchronous` | `"normal"` | `"normal"` or `"full"`. |
| `busy_timeout_ms` | `10000` | How long a writer waits for the database lock. With `spool_fallback`, a write that fails sends the next 30 seconds of writes straight to the spool, so requests do not each wait for a lock held by a long job. |
| `max_payload_kb` | `64` | Largest `/collect` body on the wire (the tracker keeps its requests under it). |
| `max_chunk_kb` | `1024` | Largest `/chunk` body. |
| `max_decompressed_kb` | `256` | Largest body after gzip decompression (the tracker's batches stay under 60 KB). |
| `max_events_per_request` | `200` | Events of a batch stored as hits; a longer batch answers 400 and keeps its raw body. |
| `rate_limit_per_minute` | `600` | `/collect` and `/p.gif` requests per client. |
| `rate_limit_kb_per_minute` | `2048` | Kilobytes of `/collect` bodies (after decompression) per client. |
| `chunk_rate_limit_per_minute` | `120` | `/chunk` requests per client. |
| `chunk_kb_per_minute` | `8192` | Kilobytes of `/chunk` bodies per client. |
| `flagged_per_minute` | `30` | Requests per client that would only be stored flagged. |
| `flagged_global_per_minute` | `300` | The same, all clients together. |
| `rate_limit_key` | `"remote_addr"` | What identifies a client: the connecting address, or a request header such as `"cf-connecting-ip"` behind a proxy. The header is believed only from `rate_limit_proxies` and only when it holds an IP address; its last value is used (the one the nearest proxy appended; earlier values come from the client), and a port is ignored. The same identity drives the dashboard's login and share-page throttles. Under WSGI or CGI a client-sent `X_Real_IP` reaches the collector as the same variable as `X-Real-IP`: the front server must drop header names with underscores (nginx does by default; Apache drops them for CGI; check a Caddy or custom proxy), or a client can choose its identity. |
| `rate_limit_proxies` | `[]` | Addresses (CIDRs, or `"cloudflare"` for Cloudflare's published ranges) of the proxies whose `rate_limit_key` header is believed. From anyone else the header is ignored, so a client that reaches the collector directly cannot invent identities or borrow a victim's. List only your own proxies: a broad range such as `"0.0.0.0/0"` would let every client choose its identity again. |
| `rate_limit_ipv6_prefix` | `64` | IPv6 clients are grouped by this network prefix. |
| `min_free_disk_mb` | `512` | Below this much free space in `data_dir`, writes answer 503. |
| `cors` | `"reflect"` | `"reflect"` any `Origin` on ingest endpoints, or `"allowed"` origins only. |
| `accept_ch` | `true` | Send `Accept-CH` for Client Hints. |
| `public_url` | `""` | Public base URL of the collector, e.g. `"https://stats.example.com/"`: the tracking code the dashboard shows uses it (else the dashboard's own address), and the dashboard accepts changes coming from that origin as well as from its own host. |
| `secret` | `""` | Long random string for IP hashing, cookieless hashes and tokens. Empty: generated once in `data_dir/.secret`. Never change it casually: cookieless hashes and hashed IPs depend on it. |
| `static_dir` | `""` | Folder of tracker files served at `/ts.js` and `/ts/…` (default: the repository's `tracker/build`). |
| `dashboard_dir` | `""` | Folder of dashboard files served at `/dashboard/` (default: the repository's `dashboard`). |
| `store_raw_body` | `false` | Also keep the raw body of requests that parsed fine. |
| `spool_fallback` | `true` | When SQLite fails, write the request to the spool instead of answering 503. |
| `allow_data_in_web_root` | `false` | When the data directory, the configuration file or the log file is inside the web server's folder (`DOCUMENT_ROOT`), Tidesight writes `data_dir/.htaccess` (refusing every request), and `jobs.py cron` checks every 10 minutes, through `public_url`, that a file put in each of those folders (the code's too) cannot be downloaded, after making sure `public_url` leads to this installation (its `/health` answers a digest only it can make); if a file can be downloaded, the collector and the dashboard answer `503` (logged, and said in the dashboard) until a later check passes. A check that cannot conclude (no `public_url`, the site unreachable, `public_url` leading elsewhere) changes nothing and says why in `jobs.py status` and on the Status page, which also warn about a configuration file or data directory other accounts can read. `true` skips the check, for folders protected some other way. |
| `log_file` | `""` | Error log; stderr (the host's error log) when empty. Set it under FastCGI on Dreamhost, which does not show a FastCGI program's own messages in the site's error log. |
| `resolved_config_file` | `""` | When set, `jobs.py cron` rewrites this file with the resolved configuration (as `config-json`) whenever it changed: properties added in the dashboard reach the PHP and Node connectors this way. |

## `[headers]`

| Key | Default | Meaning |
|---|---|---|
| `secret_patterns` | `["*token*", "*key*", "*secret*", "*password*", "*auth*", "*session*", "*jwt*", "*assertion*", "*credential*", "*signature*", "*cookie*"]` | Header names (glob, case-insensitive) whose values are replaced by `[redacted]`. `Authorization` and `Proxy-Authorization` always are. The `Referer` is kept with the secrets in its URL redacted (`redact_params`, `redact_paths`), and kept `ts_*` cookie values end at a comma (past it is another Cookie header the server merged in). |
| `drop` | `[]` | Header names never stored. |
| `cookie_names` | `["ts_*"]` | Cookie names whose values are kept; other cookies are only counted. The dashboard's sign-in cookie (`ts_login`) is never kept. |

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
| `users` | `{}` | `{name = "pbkdf2_sha256$…"}`: who may sign in (the dashboard's form, or HTTP Basic for scripts), besides the people managed in the dashboard (`data_dir/users.json`). Usually empty: until a user exists, the dashboard offers to create the first one, with the one-time setup code Tidesight writes in `data_dir/setup-code.txt` (only someone who can read the server's files has it; `jobs.py setup-link` prints the dashboard's address with the code); afterwards admins invite people on the Users page, and send password links to those who forgot theirs. A user defined here wins over `users.json`, is listed on the Users page and changed here (hash a password with `jobs.py hash-password --user NAME`). On the server, `jobs.py user set NAME`, `user link NAME`, `user remove NAME` and `user list` do the same as the Users page. Changing a user's password ends their sessions. |
| `admins` | `[]` | Users who are admins whatever their role in the dashboard (the Users page shows their role as set here, and cannot change it). A user's role is otherwise the one set on the Users page (admins manage properties and people; viewers read every report, save segments and add annotations); a user without one (of this file, or of `users.json` written before roles) is an admin while this list is empty, a viewer otherwise. |
| `session_days` | `30` | How long "Keep me signed in" lasts (a session without it lasts 12 hours). |
| `realtime_minutes` | `30` | Window of the realtime view. |
| `demo` | `false` | A public, read-only demonstration: anyone may open every page of the dashboard without signing in, as an admin sees it, and every change (properties, people, segments, notes, sign-in, updates) answers `403`. Only for made-up data: `tools/demo.py --demo` writes it, and the website's live demo uses it. |
| `asset_origins` | `[]` | Origins, besides the sites' own, that replays, heatmap backgrounds and the framed live page may load images, styles, fonts and media from (e.g. a CDN: `["https://cdn.example.net"]`). The dashboard's Content Security Policy blocks every other host, so a recording sent by anyone cannot make your browser contact a server of their choosing. |

The dashboard signs in with a form and a session cookie (`ts_login`: HttpOnly, `SameSite=Strict`, limited to the dashboard's path; only its hash is stored, in `data_dir/web.db`). It throttles password guessing (after 10 failed sign-ins from a client within 10 minutes, its attempts answer `429` for the rest of the window; the count is kept in memory and in `web.db`, so it holds under CGI too; the client is identified as in `rate_limit_key`), refuses cross-site API requests (`Sec-Fetch-Site: cross-site`), requires its own header and origin on every change (`docs/api.md`), and serves its pages with a strict Content Security Policy (no inline scripts, frames only from the sites' origins; signed out, no site is named at all). `web.db` also holds saved segments and annotations; deleting it signs everyone out and forgets them, nothing else. Throttles there are keyed by a keyed hash of the client, never its address. The dashboard folder serves its own files only (`index.html`, `app.css`, `favicon.svg`, `js/`, `vendor/`), and the dev server's `--no-auth` mode answers on `localhost` only (a rebinding DNS name cannot reach it).

## Sites

`[defaults]` applies to every site; `[sites.<id>]` overrides it. The table name is the site id used by the tracker (`data-site="<id>"`). Sites not in the file are still recorded, flagged `unknown_site`.

| Key | Default | Meaning |
|---|---|---|
| `name` | the id | Display name. Without one, the dashboard shows the most precise domain of `origins` (`blog.example.com`; `example.com` for `example.com` with `www.example.com`). |
| `origins` | `[]` | Allowed origins, e.g. `["https://example.com", "https://*.example.com"]`. Empty: any origin. Hits from other origins are stored but flagged (the property's settings list the refused origins, with a button to allow one). |
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
| `redact_params` | `["*token*", "*password*", "*passwd*", "pwd", "pass", "*secret*", "key", "*_key", "*-key", "*api_key*", "*api-key*", "*apikey*", "*access_key*", "*private_key*", "auth", "*authorization*", "jwt", "otp", "*mfa*", "*session*", "*sessid*", "sid", "ticket", "*signature*", "sig", "*credential*"]` | URL parameters (glob: only `*` is special; case-insensitive) whose values become `redacted` in every URL the tracker sends (http, https, ws, wss): page, referrer, canonical and link URLs, form actions, resource URLs, URLs and `name=value` pairs in error messages and click text, the URLs in recorded pages (replays, snapshots). Query string, `#fragment` and path parameters (`;jsessionid=…`) alike, and a parameter value that is itself a URL with a secret (`?next=%2Freset%3Ftoken%3D…`) is redacted whole; the collector applies the same rules to the `Referer` header it stores. Add e.g. `"code"` for OAuth callbacks; `[]` turns it off. Patterns are matched in linear time (`docs/tracker.md`, "Secrets in URLs"). |
| `redact_paths` | `[]` | Paths that carry a secret (glob, case-insensitive; `*` matches any run of characters, `/` included): what each `*` matched becomes `redacted`, e.g. `["/reset-password/*", "/invite/*/accept"]` turns `/reset-password/9f8e7d` into `/reset-password/redacted`. Applied to the same URLs as `redact_params`, and to hash routes (`#/reset-password/…`). |
| `exclude_paths` | `[]` | Glob patterns (case-sensitive) where the tracker sends nothing, e.g. `["/admin/*"]`; matched against the path and against a hash route's path (`#/admin/…`). |
| `download_extensions` | (36 extensions) | File extensions counted as downloads. |
| `public` | `false` | Publish a read-only stats page at `/share/<id>/`. |
| `public_metrics` | `["visitors", "pageviews", "pages", "sources", "countries", "devices"]` | What the public page shows (`events` and `realtime` can be added). |
| `share_token` | `""` | When set, the public page requires `?t=<token>`. |
| `max_hits_per_day` | `0` (off) | Stored hits per UTC day; beyond it, requests for the site answer 429 until midnight UTC. Off by default: anyone can send hits, so a flood could use the quota up and lock real visitors out for the rest of the day. Set it when bounded storage matters more than availability. |
| `search_console` | `""` | The Search Console property whose figures `jobs.py cron` imports, as Search Console names it: `"sc-domain:example.com"` (a domain property) or `"https://www.example.com/"` (a URL prefix). Needs the Google account connected in the dashboard or with `jobs.py search connect google` (`docs/search-engines.md`). |
| `bing_site` | `""` | The Bing Webmaster Tools site imported likewise, e.g. `"https://www.example.com/"`. |
| `goals` | `[]` | Conversions: `[{name = "Signup", type = "event", match = "signup"}, {name = "Thank you", type = "page", match = "/thank-you*"}]`. A visit converts when it sends a custom event, or views a page, matching `match` (`*` matches anything; `?` and `[` are literal). Each goal gets an `id` from its name (or `id = "…"`). Reports then show conversions and conversion rates (`docs/api.md`); the dashboard edits them for its own properties. |

### Limits and abuse protection

The collector is a public endpoint: anyone can send it data. These layers bound what a single client, junk traffic or a flood can store, without slowing real visitors. Each value is in `[server]` (or per site for the quota); `0` disables one.

| Layer | What it bounds | When exceeded |
|---|---|---|
| Body size | `max_payload_kb`, `max_decompressed_kb` (gzip bombs), `max_chunk_kb`. | `413` (metadata stored), or `400 bad_gzip`. |
| Events per request | `max_events_per_request` hits per request. | `400`; the hits within the limit are stored, the raw body keeps the rest. |
| Per client | Requests and kilobytes per minute, separately for `/collect` + `/p.gif` and for `/chunk`. The kilobytes are what the request stores: its body (after decompression, and at least 256 bytes per hit, however small the events, or the hits as stored if longer), its query string, its headers and kept cookies. A client is its IPv4 address or its IPv6 /64 (one host can use a whole /64). Once a client is over an allowance, its further requests that minute are refused before their body is decompressed or parsed (the costly part). | `429` + `Retry-After`; counted (`rate_limited`, `chunk_rate_limited`). |
| Flagged traffic | Requests that would only be stored flagged (unknown or missing site, wrong or missing origin, malformed body, too many events, a chunk kind the site does not record) have a smaller allowance, per client and for all clients together, so junk cannot crowd out real data. | `429` (`400`/`413` for malformed or oversize requests); counted as `flagged_limited`. |
| Chunk kinds | A site accepts `replay`, `mouse` and `snapshot` chunks only when `replay`, `mouse_moves` or `dom_snapshots` is on for it; others are flagged (`site_ok = 0`) within the flagged allowance. | — |
| Daily quota | `max_hits_per_day` per site, off by default (see the trade-off above). | `429`; counted as `over_quota`. |
| Disk space | `min_free_disk_mb` free in `data_dir`. | `503` + `Retry-After: 300` (the tracker keeps the data and retries). |

Requests that store nothing but a counter (a DNT/GPC drop, an oversize chunk) are counted only within the client's request allowance, so a flood of them is not a flood of writes. The allowance rows (`rate_limits`) are keyed by a keyed hash of the client, never its address, whatever the privacy settings. A batch with a few invalid events still has good hits: it is not flagged. All limits are checked in the same transaction as the write. Counters (`counters` table, shown on the Status page) are recorded under a configured site or under `""`, never under a site id taken from the request, so a flood of invented ids cannot create rows. The stored hits of each site are counted per day (`hits`). Behind a proxy or Cloudflare, set `rate_limit_key` to the header carrying the visitor's address, otherwise every visitor shares the proxy's allowance.

The tracker keeps its own pace too: it caps its events and uploads per page and per minute, splits batches under `max_kb` from `/config.js`, and backs off on `429` and `503`.

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
| `replay` | `sample_rate` 0.1, `mask_all_inputs` true (password fields are always masked; also masks the text of rich-text editors, `contenteditable` and `role="textbox"`, and the links, images and labels inside them), `mask_all_text` false (mask every text node: for pages whose code editors draw what visitors type outside inputs, e.g. Monaco, Ace, CodeMirror 5), `mask_text_class` "ts-mask", `block_class` "ts-block", `record_canvas` false, `max_minutes` 30 |
| `mouse_moves` | `sample_rate` 0.1, `interval_ms` 100, `max_points` 3000 (per page) |
| `dom_snapshots` | `sample_rate` 0.01 |
| `rage_clicks` | `count` 3, `window_ms` 1000, `radius_px` 30 |
| `dead_clicks` | `wait_ms` 1000 |
| `errors` | `max_per_page` 25 |
| `scroll` | `marks` [25, 50, 75, 90, 100] |
| `clicks` | `text_max` 100 |
