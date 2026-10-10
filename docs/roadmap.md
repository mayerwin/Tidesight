# Roadmap

Status of each phase and what remains. Update this file when a roadmap item lands or the plan changes.

Legend: ✅ done · 🚧 in progress · ⬜ not started

## Phase 0 — Hosting test ✅

✅ `tools/sqlite_host_test.py`: single file, standard library only (Python 3.6+), runnable over SSH, as CGI or under Passenger (token-protected). It reports Python and SQLite versions and features (JSON1, FTS5, UPSERT, window functions, RETURNING, STRICT), the data directory's filesystem (local or network), WAL and DELETE journal modes (and WAL persistence), multi-process insert concurrency with a concurrent reader in both modes (errors, lock retries, throughput, p50/p95/p99 latency), integrity and row counts, concurrent spool appends, whether the data directory is served over HTTP, runtime modules, outbound HTTPS, and a PASS/WARN/FAIL summary with the recommended `storage_mode` and `journal_mode`. Verified locally (Windows, NTFS: PASS with WAL).

✅ **Run on a Dreamhost shared server** (2026-10-09): Python 3.12.3, SQLite 3.45.1 (JSON1, FTS5, upsert, window functions, `RETURNING`, strict tables), data directory on a local XFS filesystem (not NFS) with 699 GB free, outside the web roots; cron and outbound HTTPS available. WAL: 8 writer processes and a reader, 16,000 rows at 1,819 rows/s, p99 78 ms, no lock errors, integrity ok. DELETE journal mode works but is slower (312 rows/s, 13 lock retries: the only warning). Concurrent spool appends intact. **Recommendation: `storage_mode = "sqlite"`, `journal_mode = "wal"`** (the defaults). (Passenger, which this plan counted on, turned out to be gone from Dreamhost's shared plans: FastCGI instead, below.)

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

- ✅ **FastCGI on Dreamhost** (2026-10-09). Dreamhost no longer offers Passenger on shared plans, so the web app runs as a FastCGI program under Apache's mod_fcgid (`tidesight.fcgi`, a standard-library FastCGI server in `tidesight/fcgi.py`): one process answers request after request (about 10 ms a request, against 150 ms for CGI), and replaces itself when its code changes. Dashboard users are kept in `data_dir/users.json` (managed in the dashboard since; `jobs.py user` on the server), and the nightly backup now keeps `web.db`, `properties.json` and `users.json` too.
- ✅ **In production on Dreamhost shared hosting** (2026-10-09; in one folder since 2026-10-10: the website's folder holds `app/`, `data/` with the geo databases, `tidesight.toml`, the FastCGI entry point and `.htaccess`), with the cron jobs of the deployment guide (every 2 minutes, nightly backup, monthly geo update, weekly vacuum). Checked over HTTP and HTTPS: health, tracker, configuration, collection of plain and compressed batches and replay-sized bodies (once the hosting's web application firewall was off), dashboard sign-in and API, and the first account created in the dashboard.
- ✅ **The first account in the dashboard** (2026-10-09): while there is no user, the dashboard offers to create one, with a setup code written on the server (`data_dir/setup-code.txt`, or `jobs.py setup-link` for an address with the code) so that a bot finding the new site first cannot claim it. An adversarial review of it found no way in without the code, and four smaller issues, fixed: the dashboard now refuses to run with its data inside the web root (as the collector did), a damaged code file matches nothing and is replaced (written whole, then linked into place), the form re-reads every user file before creating the first user and never signs in as a user of the configuration file, and bodies with lone surrogates answer `400` instead of an error. Two races behind occasional test failures are gone too: `web.db` migrations re-check their version under the write lock, and a new database switched to WAL by several processes at once retries instead of failing.

- ✅ **People in the dashboard** (2026-10-09): a Users page for admins (invitations as one-time links, roles, password links for forgotten passwords, removal, last sign-in), changing one's own password, `jobs.py user link` for a locked-out admin; no email involved.
- ✅ **Tidesight in one folder** (2026-10-09): code (`app/`), configuration and data may live in the website's folder, behind `server/python/web.htaccess`, which sends every request to the app; the data directory gets an `.htaccess` refusing everything, and `jobs.py cron` checks from the outside that its files cannot be downloaded (the collector and the dashboard stop while they can).

- ✅ **Updates in place** (2026-10-10): `jobs.py cron` looks for the latest GitHub release once a day; admins see it next to the version and on the Status page and ask for it with a click; the next cron run downloads, checks and swaps it in, keeping the previous code for `jobs.py update --rollback`. The dashboard's footer links to the repository (with the GitHub mark).

- ✅ **A website and a live demo** (2026-10-10): `mayerwin.github.io/Tidesight` (`site/`, built by `tools/build_site.py`, published by GitHub Actions every six hours), with a live demo in which Tidesight's own dashboard and Python web app run in the browser (Pyodide) on demo data, read-only (`[dashboard] demo`). `tools/demo.py` makes three made-up properties (a surf shop, a magazine, a web app) with a team, annotations and saved segments; `tools/screenshots.mjs` takes the README's and the website's screenshots from them. A release archive (`tidesight.tar.gz`, unpacked as `app/`) comes with every release. Polish found on the way: numeric column headers aligned with their numbers, retention cohorts that stop where the period ends, whole-number axes for counts, funnels that start from the property's goals, the replay player's controls.

## Phase 7 — Beyond the visits 🚧

- ✅ **Search engines** (2026-10-10): Google Search Console (a service account: its key file, an RS256-signed token, standard library only) and Bing Webmaster Tools (an API key), connected once in a property's settings with step-by-step instructions, each property linked to its Search Console property and Bing site. `jobs.py cron` imports into `search.db`: Google's 16 months of history (newest first, empty days found in one call), its recent days again until final, Bing twice a day; errors recorded and retried. The **Search engines** report: clicks, impressions, CTR, average position with comparisons, per query, page, country and device, a query's pages and a page's queries, and next to each landing page the organic visits Tidesight recorded (bounce rate, engaged time, conversions). `docs/search-engines.md`.
- ✅ **Paths** (2026-10-10): a flow of the visits' first pages (from their entry, from a page, or leading to a page) drawn as a Sankey diagram, and what comes before and after one page (entrances per channel, previous and next pages, exits).
- ⬜ **Email reports and alerts:** a weekly summary per property; alerts when traffic drops, errors spike or a goal stops converting. Needs SMTP settings (shared hosts usually allow sending through the account's mailbox).
- ⬜ **Importing history** from GA4, Plausible, Matomo (see below): open question.
- ⬜ **Cross-domain visitors:** one visitor across `site-a.com` and `shop-b.com` (a linker parameter on links between listed domains, as GA4 does, read by the tracker and checked by enrichment).
- ⬜ **Server-side events:** an endpoint with a per-property secret key for backends (purchases, sign-ups, webhooks), joined to the visit when the request carries the visitor id (GA4's Measurement Protocol).
- ⬜ **Consent mode:** the tracker waits for `tidesight('consent', …)` before setting identifiers, and records cookieless, unlinked pageviews meanwhile.
- ⬜ **Video and audio:** plays, pauses, progress (25/50/75/100 %) and completions of `<video>`, `<audio>`, YouTube and Vimeo players, as a plugin.
- ⬜ **Smaller items:** custom channel groupings (AI traffic beyond GA4's list) and content groups; funnels saved per property; ad costs (Google Ads, Meta: cost per conversion, return on ad spend); A/B test reports from an event property; automatic insights (anomalies, notable changes); a WordPress plugin to install the tag.

## Ideas for later

- Faster enrichment for very large sites (bulk inserts, parallel rebuilds).

## Open questions

1. **Importing history:** imported figures are daily aggregates (per page, source, country, device…), never visits, so they would live in their own table and the reports would add them for the days before Tidesight's first (filters across dimensions cannot apply to them, as in Plausible). One-shot imports from stable formats keep the upkeep low: Plausible's CSV export (also what other tools produce), GA4 through its Data API (the service-account code of the search engines), Matomo's Reporting API. Universal Analytics' data no longer exists (Google deleted it in 2024).
2. **Roles:** admins and viewers exist; would a role limited to some properties help?
3. **Public stats pages:** which metrics should be public by default, per site?
