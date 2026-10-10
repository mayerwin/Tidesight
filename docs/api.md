# Dashboard API

The dashboard is a static page on top of a small JSON API: reads in `server/python/tidesight/api.py` (with the report engine in `query.py`), changes in `admin.py`, sign-in and files in `wsgi.py`. Scripts can use the same API, for example to pull numbers into a spreadsheet or to add properties.

## Access

| URL | Access |
|---|---|
| `/dashboard/api/<endpoint>` | A signed-in session (the `ts_login` cookie set by `login`), or HTTP Basic authentication with a `[dashboard] users` account on each request (`docs/configuration.md`). |
| `/share/<site>/api/<endpoint>` | Only for sites with `public = true`, and only `overview`, `breakdown`, `sites` and `realtime`, limited to the site's `public_metrics`. With a `share_token`, send it as the `X-Share-Token` header or `?t=<token>`. |

**Signing in.** Passwords travel over HTTPS only: when `public_url` is `https://`, `login`, `setup`, `password` and `password_link` answer `403` to a plain-HTTP request (except on `localhost`), and the dashboard's page sends `Strict-Transport-Security`. `POST /dashboard/api/login` with `{"user": …, "password": …, "remember": true}` sets the `ts_login` cookie: random, HttpOnly, `SameSite=Strict`, limited to the dashboard's path, `Secure` over HTTPS; web.db keeps only its SHA-256. It lasts `[dashboard] session_days` with `remember`, 12 hours otherwise, and ends at `POST logout`, or when the user's password hash changes or the user is removed from the configuration. `GET me` tells who is signed in: `{user, admin, auth: "session"|"basic"|"none", can_sign_out, can_change_password, public_url}`; while Tidesight has no user at all, it answers `{"user": null, "setup": true}` (every other endpoint `403` with `"setup": true`). With `[dashboard] demo = true` (a read-only demonstration), nobody signs in: `me` answers `{"user": "demo", "admin": true, "auth": "none", "demo": true, …}`, every read works, and every change answers `403` with `"demo": true`. Without valid credentials the API answers `401`; requests carrying the `X-Tidesight` header (the dashboard's) get no `WWW-Authenticate` challenge, so the browser shows the dashboard's own form rather than its password prompt. Failed sign-ins count per client (10 within 10 minutes, then `429`), in memory and in web.db, so the limit holds across processes (CGI) and restarts.

**Changes** (`POST`, `PUT`, `PATCH`, `DELETE`) take a JSON body (`Content-Type: application/json`, at most 64 KB) and must carry an `X-Tidesight` header (any value): a page on another origin cannot add it without a CORS permission this API never grants. When the request has an `Origin`, it must be the dashboard's own host (or `[server] public_url`'s). Every API request a browser makes must come from the dashboard itself: a `Sec-Fetch-Site` other than `same-origin` or `none` is refused, so neither another site nor a sibling subdomain (which `SameSite` cookies do not keep out) can make a signed-in browser call it. A session cookie sent twice (one planted by a sibling subdomain) is not trusted.

**Who may change what.** Users listed in `[dashboard] admins` (every user when the list is empty) add, change and delete properties; every user may save segments (200 per property, and 200 for every property) and annotations (2,000 per property), and change or delete their own, admins anyone's. Properties defined in the configuration file are read-only through the API.

**Public share APIs** are restricted further, so a public page cannot single anyone out: breakdowns and filters only use the dimensions of the site's `public_metrics` (pages, sources, countries, devices, events; never `uid`, `visitor`, `asn`, `timezone`, `hour`…), only one exact value (`is`, a single value: a group with and without a small value would reveal it), only one filter at a time, and no comparison or goal; breakdowns return at most 100 rows; breakdown rows of fewer than 3 visitors are hidden (a page one person saw may be a reset link with its token in the path); with a filter, every overview number (totals, previous period, each point of the series) of fewer than 3 visitors shows as zero; realtime lists pages and referrers only when those metrics are public and at least 3 visitors were there; files and exports are not available, and each client may make 120 calls a minute (counted in web.db under CGI). Wrong tokens count as failed sign-ins.

Stored recordings are anyone's data, so `replay`, `snapshot` and `session` decode a bounded amount per call (bytes and JSON objects); `replay` answers `"truncated": true` when a recording was larger, and only events with rrweb's shape are passed on.

```sh
curl -u admin:… "https://stats.example.com/dashboard/api/overview?site=example&from=2026-09-01&to=2026-09-30&compare=previous"
curl -u admin:… -H "X-Tidesight: 1" -H "Content-Type: application/json" \
     -d '{"domains": ["shop.example.com"], "timezone": "Europe/Paris"}' https://stats.example.com/dashboard/api/properties
```

Errors answer with a status code and `{"error": "…"}`: `400` for a bad parameter or setting, `401` without valid credentials, `403` for what the user may not do, `404` for an unknown site, endpoint or record, `409` for a conflict (an id taken, a property of the configuration file), `413`/`415` for a body too large or not JSON, `429` after too many failed sign-ins, `503` while no enrichment has run yet.

## Common parameters

| Parameter | Meaning |
|---|---|
| `site` | Site (property) id. Required, except for `sites`, `status`, `me` and `properties`. |
| `from`, `to` | Days in the site's time zone, `YYYY-MM-DD`, inclusive. Default: the last 30 days up to today. |
| `filters` | A JSON list of filters, all of which must match: `[{"dim": "country", "op": "is", "values": ["FR", "BE"]}, {"dim": "page", "op": "contains", "values": ["/docs"]}]`. Operators: `is`, `is_not`, `contains`, `not_contains`, `starts_with` (texts ignore case; `%` and `_` are literal); `hour`, `weekday`, `new` and `goal` take `is` and `is_not`. A value of `(none)` stands for a missing one, which never matches `is_not` or `not_contains` texts. Filters on `page` and `event` keep the visits that viewed such a page or sent such an event (`is_not`: that never did) — in a page or event report they select the rows themselves. `goal` keeps the visits that completed one of the listed goals. At most 20 filters, 200 values each and 300 in all. |
| `f.<dimension>` | The older form, still accepted: `f.country=FR` is `{"dim": "country", "op": "is", "values": ["FR"]}`; `f.path_prefix=/docs/` is `page starts_with /docs/`. |
| `bots` | `exclude` (default: visits detected as bots are left out), `include` (every visit; `1` is accepted), or `only`. Public pages always exclude them. |
| `compare` | `previous` (the same number of days just before), `year` (the same days a year before), or `custom` with `compare_from` and `compare_to`. `overview`, `breakdown` and `goals` then answer for both periods. |
| `goal` | A goal id of the site (`sites` lists them): adds `conversions` (visits that completed the goal) and `conversion_rate` (per visit) to `overview` and `breakdown`. |
| `limit`, `offset` | Rows of a list, and how many to skip (paging). |

Internal traffic (`internal_ips`) and hits flagged at ingest (unknown site, wrong origin) are always excluded.

**Dimensions.** Session dimensions, usable in every report and filter: `source`, `medium`, `campaign`, `term`, `content`, `channel`, `category` (source category), `referrer` (domain), `country`, `region`, `city`, `device`, `brand`, `model`, `browser`, `browser_version`, `os`, `os_version`, `screen`, `language`, `timezone`, `asn` (network), `entry`, `exit`, `hour`, `weekday` (0 for Sunday to 6, of the site's local day), `new` (1 for a visitor's first visit), `visitor`, `uid`, `bot`. Page dimensions `page` and `title` make a report count pageviews; `event` makes it count events.

**Metrics.** Visit reports: `visitors`, `sessions`, `pageviews`, `views_per_session`, `bounce_rate`, `avg_duration`, `avg_engaged`, `new_visitors`. Page reports: `pageviews`, `visitors`, `sessions`, `avg_time`, `avg_engaged`, `avg_scroll`, `entries`, `exits`, `exit_rate`, `bounce_rate`. Event reports: `count`, `visitors`, `sessions`, `total_value`, `avg_value`.

Times are UTC milliseconds; durations are milliseconds; rates are fractions (`0.27` is 27 %).

## Reports

| Endpoint | Parameters | Returns |
|---|---|---|
| `sites` | | The sites (`id`, `name`, `label` — the name, else the most precise domain —, `domains`, `timezone`, `public`, `managed` for properties added in the dashboard, `goals`, `first_day` with data), the Tidesight `version`, and `freshness`: when enrichment last ran and when the last hit arrived. |
| `overview` | `interval` (`hour` of the day, `day`, `week` from Monday, `month`; chosen from the range when omitted), `goal`, `compare` | `totals` (visitors, sessions, pageviews, bounces, bounce rate, views per session, average duration and engaged time, events, new visitors; conversions with a goal), `previous` (the comparison period, or the same number of days before), a `series` per interval, `compare` (`from`, `to`, `totals`, `series`) when comparing, and the period's `annotations`. |
| `breakdown` | `dim`, `dim2` (a second dimension), `sort` (a metric, `value` or `value2`), `dir` (`asc`, `desc`), `limit`, `offset`, `q` (rows whose value contains it), `values` (a JSON list of up to 300: only these rows), `goal`, `compare` | `rows` (`value`, `value2`, the scope's metrics; with a comparison each row's `cmp`), `totals` over every matching row, `total_rows`, `metrics`, `columns` (labels), `scope` (`session`, `page` or `event`), and with a comparison `cmp_totals` and `compare`. |
| `breakdown_series` | `dim`, `values` (JSON list, up to 10), `metric`, `interval` | One metric over time for those rows: `keys` (the buckets) and a `series` per value — the report's "plot rows". |
| `goals` | `goal` (the one charted), `interval`, `compare` | Each goal with `conversions`, converted `visitors`, `conversion_rate` and the summed `value` of matching events (each with `cmp` when comparing), the visits of the period, and the chosen goal's series. |
| `pages` | | Every page with views, visitors, engaged time, scroll depth, entries, exit and bounce rates (the `page` breakdown). |
| `not_found` | | Pages flagged with an error status (`ts:status` or `tidesight('status')`): path, status, views, visitors, last seen and the top referrers that lead there. |
| `events` | `name` (optional) | Without `name`: custom events with counts, visitors and total value. With `name`: a daily series, the total value and the most common values of each property. |
| `realtime` | | Visitors active in the last `realtime_minutes` (read from the raw hits, so it does not wait for enrichment): the count, a per-minute series, top pages and referrers, and the latest hits. |
| `live` | | `active`: visitors seen in the last 5 minutes (one indexed count of the raw hits). |
| `funnel` | `steps`: a JSON list such as `[{"type":"page","value":"/pricing"},{"type":"event","value":"signup"}]` (1–10 steps, `*` wildcards in values) | Sessions reaching each step in order, with the median time from the previous step. |
| `retention` | `period`: `day`, `week` (default) or `month` | Cohorts of visitors by their first visit in the range, and the share returning in each of the following periods (at most 12; a cohort's list stops at the end of the range). |
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
| `status` | | Storage mode, database sizes (MB), the last enrichment (version, time), the last hit received, flagged requests and today's counters. |
| `annotations` | | The notes of the period (`id`, `day`, `text`, `created_by`). |
| `segments` | | Saved segments of the site and those for every site (`id`, `site_id`, `name`, `filters`). |

## Files

| Request | Downloads |
|---|---|
| `breakdown`, `overview` or `goals` with `format=csv`, `tsv`, `json` or `xlsx` | The report as a file: every matching row (up to 100,000 with `limit`), with the same filters, sort, comparison and goal. Durations in seconds, rates as fractions (a percentage format in Excel); CSV and TSV start with a byte-order mark for spreadsheets and never begin a cell with `=`, `+`, `-` or `@`; JSON (`meta`, `columns`, `rows`) and Excel (an "About" sheet) say what was exported: property, period, comparison, filters, traffic. |
| `export` with `what`, `format` (`csv`, `json`, `jsonl`), `from`, `to` | Raw rows of one dataset: `sessions`, `pageviews`, `events`, `errors`, `vitals`, `clicks`, `forms`, `daily`, `visitors`, or `raw` (the raw hits with their request: IP, headers, envelope). Unfiltered, flags as columns. Days are the site's, except `raw` and `visitors` (UTC days of receipt and last visit). Also `jobs.py export`. |
| `export_catalog` | What each `export` dataset holds: label, description, columns, and its number of rows for the site and period. |

## Changes

| Request | Body | Effect |
|---|---|---|
| `GET properties` | | Every property with its domains, status (`last_hit`, `hits_today`, visits per day for 30 days), `managed`; ids that sent data in the last two days without being configured (`unknown`); deletions waiting for the next job run; whether the user `can_edit`. |
| `GET property?site=` | | One property's settings (name, origins, time zone, goals, features, privacy, options, public page, share token for admins, limits…), and what arrives: the last hit, hits per day for 14 days, refused or flagged requests of the last 7 days, and the origins refused (`rejected_origins`). |
| `POST properties` | `domains` (or `origins`): `example.com`, `*.example.com`, `http://localhost:3000`…; optional `name`, `timezone`, `id`, `preset` (`standard`, `privacy`, `complete`), and any setting below | Adds a property to `properties.json`; the id is made from the name or domain unless given. The collector accepts its hits at once. `409` when the id is taken or its data is still being deleted. |
| `PUT property?site=` | Any of `name`, `origins`, `timezone`, `public`, `public_metrics`, `share_token` (`"generate"`, or `""` for none), `goals` (`[{name, type: event\|page, match}]`), `max_hits_per_day`, `session_timeout_min`, `exclude_paths`, `redact_paths`, `redact_params`, `search_params`, `hash_mode`, `spa_replace`, `features` (`{name: true\|false}`), `privacy` (`hash_ip`, `truncate_ip`, `honour_dnt`, `honour_gpc`, `drop_fingerprint`), a feature's options (`{"replay": {"sample_rate": 0.2}}`). `null` resets a setting to its default. | Changes a property, checked like the configuration file. A domain needs a dot (or is `localhost` or an IP); `*.` covers the subdomains of a name, never of a top-level domain. |
| `DELETE property?site=` | `confirm` (the id), `delete_data` (`true` to delete everything stored for it) | Removes the property. Its data stays (a property re-created with the same id shows it again), or is deleted by the next `jobs.py cron` run. |
| `POST segments` | `name`, `filters` (as above), `site` (or none: every property) | Saves a segment. `PUT segment?id=` renames it or changes its filters; `DELETE segment?id=` deletes it. |
| `POST annotations` | `site`, `day`, `text` (up to 500 characters) | A note shown on the property's charts. `DELETE annotation?id=&site=` removes it. |
| `POST login`, `POST logout` | `{user, password, remember}` | See "Signing in" (`logout` takes `POST` only). |
| `POST setup` | `{code, user, password, remember}` | The first user, only while there is none: `code` is the setup code of `data_dir/setup-code.txt` (case, spaces and dashes do not matter). Creates the user (an admin), deletes the code and signs in (`200` with the cookie); `401` for a wrong code (counted like a failed sign-in: `429` after 10), `400` for a bad name or a password under 8 characters, `409` once a user exists. |
| `GET users` | | Admins only: `{users: [{name, role: admin\|viewer, source: dashboard\|config, status: active\|invited\|invitation expired, you, last_sign_in, link: {kind, expires_at}, created_at, created_by}]}`. |
| `POST users` | `{name, role}` | Admins only: invites someone. The user exists without a password; the answer's `token` makes the link `<dashboard>/#password=<token>`, good once, for a week. `409` if the name is taken. |
| `PATCH user?name=` | `{role}` | Admins only: changes a role (not one's own; never the last admin who can sign in: `409`). Users of the configuration file: `409`. |
| `DELETE user?name=` | | Admins only: removes someone (not oneself; never the last admin): their sessions and links end. |
| `POST user_link?name=` | | Admins only: a new one-time link for someone: `kind` `invite` (no password yet, a week) or `reset` (a day; the current password works until it is used). It replaces the person's earlier link. |
| `POST password_link` | `{token}`, then `{token, password, remember}` | No sign-in needed. With the token alone: `{user, kind}` of a valid link. With a password: sets it, spends the link, ends the person's other sessions and signs in (`200` with the cookie). `401` for a used, replaced or expired link (counted like a failed sign-in), `400` for a password under 8 characters. A new password, however it is set, voids the person's other links. |
| `GET update` | | Admins only: `{current, latest, available, release_url, checked_at, error, request, last_update, git, repository}`: the running version, the latest release on GitHub (looked for once a day), a pending request (`{version, requested_by, requested_at}`, plus `started_at` once a cron run is installing it) and the last update's outcome (`{from, to, at, requested_by}`, `error` when it failed, `rollback` after `jobs.py update --rollback`). |
| `POST update` | `{version}` | Admins only: asks for the latest release; the next `jobs.py cron` run installs it (`409` for another version, a git checkout, or while an update is being installed). `DELETE update` withdraws the request (`409` once its installation has started). |
| `POST password` | `{current, password}` | A signed-in user changes their own password: their other sessions end, this one goes on (a new cookie). `403` for a wrong current password, `409` for a user of the configuration file. |
