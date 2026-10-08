# Roadmap

Status of each phase and what remains. Update this file when a phase item lands or the plan changes.

Legend: ✅ done · 🚧 in progress · ⬜ not started

## Phase 0 — Hosting test 🚧

`tools/sqlite_host_test.py`: a single file, standard library only, runnable over SSH **and** as a Passenger/CGI request. It reports:

1. Python version, the `sqlite3` module version and the SQLite library version (JSON1 and FTS5 availability, plus `RETURNING`, which needs SQLite ≥ 3.35).
2. The filesystem type of the data directory (`stat -f`, `/proc/mounts`), to tell local disk from NFS.
3. Whether `PRAGMA journal_mode=WAL` works and survives a reconnect, and whether rollback-journal mode (`DELETE`) works.
4. **Concurrency:** N processes (e.g. 8) each insert M rows (e.g. 2,000) with `busy_timeout`. Reports errors, `database is locked` counts, throughput and p50/p95/p99 insert latency, in both WAL and DELETE modes.
5. **Durability:** `PRAGMA integrity_check` after the run; row counts equal N×M.
6. Whether the data directory is outside the web root and not served over HTTP.
7. Whether `os.fork`/`multiprocessing`, `gzip` and `zlib` are available.

It ends with a PASS/WARN/FAIL summary. Expected outcome: PASS with WAL or DELETE mode. If it fails, use `storage_mode = "spool"`.

**Remaining:** run it on the Dreamhost account and record the result here, then set `storage_mode` and `journal_mode` in the production config.

## Phase 1 — Collector + core tracker (MVP) ⬜

- **Collector:** `/collect`, `/p.gif`, `/config.js`, `/health`; per-site config; input validation and size limits (64 KB per request, larger for chunks); per-IP rate limiting in SQLite; CORS.
- **Schema:** raw storage (`raw_requests`, `raw_hits`) with typed columns for the hottest fields plus JSON for the rest; numbered migrations.
- **Tracker core:** pageviews including SPA navigation (History API + `hashchange`), title, referrer, UTM and click IDs; custom events (`tidesight.track(name, props)`); identity (visitor id, session id with a 30-minute idle timeout, fingerprint signals); engagement time counted only while visible, plus heartbeats; batching and reliable delivery.
- **Deployment:** `passenger_wsgi.py`, `docs/deploy-dreamhost.md`, `config/tidesight.example.toml`.
- **Tests:** pytest for the collector, Playwright for the tracker.

## Phase 2 — Enrichment (re-runnable) ⬜

Each job keeps a version number per row, so changing a parser triggers a recompute.

- **Geo:** country, region, city, latitude/longitude, time zone, ASN/ISP and hosting/VPN flag, from offline databases: MaxMind GeoLite2 City + ASN (free licence key) or DB-IP Lite (no key, CC-BY). Several providers can be stored side by side.
- **Device:** browser, OS, device type, brand and model from the User-Agent and Client Hints.
- **Bots:** known bot UAs, datacenter ASNs, `navigator.webdriver`, headless signals, no-JS hits. Bots are **flagged, never deleted**.
- **Sessions and visitors:** sessions rebuilt from raw hits; identity stitching combines the visitor id, the cookieless hash and the fingerprint into a confidence-scored visitor key; entry/exit pages, bounce, new vs returning, source/medium/channel (GA4-style default channel rules).
- **Rollups:** daily aggregates for the dashboard.

## Phase 3 — Behaviour plugins ⬜

Each switchable, each with a sampling rate.

- Click tracking (selector, text, link target), outbound links, downloads, mailto/tel.
- Scroll depth (25/50/75/90/100 % plus maximum reached).
- Form analytics: field focus, time, abandonment, submit. **Field values are never captured.**
- Rage clicks, dead clicks and error clicks (as in Clarity and Hotjar).
- Web Vitals (LCP, CLS, INP, FCP, TTFB) and Navigation Timing.
- JS errors (message, stack, source, line), unhandled rejections and resource load failures.
- Click heatmaps with element-relative coordinates plus page and viewport size, so heatmaps survive layout changes.
- Mouse-movement heatmaps: sampled points, heavy, stored in `replay.db`.
- Site-search query capture (configurable URL parameter).
- 404 tracking (the site opts in by flagging its error pages).

## Phase 4 — Session replay ⬜

- rrweb recording, lazy-loaded only when the session is sampled in (per-site rate). Chunks are compressed and stored in `replay.db`.
- Masking: password fields always, all inputs by default (configurable), and any element with a `ts-mask` class.
- Player in the dashboard, linked from a session's event timeline.

## Phase 5 — Dashboard & API ⬜

- `api.py`: authenticated read-only JSON (HTTP Basic auth over HTTPS to start).
- Views: realtime-ish (last 30 minutes, polling); overview, sources/channels, pages, entry/exit; geo map, devices/browsers; events, funnels, retention cohorts; performance, errors; heatmaps, replays; per-visitor timeline.
- Optional public stats page per site (selected metrics only), as Plausible does with shared links.
- CSV/JSON export.

## Phase 6 — Privacy mode, maintenance, connectors ⬜

- `jobs.py privacy-clean`: hash or truncate IPs in raw data (/24 for IPv4, /48 for IPv6, or a salted hash); drop fingerprint fields and replay; apply retention windows; honour DNT/GPC from now on. Every step is a separate flag, dry-run first, with an automatic backup.
- Vacuum/optimise; archive old raw data into yearly database files.
- Other connectors (PHP, Node, Cloudflare Worker + D1), conforming to `docs/protocol.md` and the shared conformance suite.

## Open questions for the owner

1. **Dreamhost plan details:** the domain or subdomain for the collector, SSH access, and Passenger availability. Phase 0 answers most of this.
2. **Dashboard authentication:** HTTP Basic is enough to start. Multiple users later?
3. **Public stats page:** which metrics should be public by default, per site?
4. **Licence:** MIT adopted provisionally (rrweb is MIT; GeoLite2 terms must be respected when distributing data, which Tidesight never bundles).
5. **Which sites come first?** They will drive the first config and the tests.
