# Tidesight

Self-hosted web analytics that keeps every raw hit. Traffic ebbs and flows like a tide; *sight* is the insight you get from watching it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/overview-dark.png">
  <img alt="The Tidesight dashboard: overview of a demo site with visitors, visits, pageviews, bounce rate and visit duration, a 30-day traffic chart, top pages and channels" src="docs/images/overview.png">
</picture>

- **Collect** with a small JavaScript tracker (`ts.js`, about 6 KB gzip) and plugins loaded on demand.
- **Ingest** with a Python collector that uses only the standard library and appends every hit, raw, to SQLite. It runs on cheap shared hosting: Passenger WSGI or CGI, cron, no daemons.
- **Analyse** with re-runnable jobs that derive sessions, geography, devices, bots, channels and visitor identity, and a dashboard on top of a small JSON API.

Tidesight works for any number of sites and is data-rich by default. A privacy mode (IP truncation or hashing, retention windows, DNT/GPC, no fingerprinting) is a configuration change plus a clean-up job.

## What it records

| | |
|---|---|
| **Traffic** | Pageviews with full context (referrer, UTM and click ids, screen, language, time zone, high-entropy Client Hints), SPA route changes (History API and `#hash` routes), custom events with properties, user ids, 404 pages, site search. |
| **Visitors** | A persistent visitor id (first-party cookie + localStorage, optionally a server-set cookie), sessions with idle timeout, engaged time (visible, focused, not idle), new vs returning, identity stitching across devices and ids. |
| **Behaviour** | Clicks with element-relative heatmap positions, rage, dead and error clicks, outbound links and downloads, scroll depth, form analytics (timings and abandonment, never values), mouse movement, copy and selection, ad-blocker detection. |
| **Quality** | Web Vitals (LCP, INP, CLS, FCP, TTFB), Navigation Timing, long tasks, JavaScript and resource errors (de-duplicated). |
| **Replay** | Session recordings with [rrweb](https://github.com/rrweb-io/rrweb) (sampled, passwords never recorded, inputs masked by default) and DOM snapshots that keep heatmaps accurate when layouts change. |
| **Server side** | Raw IP and every request header (credentials and other cookies redacted), the raw payload, receive time and client time. |
| **Derived** | Geolocation (DB-IP Lite, MaxMind GeoLite2 or IPinfo Lite, read by a pure-Python reader), browser, OS and device from the user agent and Client Hints, bot classification, GA4 channel grouping (including AI assistants), a cookieless daily visitor hash, daily rollups. |

Every family of data points and every heavy feature has a per-site switch, and behaviour plugins have a per-site sampling rate. The full catalogue is in [`docs/datapoints.md`](docs/datapoints.md).

## The dashboard

Overview, realtime, pages (top, entry, exit, not found), sources (channels, referrers, campaigns), geography, devices, events and their properties, funnels, retention cohorts, heatmaps (clicks, rage and dead clicks, mouse, scroll reach), replays, forms, site search, Web Vitals, errors, sessions with a visit timeline, visitor profiles and a status page. It has light and dark themes, a table view for every chart, CSV and JSON export, and optional public share pages per site. Everything it shows comes from a [JSON API](docs/api.md) that scripts can use too.

<table>
  <tr>
    <td width="33%"><img alt="Click heatmap of a pricing page drawn over its DOM snapshot, with the most clicked elements listed beside it" src="docs/images/heatmap.png"></td>
    <td width="33%"><img alt="World map shaded by visitors per country, with city markers" src="docs/images/geography.png"></td>
    <td width="33%"><img alt="Visit timeline: pageviews, Web Vitals, scroll, clicks, an error and a rage click, with the visit's device, location and source" src="docs/images/visit.png"></td>
  </tr>
  <tr>
    <td align="center">Heatmaps</td>
    <td align="center">Geography</td>
    <td align="center">Visit timeline</td>
  </tr>
</table>

## Try it locally

Requires Python 3.9 or later and nothing else.

```sh
python tools/demo.py --out demo-data
python server/python/devserver.py --config demo-data/tidesight.toml --no-auth --port 8790
```

Then open <http://127.0.0.1:8790/dashboard/>. The demo sends 60 days of synthetic traffic from about 4,000 visitors through the real collector and enrichment jobs (pages, campaigns, purchases, errors, recordings, a few bots), which takes about half a minute. `--no-auth` skips the dashboard password; use it only locally.

## Add the tracker to a site

```html
<script async src="https://stats.example.com/ts.js" data-site="example"></script>
```

```js
tidesight('track', 'signup', { plan: 'pro' });   // custom event
tidesight('identify', 'user-42');                 // known user
tidesight('ignore');                              // run once in your own browser to exclude it
```

Every command, the page markup (error pages, masking, elements to leave out), single-page apps, Content Security Policy and the no-JavaScript pixel are covered in [`docs/tracker.md`](docs/tracker.md); switching features per site, in [`docs/configuration.md`](docs/configuration.md).

## Deploy

On Dreamhost or any host with Passenger (or plain CGI) and cron, follow [`docs/deploy-dreamhost.md`](docs/deploy-dreamhost.md): check the host with `tools/sqlite_host_test.py`, copy and edit [`config/tidesight.example.toml`](config/tidesight.example.toml), install `passenger_wsgi.py`, and add the cron lines. Updates are a `git pull`: database migrations run automatically, and when the way data is derived changes, the next enrichment rebuilds it from the raw hits.

A host that runs PHP but not Python web apps can ingest with the [PHP connector](server/php/README.md) and keep Python for the cron jobs only; [Node](server/node/README.md) and [Cloudflare Workers](server/worker/README.md) work too.

## How it works

```
 browser ── ts.js + plugins ──▶ collector (/collect, /chunk, /p.gif)
                                   │  appends raw, flags anything odd, never enriches
                                   ▼
                          events.db  (raw hits)        replay.db  (recordings, mouse, snapshots)
                                   │
                cron: jobs.py enrich, rollup, backup, privacy-clean, …
                                   ▼
                          derived.db  (sessions, pageviews, geo, devices, bots, identity, rollups)
                                   │
                         api.py (JSON) ──▶ dashboard (static, no build step)
```

`derived.db` can be deleted at any time: it is rebuilt from the raw data. Details in [`docs/architecture.md`](docs/architecture.md), and the reasoning behind each choice in [`docs/design.md`](docs/design.md).

Besides the Python collector, connectors for **PHP**, **Node** and **Cloudflare Workers (D1)** speak the same protocol and write the same schema, checked by a shared conformance suite ([`docs/connectors.md`](docs/connectors.md)).

## Status

Every phase of the plan is built and tested: collector, tracker, enrichment, behaviour plugins, session replay, dashboard and API, privacy mode and maintenance jobs, and the PHP, Node and Worker connectors. Still open: running the hosting test on the production Dreamhost account, and trying the PHP and Worker connectors on live hosts. See [`docs/roadmap.md`](docs/roadmap.md).

## Development

| Task | Command |
|---|---|
| Python tests | `python -m pytest` |
| Tracker build and browser tests | `cd tracker && npm install && npx playwright install chromium && npm test` |
| Connector conformance | `TIDESIGHT_CONNECTORS=php python -m pytest server/python/tests/test_conformance.py` |

Contributors and coding agents: start with [`AGENTS.md`](AGENTS.md).

## Documentation

- [Design principles and decisions](docs/design.md)
- [Architecture](docs/architecture.md)
- [Using the tracker](docs/tracker.md)
- [Read API](docs/api.md)
- [Wire protocol](docs/protocol.md)
- [Database schema](docs/schema.md)
- [Data-point catalogue](docs/datapoints.md)
- [Configuration](docs/configuration.md)
- [Deploying on Dreamhost](docs/deploy-dreamhost.md)
- [Privacy mode and clean-up jobs](docs/privacy.md)
- [Connectors and writing a new one](docs/connectors.md)
- [Roadmap](docs/roadmap.md)

## Licence

MIT, see [`LICENSE`](LICENSE). Vendored third-party code keeps its own licence: rrweb and rrweb-player (MIT), the Fraunces and IBM Plex fonts (SIL OFL 1.1), and country outlines from Natural Earth via world-atlas (ISC). Geolocation databases are downloaded by the `geo-update` job, never bundled; DB-IP Lite data requires attribution (CC BY 4.0), which the dashboard shows.
