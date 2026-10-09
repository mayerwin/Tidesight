# Architecture

How the pieces of Tidesight fit together. For the reasoning behind these choices, see `docs/design.md`.

## Overview

```
Browser ── ts.js + lazy plugins ─────► POST /collect   events, batched (fetch keepalive / sendBeacon), text/plain JSON, optionally gzip
                                       POST /chunk     heavy data: replay and mouse-movement chunks
                                       GET  /p.gif     no-JS and email pixel
                                       GET  /config.js per-site feature flags (cached)

collect.py (WSGI) ── validates, bounds, appends ─► events.db   raw_requests + raw_hits
                                                  replay.db   chunks (replay, mouse moves)
                     (storage_mode = "spool")   ─► spool/*.jsonl ── jobs.py load-spool (cron) ─► events.db / replay.db

cron ── jobs.py enrich ─► derived.db: client IP, geo, UA/device, bots, sessions, pageviews, visitors (idempotent, versioned)
cron ── jobs.py rollup ─► derived.db: daily aggregates for fast dashboards
cron ── jobs.py backup ─► SQLite online backups, rotated

dashboard (static) ──► wsgi.py: sign-in (session cookie, or HTTP Basic for scripts), CSRF rules, report files
                       api.py + query.py: reports (filters, comparisons, breakdowns, goals); public share pages
                       admin.py: properties (properties.json), segments and annotations (web.db)
```

## Components

| Component | Where | Role |
|---|---|---|
| Tracker core | `tracker/src/core.js` → `ts.js` | Pageviews (incl. SPA), custom events, identity, sessions, engagement, context, batching and delivery. |
| Tracker plugins | `tracker/src/plugins/*.js` → `ts/<name>.js` | Fingerprint, clicks and links, scroll, forms, rage/dead/error clicks, Web Vitals and timing, errors, mouse movement, session replay (rrweb), and others. Loaded on demand. |
| Collector | `server/python/tidesight/collect.py` | WSGI app for the ingest endpoints. Standard library only. |
| Storage | `server/python/tidesight/storage.py` | SQLite and spool writers, migrations for `events.db` and `replay.db`. |
| Config | `server/python/tidesight/config.py`, `properties.py` | Loads the TOML config, merges per-site settings over defaults, and adds the properties made in the dashboard (`properties.json`; the file's sites win). |
| Enrichment | `server/python/tidesight/enrich/` | Re-runnable jobs that fill `derived.db`. |
| Jobs CLI | `server/python/tidesight/jobs.py` | `enrich`, `rollup`, `backup`, `load-spool`, `privacy-clean`, `archive`, `export`, … run from cron. |
| Web app | `server/python/tidesight/wsgi.py`, `auth.py`, `webdb.py` | Routes the dashboard, its API, share pages and the collector; sign-in sessions and throttles (`web.db`). |
| API | `server/python/tidesight/api.py`, `query.py`, `tabular.py` | Reports for the dashboard and public share pages: the filter and breakdown engine, comparisons, goals, and report files (CSV, TSV, JSON, Excel). |
| Admin | `server/python/tidesight/admin.py` | Adding, changing and deleting properties, segments and annotations. |
| Dashboard | `dashboard/` | Static front end (no build step) on the API: reports with rich tables, period comparisons, filters and segments, property management. |
| Other connectors | `server/php/`, `server/node/`, `server/worker/` | Same wire protocol and schema, checked by the shared conformance suite. |

## Deployment topology

- **Collector domain.** Prefer a subdomain per deployment (e.g. `stats.example.com`). The tracker is served from the same host, never from a third-party CDN.
- **First-party identity.** The tracker sets its identifiers on the *site's* domain with JS cookies and localStorage, as GA's `_ga` does, so identity survives third-party-cookie blocking. When the collector shares the site's registrable domain, it can also set a server cookie (`ts_svid`) as a longer-lived backup.
- **Multi-site.** Each site has an id and a list of allowed origins. Hits from unknown sites or origins are stored, flagged (`site_status`) and kept out of reports, never silently dropped.
- **Shared hosting.** On Dreamhost the collector runs under Passenger (`passenger_wsgi.py`) with a CGI fallback (`collect.cgi`). Static files (tracker, dashboard) can be served directly from the app's `public/` directory. Background work runs from cron. See `docs/deploy-dreamhost.md`.

## Data flow

1. **Collect.** The tracker queues events and flushes them on a timer, when the page is hidden and on `pagehide`. Failed batches are kept in localStorage and retried with backoff.
2. **Ingest.** The collector reads the body (bounded), decompresses gzip if present, parses the JSON envelope, resolves the site and checks the origin, applies the per-IP rate limit, and writes one `raw_requests` row plus one `raw_hits` row per event in a single transaction. Headers are stored as received, except secrets.
3. **Enrich.** Cron runs `jobs.py enrich`, which processes new raw hits incrementally: decides the client IP, looks up geo, parses UA and Client Hints, flags bots, assigns hits to sessions, rebuilds the affected sessions and pageviews, and stitches visitor identity.
4. **Roll up.** `jobs.py rollup` maintains daily aggregates per site for the dashboard's long-range views.
5. **Read.** The API serves the dashboard from `derived.db`; realtime views and per-visitor timelines also read recent raw hits.

## Database files

| File | Written by | Contents |
|---|---|---|
| `events.db` | collector (and `load-spool`) | `raw_requests`, `raw_hits`, `rate_limits`, `meta`. The source of truth. |
| `replay.db` | collector (and `load-spool`) | `chunks`: compressed replay and mouse-movement data. |
| `derived.db` | jobs | Everything computed: lookups, sessions, pageviews, visitors, rollups, job state. Can be deleted and rebuilt. |
| `web.db` | dashboard | Sign-in sessions (token hashes), the sign-in throttle, saved segments, annotations, and the properties whose data `jobs.py cron` is to delete. Deleting it signs everyone out and forgets segments and notes. |
| `properties.json` | dashboard | The properties added in the dashboard, as `[sites.<id>]` tables (written atomically, under a lock). |

The schema and its migrations are documented in `docs/schema.md`.

## Wire protocol

Versioned (`v` field), documented in `docs/protocol.md`, and backward compatible with older trackers that may sit in browser caches. One request may carry a batch of events; every event has a unique id for de-duplication.
