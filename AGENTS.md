# AGENTS.md

Instructions for coding agents (Claude Code, Codex, …) working in this repository. Human-facing documentation lives in `README.md` and `docs/`.

## Project in one paragraph

Tidesight is self-hosted web analytics for any number of sites. A small JavaScript tracker (`tracker/`) sends events to a standard-library-only Python collector (`server/python/`) that appends them, raw, to SQLite. Re-runnable cron jobs derive sessions, geography, devices, bots and identity into a separate database, and a static dashboard reads them through a small JSON API. It must run on cheap shared hosting (Dreamhost: Passenger WSGI or CGI, cron, no daemons).

**Before changing behaviour, read `docs/design.md`** (principles and decisions). Current status and next steps: `docs/roadmap.md`.

## Where things are documented

| Topic | File |
|---|---|
| Principles, decision log, trade-offs | `docs/design.md` |
| Components, data flow, database files | `docs/architecture.md` |
| Wire protocol (endpoints, payloads, versioning) | `docs/protocol.md` |
| SQLite schema and migrations | `docs/schema.md` |
| Every data point, its source and default | `docs/datapoints.md` |
| Configuration reference | `docs/configuration.md` |
| Privacy mode and clean-up jobs | `docs/privacy.md` |
| Deployment on Dreamhost | `docs/deploy-dreamhost.md` |
| Writing another connector | `docs/connectors.md` |
| Phases, status, open questions | `docs/roadmap.md` |

## Repository layout

```
AGENTS.md  README.md  LICENSE
config/tidesight.example.toml     # annotated example configuration
docs/                             # see the table above
tools/sqlite_host_test.py         # Phase 0 hosting test (single file, stdlib only)
tracker/
  src/core.js                     # pageviews, events, identity, sessions, engagement, delivery
  src/plugins/*.js                # behaviour plugins, loaded on demand
  build.mjs                       # esbuild → build/
  build/                          # committed minified bundles (deployed as-is)
  test/                           # Playwright tests + fixture site
server/python/
  tidesight/collect.py            # WSGI ingest app (stdlib only)
  tidesight/storage.py            # SQLite + spool writers, migrations (stdlib only)
  tidesight/config.py             # config loading and defaults
  tidesight/enrich/               # re-runnable enrichment jobs
  tidesight/jobs.py               # cron CLI
  tidesight/api.py                # dashboard read API
  passenger_wsgi.py, collect.cgi  # hosting entry points
  devserver.py                    # local development server
  tests/                          # pytest
server/php/  server/node/  server/worker/   # other connectors
dashboard/                        # static dashboard (no build step)
```

## Setup and commands

Python 3.9+ (on this Windows machine: `py -3.10`); Node 18+ for the tracker build and browser tests.

| Task | Command (from the repo root unless noted) |
|---|---|
| Python tests | `python -m pytest server/python/tests` |
| Connector conformance | `python -m pytest server/python/tests/test_conformance.py` |
| Install tracker tooling | `cd tracker && npm install && npx playwright install chromium` |
| Build the tracker | `cd tracker && npm run build` |
| Tracker tests | `cd tracker && npm test` |
| Local server | `python server/python/devserver.py --config config/dev.toml` |
| Enrichment jobs | `python -m tidesight.jobs --config <file> enrich` (run with `server/python` on `PYTHONPATH`) |
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
- Keep the core small (target < 4 KB gzip for `ts.js`); the build prints sizes.
- Rebuild (`npm run build`) and commit `tracker/build/` with any change to `tracker/src/`.

**SQLite**
- Parameterised queries only.
- Each schema change is a new numbered migration in `storage.py` (raw databases) or `enrich/db.py` (`derived.db`), applied automatically and idempotently. Document it in `docs/schema.md`.
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
