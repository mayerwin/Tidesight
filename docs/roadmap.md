# Roadmap

Status of each phase and what remains. Update this file when a roadmap item lands or the plan changes.

Legend: ✅ done · 🚧 in progress · ⬜ not started

## Phase 0 — Hosting test ✅

✅ `tools/sqlite_host_test.py`: single file, standard library only (Python 3.6+), runnable over SSH, as CGI or under Passenger (token-protected). It reports Python and SQLite versions and features (JSON1, FTS5, UPSERT, window functions, RETURNING, STRICT), the data directory's filesystem (local or network), WAL and DELETE journal modes (and WAL persistence), multi-process insert concurrency with a concurrent reader in both modes (errors, lock retries, throughput, p50/p95/p99 latency), integrity and row counts, concurrent spool appends, whether the data directory is served over HTTP, runtime modules, outbound HTTPS, and a PASS/WARN/FAIL summary with the recommended `storage_mode` and `journal_mode`. Verified locally (Windows, NTFS: PASS with WAL).

✅ **Run on the Dreamhost account** (2026-10-09, shared server `iad1-shared-b8-02`): Python 3.12.3, SQLite 3.45.1 (JSON1, FTS5, upsert, window functions, `RETURNING`, strict tables), data directory on a local XFS filesystem (not NFS) with 699 GB free, outside the web roots; cron and outbound HTTPS available. WAL: 8 writer processes and a reader, 16,000 rows at 1,819 rows/s, p99 78 ms, no lock errors, integrity ok. DELETE journal mode works but is slower (312 rows/s, 13 lock retries: the only warning). Concurrent spool appends intact. **Recommendation: `storage_mode = "sqlite"`, `journal_mode = "wal"`** (the defaults). Still to confirm in the Dreamhost panel when deploying: Passenger for the collector's domain (CGI works otherwise).

## Phase 1 — Collector + core tracker ✅

- Collector (`collect.py`): `/collect`, `/chunk`, `/p.gif`, `/config.js|json`, `/health`, static tracker files; per-site config with hot reload; validation and size limits; flagged storage of malformed and unknown-site hits; per-key rate limiting in the insert transaction; CORS; secrets redaction; privacy switches; optional server cookie; spool mode and automatic spool fallback.
- Schema: `raw_requests` + `raw_hits` (typed hot columns + raw JSON), `replay.db` chunks, numbered migrations.
- Tracker core (`ts.js`, ~8 KB gzip of a 20 KB budget): pageviews with full context and high-entropy Client Hints, SPA routes (History API, hash routes), custom events, identify, visitor id in cookie + localStorage + optional server cookie, sessions (idle timeout, midnight split), engagement (visible, focused, not idle) with heartbeats, batching, gzip, keepalive delivery on unload, bounded offline retry, cached config, plugin loader with per-session sampling.
- Deployment: `tidesight.fcgi` (FastCGI), `collect.cgi`, `passenger_wsgi.py`, `docs/deploy-dreamhost.md`, `config/tidesight.example.toml`.
- Tests: pytest (collector, storage, config, concurrency), Playwright (core).

The core's budget is 20 KB gzip (the build enforces it). The original 4 KB target was dropped: size must never cost functionality or robustness.

## Phase 2 — Enrichment ✅

Client IP decision, geolocation (pure-Python MaxMind DB reader; GeoLite2, DB-IP Lite, IPinfo Lite; downloads), UA + Client Hints parsing, bot classification (UA patterns, automation signals, hosting ASNs), sessions and pageviews rebuilt from hits (clock-skew correction, retry de-duplication, pixel sessions), GA4 channel attribution (including the AI Assistant channel), typed event tables, staged identity stitching with per-id confidence, daily rollups. Versioned: a parser, database or algorithm change rebuilds `derived.db` and swaps it in.

## Phase 3 — Behaviour plugins ✅

Fingerprint and automation signals, clicks with element-relative heatmap coordinates, outbound links and downloads, rage/dead/error clicks, scroll depth, form analytics (never values), Web Vitals, Navigation Timing, long tasks, errors (de-duplicated per page), resize/visibility/copy/selection, mouse movement, ad-blocker detection, site search, 404 pages. Each switchable and sampled.

## Phase 4 — Session replay ✅

rrweb recorder (vendored, lazy-loaded when sampled in), compressed chunks in `replay.db`, masking (passwords always, inputs by default, `ts-mask`/`ts-block`), DOM snapshots for heatmap backgrounds, rrweb player in the dashboard.

## Phase 5 — Dashboard & API ✅

`api.py` (read-only JSON, HTTP Basic auth, public share pages with tokens, CSV/JSON export) and the static dashboard: overview, realtime, pages, sources/channels, geography map, devices, events and properties, funnels, retention cohorts, heatmaps (clicks, rage/dead, mouse, scroll), replays, forms, site search, performance, errors, sessions, visit timelines, visitor profiles, status. `tools/demo.py` generates realistic demo data.

## Phase 5b — Dashboard 2: properties, analysis, exports ✅

- ✅ **Properties in the dashboard:** add (a three-step wizard: domains with www and subdomain options, name, time zone; what to record; the tracking code with a live check for the first hit), configure (general, tracking code with the refused origins and an "Allow" button, goals, data collection switches and sample rates, privacy, public page and share token) and delete (keeping or deleting the data; the deletion runs in `jobs.py cron`) — stored in `properties.json` next to the configuration file's sites, which stay read-only. A property shows its name, else its most precise domain. Data arriving for an unknown id is offered as a new property. `[dashboard] admins` limits who may change properties.
- ✅ **Filters and segments:** any dimension, `is`/`is not`/`contains`/`does not contain`/`starts with`, several values, value suggestions with counts, editable chips, clicks on rows to filter in or out, goal filters; saved segments (per property or for all). The traffic setting reads "Humans only / Humans and bots / Bots only".
- ✅ **Periods:** presets with keyboard shortcuts, a two-month range calendar, previous/next period, and comparisons (previous period, a year before, custom) drawn dashed in charts, with changes in tiles, lists and tables.
- ✅ **Report tables:** server-side sort, search and paging, totals, shares of the total, a second dimension, a column chooser, row actions (filter, exclude, open the report, open the page, heatmap), plotting up to five rows over time, and exports of every matching row (Excel, CSV, TSV, JSON, or copy); every other table exports what it shows. An Explorer for any dimension.
- ✅ **Goals and annotations:** goals per property (events or pages, `*` wildcards) with conversions, rates and values, a goal's conversions in every report; notes on charts; a day-of-week × hour grid of visits (the `weekday` dimension).
- ✅ **Export dialog** that says what each download holds: the reports on screen, and each raw dataset with its description, columns and number of rows.
- ✅ Sign-in page with sessions (instead of the browser's password prompt), a command palette (Ctrl/⌘ K), shortcuts, a freshness indicator, dark mode throughout. Browser tests: `npm run test:dashboard` in `tracker/`.

## Phase 6 — Privacy mode, maintenance, connectors ✅

- ✅ `jobs.py privacy-clean` (IP truncation/hashing with geo kept, fingerprint/replay/mouse deletion, retention windows; dry run first; automatic backup), DNT/GPC honouring, `backup` (online, gzip, rotated), `vacuum`, `archive` (yearly files that enrichment still reads), `export`.
- ✅ Conformance suite for connectors and the resolved JSON configuration.
- ✅ PHP (7.4+, PDO SQLite or SQLite3), Node (22.13+, `node:sqlite`) and Cloudflare Worker (D1) connectors, each passing the conformance suite, plus differential tests against the Python collector on generated inputs. Not yet tried on a live Apache/nginx host or on Cloudflare itself (only on PHP's built-in server and a local D1 shim).

## Security and abuse hardening 🚧

- ✅ Abuse limits (`docs/configuration.md`, "Limits and abuse protection"): per-client allowances for requests and kilobytes (separate for `/chunk`), smaller allowances for flagged traffic (per client and global), client identity from the connecting address (headers only from `rate_limit_proxies`), bounded decompression, nesting and event counts, a disk guard, an optional daily quota per site; the tracker paces itself (events, uploads, retries, request sizes). Mirrored by every connector.
- ✅ Adversarial audits of the Python server and dashboard API, and of the tracker and dashboard UI, with every finding fixed: login throttling, share-page tokens and filters, a CSP built from the sites' origins, bounded replay decoding, CSV formula neutralization, enrichment that isolates poison-pill hits; on the client, DOM-clobbering-proof startup, linear-time URL redaction (`redact_params`, new `redact_paths`) applied to every URL the tracker sends and to the stored `Referer`, rich-text masking in replays, keepalive delivery that sends only the origin as `Referer`.
- ✅ A second audit pass on the Python server, the tracker and the dashboard, and audits of the PHP, Node and Worker connectors, with their findings fixed: early refusal before parsing, bounded enrichment and API work on hostile data (session caps, object budgets), public share pages that never show fewer than 3 visitors, allowance keys that never hold an address, URL redaction inside nested parameter values, rich-text editors' attributes masked in replays, click and error-click text redacted, short maintenance transactions with a spool circuit breaker, a refusal to run with data inside the web root, and connector-specific hardening (timeouts and memory bounds in Node, regular-expression and hosting fixes in PHP).

- ✅ An audit of the new dashboard (sign-in, the properties and changes API, reports and files) with its findings fixed: sign-in attempts counted before the password check (a burst of concurrent guesses stops at the limit, in every process), an unreadable `properties.json` never written over nor dropped by a running process, public filters of one exact value and at most 100 rows, API calls only from the dashboard itself (not from sibling subdomains), sign-out by `POST` only, duplicate session cookies refused, segments and notes changed only by their author or an admin, throttle keys hashed, public call allowances kept in `web.db` under CGI, the dashboard folder limited to its own files, `--no-auth` answering on localhost only, strict date and id checks.

- ✅ **FastCGI on Dreamhost** (2026-10-09). Dreamhost no longer offers Passenger on shared plans, so the web app runs as a FastCGI program under Apache's mod_fcgid (`tidesight.fcgi`, a standard-library FastCGI server in `tidesight/fcgi.py`): one process answers request after request (about 10 ms a request, against 150 ms for CGI), and replaces itself when its code changes. Dashboard users are added with `jobs.py user set NAME` (`data_dir/users.json`), and the nightly backup now keeps `web.db`, `properties.json` and `users.json` too.
- ✅ **Installed on `tidesight.erwinmayer.com`** (2026-10-09): code in `~/tidesight`, data in `~/tidesight-data` (geo databases loaded), FastCGI entry point and `.htaccess` in the web directory, cron jobs (every 2 minutes, nightly backup, monthly geo update, weekly vacuum). Checked over HTTP: health, tracker, configuration, collect (plain), dashboard sign-in and API. DNS record at Cloudflare added. Waiting on the owner: the certificate, the Web Application Firewall off (it refuses compressed batches), and a first user.

## Ideas for later

- Custom channel groupings (e.g. AI traffic beyond GA4's list).
- Managing dashboard users from the dashboard (today: `jobs.py user` on the server, or the configuration file); email reports and alerts (traffic drops, error spikes).
- Funnels saved per property and built from goals.
- An optional consent mode in the tracker (wait for a consent call before setting identifiers).
- Faster enrichment for very large sites (bulk inserts, parallel rebuilds).

## Open questions for the owner

1. **Dreamhost:** settled: `tidesight.erwinmayer.com`, with FastCGI (Passenger is gone from Dreamhost's shared plans). Its DNS lives at Cloudflare (as for `erwinmayer.com`, served by Cloudflare Pages): an `A` record `tidesight` → `173.236.254.123` (DNS only, done 2026-10-09), then the Let's Encrypt certificate and the Web Application Firewall off in the panel.
2. **Dashboard users:** sign-in sessions and admins (`[dashboard] admins`) are in place; are other roles needed?
3. **Public stats page:** which metrics should be public by default, per site?
4. **Licence:** MIT adopted provisionally (rrweb is MIT; GeoLite2 and DB-IP data are never bundled).
5. **Which sites come first?** They will drive the first configuration.
