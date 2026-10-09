# AGENTS.md

Instructions for coding agents (Claude Code, Codex, …) working in this repository. Human-facing documentation lives in `README.md` and `docs/`.

## Project in one paragraph

Tidesight is self-hosted web analytics for any number of sites. A small JavaScript tracker (`tracker/`) sends events to a standard-library-only Python collector (`server/python/`) that appends them, raw, to SQLite. Re-runnable cron jobs derive sessions, geography, devices, bots and identity into a separate database, and a static dashboard reads them through a small JSON API. It must run on cheap shared hosting (Dreamhost: FastCGI or CGI, cron, no daemons).

**Before changing behaviour, read `docs/design.md`** (principles and decisions). Current status and next steps: `docs/roadmap.md`.

## Where things are documented

| Topic | File |
|---|---|
| Principles, decision log, trade-offs | `docs/design.md` |
| Components, data flow, database files | `docs/architecture.md` |
| Installing and using the tracker (commands, markup, CSP) | `docs/tracker.md` |
| Wire protocol (endpoints, payloads, versioning) | `docs/protocol.md` |
| Dashboard API (reports, changes, sign-in, files) for the dashboard and scripts | `docs/api.md` |
| SQLite schema and migrations | `docs/schema.md` |
| Every data point, its source and default | `docs/datapoints.md` |
| Configuration reference | `docs/configuration.md` |
| Privacy mode and clean-up jobs | `docs/privacy.md` |
| Deployment on Dreamhost | `docs/deploy-dreamhost.md` |
| Connectors (PHP, Node, Worker) and writing another | `docs/connectors.md` |
| Phases, status, open questions | `docs/roadmap.md` |

## Repository layout

```
AGENTS.md  README.md  LICENSE  pytest.ini
config/tidesight.example.toml     # annotated example configuration
docs/                             # see the table above
tools/sqlite_host_test.py         # Phase 0 hosting test (single file, stdlib only, Python 3.6+)
tools/demo.py                     # realistic demo data through the real collector and jobs
tracker/
  src/core.js                     # pageviews, events, identity, sessions, engagement, delivery, plugin API
  src/plugins/*.js                # fp, clicks, scroll, forms, perf, errors, ux, mouse, replay, adblock
  src/lib/*.js                    # helpers bundled into plugins (selectors, chunk upload)
  build.mjs                       # esbuild → build/ (core budget 20 KB gzip, enforced)
  build/                          # committed bundles, deployed as-is (incl. vendored rrweb recorder)
  test/                           # Playwright specs, fixture site, test config
  test/dashboard/                 # dashboard browser tests (playwright.dashboard.config.mjs, serve.py)
server/python/
  tidesight/collect.py            # WSGI ingest app (stdlib only)
  tidesight/storage.py            # SQLite + spool writers, raw migrations (stdlib only)
  tidesight/config.py, _toml.py   # config loading, defaults, resolved JSON; TOML fallback parser
  tidesight/anonymize.py          # IP truncation/hashing (collector and privacy-clean)
  tidesight/enrich/               # pipeline, sessions, identity, rollup, geo/mmdb, ua, bots, sources, db
  tidesight/api.py, query.py, tabular.py  # reports API, filter/breakdown engine, report files (CSV/TSV/JSON/XLSX)
  tidesight/wsgi.py, auth.py, webdb.py    # web app (dashboard + share pages + collector), sign-in sessions, web.db
  tidesight/admin.py, properties.py       # properties, segments, annotations (properties.json is stdlib-only: config loads it)
  tidesight/jobs.py, maintenance.py    # cron CLI: enrich, rollup, backup, privacy-clean, archive, export…
  tidesight/fcgi.py               # standard-library FastCGI server (mod_fcgid), CGI fallback, code-change restart
  tidesight/users.py              # dashboard users in data_dir/users.json (jobs.py user set/remove/list)
  tidesight.fcgi, collect.cgi, passenger_wsgi.py   # hosting entry points (copy into the web directory)
  devserver.py                    # local server (--no-auth, --test-api, --static)
  tests/                          # pytest, incl. test_conformance.py for every connector
server/php/  server/node/  server/worker/   # other connectors (same protocol and schema)
dashboard/
  index.html, app.css, js/        # static dashboard, ES modules, no build step (views/ one module per page)
  vendor/                         # committed: fonts, rrweb-player, world map (refresh: npm run vendor)
```

## Setup and commands

Python 3.9+ (on this Windows machine: `py -3.10`); Node 18+ for the tracker build and browser tests (22.13+ for the Node connector); PHP 7.4+ to run the PHP connector's conformance tests.

| Task | Command (from the repo root unless noted) |
|---|---|
| Python tests | `python -m pytest` (all of `server/python/tests`) |
| Connector conformance | `TIDESIGHT_CONNECTORS=php python -m pytest server/python/tests/test_conformance.py` |
| Install tracker tooling | `cd tracker && npm install && npx playwright install chromium` |
| Build the tracker | `cd tracker && npm run build` |
| Tracker tests | `cd tracker && npm test` (builds first; starts the dev server on ports 8787/8788) |
| Dashboard tests | `cd tracker && npm run test:dashboard` (demo data; servers on 8795 open, 8796 with sign-in) |
| Demo data + dashboard | `python tools/demo.py --out demo-data` then `python server/python/devserver.py --config demo-data/tidesight.toml --no-auth --port 8790` → `/dashboard/` |
| Refresh dashboard vendor files | `cd dashboard && npm install && npm run vendor` |
| Jobs (cron) | `cd server/python && python -m tidesight.jobs --config <file> cron` (also `enrich`, `status`, `backup`, …) |
| Phase 0 hosting test | `python tools/sqlite_host_test.py --help` |

## Hard rules

These are non-negotiable. The reasoning is in `docs/design.md`.

1. **Raw first.** Store every hit as received (raw IP, headers, full payload). Derive everything else in re-runnable, versioned jobs. Never make ingest depend on enrichment, and never discard raw fields at ingest unless a config switch says so.
2. **Never lose data, never break the host site.** The tracker must never let an exception reach the page. The collector answers fast, validates and bounds input, and stores malformed or unknown-site hits flagged rather than dropping them.
3. **Standard library only** in `collect.py`, `storage.py`, `config.py` and anything they import. No pip dependencies for the collector. Optional libraries are allowed only in enrichment jobs and must degrade gracefully when absent.
4. **No heavy work in the request path:** no enrichment, network calls, or slow parsing in `collect.py`.
5. **Never store secrets** from request headers: keep only Tidesight's own cookie values, a presence flag for `Authorization`, and redact headers matching the secret patterns.
6. **Everything is configurable per site.** New data-point families and features get a switch (and a sampling rate for behaviour plugins), with defaults that follow `docs/datapoints.md`.
7. **Privacy mode is a config change plus a script**, never a rewrite. Keep raw IP, visitor id and fingerprint data in places a clean-up job can find.
8. **Generic, multi-site.** Never hard-code anything about a particular site.
9. **Neutral visitor-visible names** with the `ts` prefix (`ts.js`, `ts_vid`). Nothing that sounds like surveillance.
10. **No third-party CDNs** in the tracker or dashboard: everything is served from the Tidesight host. Vendored libraries keep their licence file.

## Conventions

**Python**
- Compatible with Python 3.9 (Dreamhost may lag): no `match`, no `X | Y` type unions at runtime, no `tomllib` without the bundled fallback.
- Type hints, small functions, module docstrings. `pytest` for tests.

**JavaScript tracker**
- Plain ES2019 in `src/`, no runtime dependencies in core. Each plugin is a separate file registered through the core plugin API.
- Every handler and public function is wrapped so it cannot throw into the host page.
- Trust nothing on the host page: user content can shadow globals and `document` properties (DOM clobbering). Accept `window.tidesightConfig` only as a plain object, read `document` properties through their accessors (`dget` in `core.js`; tests fake visibility on `Document.prototype`, see `hidePage`), and load scripts only from the core's base (`api.load`).
- Never build a regular expression from configured patterns or page data: globs are matched in linear time (`spans` in `core.js`, `redact.py` on the server); URL redaction follows `server/python/tests/redact_vectors.json` everywhere.
- The build fails above 20 KB gzip for `ts.js`: the owner's budget (ask before going over it). Size never justifies weaker functionality or robustness; heavy or optional behaviour still belongs in a plugin, loaded only where it is used.
- Rebuild (`npm run build`) and commit `tracker/build/` with any change to `tracker/src/`.

**SQLite**
- Parameterised queries only.
- Each schema change is a new numbered migration in `storage.py` (raw databases) or `enrich/db.py` (`derived.db`), applied automatically and idempotently. Document it in `docs/schema.md`. Raw-schema changes must be mirrored in the PHP, Node and Worker connectors (the conformance suite compares schemas).
- API queries joining a typed table to `sessions` drive from the typed table (`CROSS JOIN`) and constrain its `site_id` and `day`; check `EXPLAIN QUERY PLAN` on demo data (`tools/demo.py`) for anything new.
- Heavy data goes to `replay.db`; derived data goes to `derived.db`; `events.db` stays raw.
- Timestamps are UTC milliseconds (integers). Keep both server receive time and client time.
- Raw payloads and header maps are JSON text. Promote a field to a real column only when queries need it.

**Wire protocol**
- Versioned (`v` field). Document every change in `docs/protocol.md`. Stay backward compatible with older trackers that may sit in browser caches.

**Documentation**
- English. When behaviour changes, update the matching doc in `docs/` in the same commit, and the status in `docs/roadmap.md` when a roadmap item lands.

## Testing

- **Collector:** pytest covers malformed input, oversize payloads, unknown sites, concurrent writes from multiple processes, gzip and beacon content types.
- **Tracker:** Playwright on the fixture site covers pageviews, SPA route changes (History API and `#hash` routes), clicks, scroll, Web Vitals, errors, unload delivery, offline retry, and asserts that no exception reaches the host page.
- **Enrichment:** fixture-based; re-running a job is idempotent and gives the same output.
- **Connectors:** every connector passes the shared conformance suite.

## Workflow

- Small, descriptive commits. Run the relevant tests before committing.
- Do not commit real data, databases, GeoIP files or secrets (`.gitignore` covers the usual paths).
