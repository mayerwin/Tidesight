# Roadmap

Status of each phase and what remains. Update this file when a roadmap item lands or the plan changes.

Legend: ✅ done · 🚧 in progress · ⬜ not started

## Phase 0 — Hosting test 🚧

✅ `tools/sqlite_host_test.py`: single file, standard library only (Python 3.6+), runnable over SSH, as CGI or under Passenger (token-protected). It reports Python and SQLite versions and features (JSON1, FTS5, UPSERT, window functions, RETURNING, STRICT), the data directory's filesystem (local or network), WAL and DELETE journal modes (and WAL persistence), multi-process insert concurrency with a concurrent reader in both modes (errors, lock retries, throughput, p50/p95/p99 latency), integrity and row counts, concurrent spool appends, whether the data directory is served over HTTP, runtime modules, outbound HTTPS, and a PASS/WARN/FAIL summary with the recommended `storage_mode` and `journal_mode`. Verified locally (Windows, NTFS: PASS with WAL).

⬜ **Run it on the Dreamhost account** (see `docs/deploy-dreamhost.md`, step 2), record the result here, and set `storage_mode` / `journal_mode` in the production configuration.

## Phase 1 — Collector + core tracker ✅

- Collector (`collect.py`): `/collect`, `/chunk`, `/p.gif`, `/config.js|json`, `/health`, static tracker files; per-site config with hot reload; validation and size limits; flagged storage of malformed and unknown-site hits; per-key rate limiting in the insert transaction; CORS; secrets redaction; privacy switches; optional server cookie; spool mode and automatic spool fallback.
- Schema: `raw_requests` + `raw_hits` (typed hot columns + raw JSON), `replay.db` chunks, numbered migrations.
- Tracker core (`ts.js`, ~8 KB gzip of a 20 KB budget): pageviews with full context and high-entropy Client Hints, SPA routes (History API, hash routes), custom events, identify, visitor id in cookie + localStorage + optional server cookie, sessions (idle timeout, midnight split), engagement (visible, focused, not idle) with heartbeats, batching, gzip, keepalive delivery on unload, bounded offline retry, cached config, plugin loader with per-session sampling.
- Deployment: `passenger_wsgi.py`, `collect.cgi`, `docs/deploy-dreamhost.md`, `config/tidesight.example.toml`.
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

## Phase 6 — Privacy mode, maintenance, connectors ✅

- ✅ `jobs.py privacy-clean` (IP truncation/hashing with geo kept, fingerprint/replay/mouse deletion, retention windows; dry run first; automatic backup), DNT/GPC honouring, `backup` (online, gzip, rotated), `vacuum`, `archive` (yearly files that enrichment still reads), `export`.
- ✅ Conformance suite for connectors and the resolved JSON configuration.
- ✅ PHP (7.4+, PDO SQLite or SQLite3), Node (22.13+, `node:sqlite`) and Cloudflare Worker (D1) connectors, each passing the conformance suite, plus differential tests against the Python collector on generated inputs. Not yet tried on a live Apache/nginx host or on Cloudflare itself (only on PHP's built-in server and a local D1 shim).

## Security and abuse hardening 🚧

- ✅ Abuse limits (`docs/configuration.md`, "Limits and abuse protection"): per-client allowances for requests and kilobytes (separate for `/chunk`), smaller allowances for flagged traffic (per client and global), client identity from the connecting address (headers only from `rate_limit_proxies`), bounded decompression, nesting and event counts, a disk guard, an optional daily quota per site; the tracker paces itself (events, uploads, retries, request sizes). Mirrored by every connector.
- ✅ Adversarial audits of the Python server and dashboard API, and of the tracker and dashboard UI, with every finding fixed: login throttling, share-page tokens and filters, a CSP built from the sites' origins, bounded replay decoding, CSV formula neutralization, enrichment that isolates poison-pill hits; on the client, DOM-clobbering-proof startup, linear-time URL redaction (`redact_params`, new `redact_paths`) applied to every URL the tracker sends and to the stored `Referer`, rich-text masking in replays, keepalive delivery that sends only the origin as `Referer`.
- ✅ A second audit pass on the Python server, the tracker and the dashboard, and audits of the PHP, Node and Worker connectors, with their findings fixed: early refusal before parsing, bounded enrichment and API work on hostile data (session caps, object budgets), public share pages that never show fewer than 3 visitors, allowance keys that never hold an address, URL redaction inside nested parameter values, rich-text editors' attributes masked in replays, click and error-click text redacted, short maintenance transactions with a spool circuit breaker, a refusal to run with data inside the web root, and connector-specific hardening (timeouts and memory bounds in Node, regular-expression and hosting fixes in PHP).

## Ideas for later

- Goals/conversions defined per site (instead of GA4's "2+ pageviews or 10 s" engagement rule only).
- Custom channel groupings (e.g. AI traffic beyond GA4's list).
- Several dashboard users with roles; email reports and alerts (traffic drops, error spikes).
- An optional consent mode in the tracker (wait for a consent call before setting identifiers).
- Faster enrichment for very large sites (bulk inserts, parallel rebuilds).

## Open questions for the owner

1. **Dreamhost:** the collector subdomain, SSH credentials for running the Phase 0 test, and confirmation that Passenger is available.
2. **Dashboard authentication:** HTTP Basic is in place; are several users with different rights needed later?
3. **Public stats page:** which metrics should be public by default, per site?
4. **Licence:** MIT adopted provisionally (rrweb is MIT; GeoLite2 and DB-IP data are never bundled).
5. **Which sites come first?** They will drive the first configuration.
