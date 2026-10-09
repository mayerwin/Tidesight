# Read API

The dashboard is a static page on top of a small read-only JSON API (`server/python/tidesight/api.py`). Scripts can use the same API, for example to pull numbers into a spreadsheet or a report.

## Access

| URL | Access |
|---|---|
| `/dashboard/api/<endpoint>` | HTTP Basic authentication with a `[dashboard] users` account (`docs/configuration.md`). |
| `/share/<site>/api/<endpoint>` | Only for sites with `public = true`, and only `overview`, `breakdown`, `sites` and `realtime`, limited to the site's `public_metrics`. With a `share_token`, send it as the `X-Share-Token` header or `?t=<token>`. |

Public share APIs are restricted further, so a public page cannot single anyone out: breakdowns and filters (`f.<dim>`) only use the dimensions of the site's `public_metrics` (pages, sources, countries, devices, events; never `uid`, `visitor`, `asn`, `timezone`, `hour`…), and only one filter at a time; breakdown rows of fewer than 3 visitors are hidden (a page one person saw may be a reset link with its token in the path); with a filter, every overview number (totals, previous period, each point of the series) of fewer than 3 visitors shows as zero; realtime lists pages and referrers only when those metrics are public and at least 3 visitors were there; exports are not available, and each client may make 120 calls a minute. Wrong tokens count as failed logins.

Stored recordings are anyone's data, so `replay`, `snapshot` and `session` decode a bounded amount per call (bytes and JSON objects); `replay` answers `"truncated": true` when a recording was larger, and only events with rrweb's shape are passed on.

```sh
curl -u admin:… "https://stats.example.com/dashboard/api/overview?site=example&from=2026-09-01&to=2026-09-30"
```

Only `GET` (and `HEAD`) is accepted. Errors answer with a status code and `{"error": "…"}`: `400` for a bad parameter, `404` for an unknown site, endpoint or record, `503` while no enrichment has run yet.

## Common parameters

| Parameter | Meaning |
|---|---|
| `site` | Site id. Required, except for `sites` and `status`. |
| `from`, `to` | Days in the site's time zone, `YYYY-MM-DD`, inclusive. Default: the last 30 days up to today. |
| `f.<dimension>` | Filter on a session dimension, e.g. `f.country=FR`, `f.channel=Organic%20Search`. `(none)` matches empty values. Also `f.page=/pricing` (sessions that viewed the page), `f.path_prefix=/docs/` and `f.event=signup` (sessions with the event). Several filters combine with AND. |
| `bots` | `1` to include bot traffic, which is excluded by default (never on public pages). |
| `limit` | Maximum rows, where the endpoint returns a list. |

Internal traffic (`internal_ips`) and hits flagged at ingest (unknown site, wrong origin) are always excluded.

**Session dimensions**, for `breakdown` and filters: `source`, `medium`, `campaign`, `term`, `content`, `channel`, `category` (source category), `referrer` (domain), `country`, `region`, `city`, `device`, `brand`, `model`, `browser`, `browser_version`, `os`, `os_version`, `screen`, `language`, `timezone`, `asn` (network), `entry`, `exit`, `hour`, `new` (1 for a visitor's first visit), `visitor`, `uid`, `bot`.

Times are UTC milliseconds; durations are milliseconds; rates are fractions (`0.27` is 27 %).

## Endpoints

| Endpoint | Parameters | Returns |
|---|---|---|
| `sites` | | The configured sites (`id`, `name`, `timezone`, `public`, `origins`) and the Tidesight `version`. |
| `status` | | Storage mode, database sizes (MB), the last enrichment (version, time) and the last hit received. |
| `overview` | `interval` (`hour`, `day`, `week`, `month`; chosen from the range when omitted) | `totals` (visitors, sessions, pageviews, bounces, bounce rate, views per session, average duration and engaged time, events, new visitors), the same for the `previous` period of equal length, and a `series` per interval. |
| `breakdown` | `dim`: a session dimension, `page`, `title` or `event` | `rows` with `value`, visitors, sessions, pageviews, bounce rate, average duration and engaged time (pages: entries, exits, average scroll; events: count and total value). |
| `pages` | | Every page with views, visitors, engaged time, scroll depth, entries, exit and bounce rates. |
| `not_found` | | Pages flagged with an error status (`ts:status` or `tidesight('status')`): path, status, views, visitors, last seen and the top referrers that lead there. |
| `events` | `name` (optional) | Without `name`: custom events with counts, visitors and total value. With `name`: a daily series, the total value and the most common values of each property. |
| `realtime` | | Visitors active in the last `realtime_minutes` (read from the raw hits, so it does not wait for enrichment): the count, a per-minute series, top pages and referrers, and the latest hits. |
| `funnel` | `steps`: a JSON list such as `[{"type":"page","value":"/pricing"},{"type":"event","value":"signup"}]` (1–10 steps, `*` wildcards in values) | Sessions reaching each step in order, with the median time from the previous step. |
| `retention` | `period`: `day`, `week` (default) or `month` | Cohorts of visitors by their first visit in the range, and the share returning in each of the following 12 periods. |
| `geo` | | Visitors per country, and city points with coordinates for the map. |
| `performance` | | Web Vitals: the 75th percentile per metric with ratings and a daily series, per-page values, and Navigation Timing medians. |
| `errors` | `grp` (optional) | Error groups with occurrences, sessions, visitors, first and last seen; with `grp`, the latest occurrences of one group with their browser and visitor. |
| `heatmap` | `page` (required), `device` (`desktop`, `tablet`, `mobile`) | Click positions (absolute and relative to the element), the most clicked elements with rage and dead clicks, scroll reach, and a mouse-movement grid. |
| `snapshot` | `page` (required) | The latest DOM snapshot of the page (rrweb events), for heatmap backgrounds. |
| `forms` | | Per form: starts, submissions, abandonments and conversion; per field the interactions, median time, fill rate and how often visitors gave up there. |
| `search` | | Site-search queries with counts and visitors. |
| `sessions` | `offset` | Visits, most recent first: entry and exit pages, source, channel, location, device, new or returning, bot, replay, errors. |
| `session` | `key` (required) | One visit: every derived field, its pageviews and the full timeline of its hits. |
| `visitor` | `key` (required) | A stitched visitor: identifiers, confidence, first and last seen, and their visits. |
| `replays` | | Visits with a session recording. |
| `replay` | `session` (required) | The rrweb events of a visit's recordings. |
| `export` | `what` (`sessions`, `pageviews`, `events`, `errors`, `vitals`, `clicks`, `forms`, `daily`, `visitors`, or `raw` for the raw hits with their request), `format` (`csv`, `json`, `jsonl`), `from`, `to` | A file download (private dashboard only). The same exports are available from the command line: `jobs.py export`. |
