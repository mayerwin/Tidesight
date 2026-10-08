# Wire protocol

The contract between trackers and collectors. Every connector (Python, PHP, Node, Worker) implements it identically and is checked by the shared conformance suite (`server/python/tests/test_conformance.py`).

**Current version: 1.** The version travels in the `v` field of every payload.

## Versioning rules

- Adding an optional field or a new event type is **not** a breaking change and does not bump `v`.
- Collectors store unknown fields and unknown event types as received. Enrichment ignores what it does not understand.
- A breaking change bumps `v`. Collectors keep accepting every older version forever, because old trackers live on in browser caches. Enrichment handles each version explicitly.
- Every change to this file is listed in the changelog at the end.

## Endpoints

All paths are relative to the collector's base URL (e.g. `https://stats.example.com/`). A deployment may mount the collector under a prefix; the tracker derives the base from its own `src` unless `data-api` overrides it.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/collect` | A batch of events (JSON envelope). |
| `POST` | `/chunk` | One chunk of heavy data (session replay, mouse movement, DOM snapshot). |
| `GET` | `/p.gif` | Pixel hit for no-JS pages and emails. |
| `GET` | `/config.js?site=ID` | Site configuration for the tracker, as JavaScript. |
| `GET` | `/config.json?site=ID` | The same configuration as JSON. |
| `GET` | `/health` | Liveness and storage check. |
| `OPTIONS` | `/collect`, `/chunk` | CORS preflight. |
| `GET` | `/ts.js`, `/ts/<file>` | Tracker files (optional; usually served as static files). |

## `POST /collect`

### Request

- **Body:** a JSON envelope (UTF-8), optionally gzip-compressed.
- **Compression:** detected from the gzip magic bytes (`1f 8b`) or `Content-Encoding: gzip`. Browsers cannot set `Content-Encoding` without a preflight, so the tracker sends gzip bytes with a `text/plain` content type and the collector sniffs them.
- **Content-Type:** `text/plain` (what browsers send; no preflight), `application/json`, or `application/x-www-form-urlencoded` with the envelope JSON in the `d` field.
- **Query string:** optional and stored as received. `site=ID` supplies the site id when the envelope has none.
- **Limits:** the body may not exceed `server.max_payload_kb` on the wire (default 64 KB) or `server.max_decompressed_kb` after decompression (default 4 MB). An envelope may carry at most `server.max_events_per_request` events (default 500).

### Envelope

```json
{
  "v": 1,
  "site": "example",
  "vid": "k3x9q2m7c1v8b4n6z0a5s2d7",
  "vt": 1728380000000,
  "sid": "p8w2e5r1t7y3u9i4",
  "uid": "user-42",
  "sent": 1728380012345,
  "lib": "ts.js/0.1.0",
  "try": 0,
  "drop": 0,
  "events": [ { "t": "pageview", "id": "…", "ts": 1728380012001, "pid": "…", "url": "https://example.com/" } ]
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `v` | int | yes | Protocol version. |
| `site` | string | yes¹ | Site id, as configured (`[sites.<id>]`). |
| `vid` | string | | Persistent visitor id (8–64 chars of `[A-Za-z0-9_-]`). |
| `vt` | int | | When the visitor id was created (client ms). |
| `sid` | string | | Current session id. |
| `uid` | string | | Known user id set by `tidesight.identify()`. |
| `sent` | int | | Client clock when the request was sent (ms). Compared with the server time, it measures clock skew. |
| `lib` | string | | Tracker name and version. |
| `try` | int | | Delivery attempt: 0 for the first send, > 0 for a retry from storage. |
| `drop` | int | | Batches the client had to discard (storage full) since its last successful send. |
| `events` | array | yes | The events, oldest first. |

¹ Or `?site=` in the query string. A request without a site id is stored with `site_status = "missing_site"`.

### Common event fields

| Field | Type | Meaning |
|---|---|---|
| `t` | string | **Required.** Event type (see below). |
| `id` | string | Unique event id. Duplicates (from retries) are removed at enrichment. |
| `ts` | int | Client time when the event happened (ms). |
| `pid` | string | Page-instance id: one per pageview, including SPA route changes. Links an event to its pageview. |
| `url` | string | Page URL at the time of the event (full, including query and hash). |
| `sid`, `vid` | string | Override the envelope's values for this event (a session rolled over inside a batch). |
| `ns` | 1 | This event started a new session. |
| `sn` | int | Session number for this visitor (1 = first session). |

### Event types

#### `pageview`

| Field | Meaning |
|---|---|
| `url` | Full URL, including hash. |
| `title` | `document.title`. |
| `ref` | Referrer: `document.referrer` for a page load, the previous URL for an SPA route change. |
| `nav` | Navigation type: `navigate`, `reload`, `back_forward`, `prerender`, `restore` (bfcache), `spa` (History API), `hash` (hash route). |
| `canon` | Canonical URL (`<link rel="canonical">`), when present and different from `url`. |
| `lang` | Document language (`<html lang>`). |
| `pn` | Pageview number within the session (1 = landing page). |
| `new` | 1 when the visitor id was created on this page load. |
| `idsrc` | Where the visitor id came from: `cookie`, `ls` (localStorage), `server` (server cookie), `new`. |
| `idc` | Identity conflict, when the cookie and localStorage held different ids: `{"cookie": "…", "ls": "…"}`. |
| `st` | Status flagged by the page (e.g. `404`), from `<meta name="ts:status" content="404">` or `tidesight('status', 404)`. |
| `props` | Custom properties passed to a manual pageview. |
| `ctx` | Client context, see below. |

#### Client context (`ctx`, on pageviews)

Present when `features.context` is on. Every key is optional.

| Key | Source |
|---|---|
| `sw`, `sh` | `screen.width`, `screen.height` |
| `saw`, `sah` | `screen.availWidth`, `screen.availHeight` |
| `vw`, `vh` | Viewport (`innerWidth`, `innerHeight`) |
| `dpr` | `devicePixelRatio` |
| `cd` | `screen.colorDepth` |
| `or` | `screen.orientation.type` |
| `lang`, `langs` | `navigator.language`, `navigator.languages` |
| `tz`, `tzo` | `Intl…timeZone`, `Date#getTimezoneOffset()` |
| `cs` | `prefers-color-scheme`: `dark` / `light` |
| `rm` | `prefers-reduced-motion: reduce` (1/0) |
| `ct` | `prefers-contrast`: `more` / `less` / `custom` / `no-preference` |
| `fc` | `forced-colors: active` (1/0) |
| `tp`, `touch` | `navigator.maxTouchPoints`, touch events supported (1/0) |
| `hc`, `dm` | `hardwareConcurrency`, `deviceMemory` |
| `net` | `navigator.connection`: `{type, eff, dl, rtt, sd}` |
| `ck`, `ls` | Cookies enabled, localStorage usable (1/0) |
| `pwa` | Display mode: `standalone`, `fullscreen`, `minimal-ui`, `browser` |
| `ifr` | Page runs in an iframe (1/0) |
| `ua`, `pf`, `vnd` | `navigator.userAgent`, `navigator.platform`, `navigator.vendor` |
| `uad` | `navigator.userAgentData` with high-entropy values: `{brands, mobile, platform, platformVersion, model, architecture, bitness, fullVersionList, formFactors, wow64}` |
| `pdf` | `navigator.pdfViewerEnabled` |
| `dnt`, `gpc` | `navigator.doNotTrack`, `navigator.globalPrivacyControl` |
| `bot` | Automation signals (when `features.bot_signals`): `{wd, pl, lg, oz, au, hl, co}` = webdriver, plugin count, language count, outer size is zero, automation globals found, headless UA, `window.chrome` present |
| `ab` | Ad blocker detected (1/0), when `features.adblock` |

#### `event` (custom)

| Field | Meaning |
|---|---|
| `n` | Event name (≤ 200 chars). GA4-style ecommerce names (`purchase`, `add_to_cart`, `begin_checkout`, `view_item`, `refund`) carry GA4's parameters in `p`. |
| `p` | Properties: a flat object of strings, numbers and booleans (ecommerce `items` may be an array). |
| `val` | Optional numeric value. |

#### `engagement`

Sent when the page is hidden or left, on SPA route changes (for the previous page) and as a heartbeat while engaged.

| Field | Meaning |
|---|---|
| `ms` | Engaged milliseconds since the previous engagement event for this `pid`. |
| `tot` | Total engaged milliseconds for this `pid`. |
| `why` | `hb` (heartbeat), `hidden`, `leave` (pagehide), `route` (SPA navigation). |
| `sd` | Maximum scroll depth reached (%), when the scroll plugin runs. |

#### `identify`

`uid` (string) and `traits` (flat object). The user id is also sent in the envelope (`uid`) from then on.

#### `fp` (fingerprint)

Once per session (and again when the combined hash changes), when `features.fingerprint` is on.

| Field | Meaning |
|---|---|
| `h` | Combined hash of all components. |
| `c` | Canvas hash. |
| `gl` | WebGL `{v: vendor, r: renderer, h: parameter hash}`. |
| `au` | Audio hash. |
| `fo` | Installed-font probe: `{h: hash, n: count}` *(off by default)*. |
| `x` | Other raw components (math constants, plugin/mime list hash, …). |
| `fv` | Fingerprint algorithm version. |
| `ms` | Time spent computing (ms). |

#### Behaviour events (plugins)

| Type | Fields |
|---|---|
| `click` | `x`, `y` (page px), `vx`, `vy` (viewport px), `pw`, `ph` (page size), `vw`, `vh`, `sel` (CSS selector), `tag`, `txt` (≤ 100 chars), `href`, `lk` (`out`, `dl`, `mail`, `tel`), `ex`, `ey` (position inside the element, 0–1), `ew`, `eh` (element size), `btn`, `mod` (modifier keys). |
| `rage` | `sel`, `n` (clicks), `ms` (time span), `x`, `y`. |
| `dead` | `sel`, `tag`, `x`, `y`: a click on a clickable-looking element with no visible effect. |
| `errclick` | `sel`, `msg`: a click followed by a JS error. |
| `scroll` | `d` (milestone %: 25, 50, 75, 90, 100), `max`, `ph`, `vh`. |
| `form` | `a` (`start`, `focus`, `blur`, `submit`, `abandon`, `invalid`, `reset`), `f` (form id, name or selector), `fld` (field name or id), `ft` (field type), `ms` (time in field), `filled` (1/0), `n` (fields touched), `last` (last field touched), `why` (validity reason). Field values are never sent. |
| `vital` | `n` (`LCP`, `CLS`, `INP`, `FCP`, `TTFB`), `val`, `r` (`good`, `ni`, `poor`), `el` (selector of the element responsible), `nt` (navigation type). |
| `perf` | Navigation Timing: `dns`, `tcp`, `tls`, `ttfb`, `dl`, `dom`, `dcl`, `load`, `rt` (redirect time), `rc` (redirect count), `ts` (transfer size), `ebs`, `dbs` (encoded/decoded body size), `proto`, `nt`. |
| `longtask` | `n` (count), `tot` (total ms), `max` (ms) for the page *(off by default)*. |
| `error` | `k` (`error`, `rejection`, `resource`), `msg`, `src`, `line`, `col`, `stack` (≤ 4 KB), `tag` and `res` (for resource errors), `n` (occurrences on this page). |
| `resize` | `vw`, `vh`, `or`. |
| `vis` | `s`: `hidden`, `visible`, `blur`, `focus`. |
| `copy`, `cut`, `paste` | `sel`, `len` *(off by default)*. |
| `select` | `sel`, `len` *(off by default)*. |
| `search` | `q`, `n` (result count), `cat`: site searches reported by the page (`tidesight.search()`). Searches in the URL are extracted at enrichment. |

### Response

| Status | Meaning | Tracker action |
|---|---|---|
| `204` | Stored. | Done. |
| `400` | Malformed; stored with an error flag. | Drop (retrying cannot help). |
| `413` | Too large; metadata stored. | Drop. |
| `429` | Rate limited; counted. | Retry later. |
| `503` | Storage unavailable. | Retry later. |

Responses carry `Cache-Control: no-store`, CORS headers, and `Accept-CH` (when `server.accept_ch`). When a site sets `server_cookie_domain`, a successful response also sets `ts_svid` (see below).

### CORS

`/collect` and `/chunk` answer with `Access-Control-Allow-Origin: <request Origin>`, `Access-Control-Allow-Credentials: true` and `Vary: Origin`. With `server.cors = "reflect"` (default) any origin is reflected: the endpoints are write-only and the hit is flagged anyway when its origin is not allowed. With `server.cors = "allowed"`, only the site's allowed origins are reflected. Preflight (`OPTIONS`) answers `204` with `Access-Control-Allow-Methods: POST, GET, OPTIONS`, `Access-Control-Allow-Headers: Content-Type, Content-Encoding` and `Access-Control-Max-Age: 86400`.

## `POST /chunk`

Heavy data goes to `replay.db`. The metadata travels in the query string, so the collector stores the body **without parsing it**.

`POST /chunk?site=ID&k=replay&vid=…&sid=…&pid=…&seq=0&ts=…`

| Parameter | Meaning |
|---|---|
| `site`, `vid`, `sid`, `pid` | As in `/collect`. |
| `k` | Kind: `replay` (rrweb events), `mouse` (mouse-movement points), `snapshot` (DOM snapshot). |
| `seq` | Chunk number within the page instance (0, 1, 2, …). |
| `ts` | Client time of the first item in the chunk (ms). |
| `url` | Page URL (optional). |

The body is a JSON array, optionally gzip-compressed (sniffed), up to `server.max_chunk_kb` (default 1 MB). Payload formats:

- `replay`: an array of rrweb events.
- `mouse`: `{"pw", "ph", "vw", "vh", "pts": [[x, y, dt], …]}` with page coordinates and milliseconds since `ts`.
- `snapshot`: an rrweb full snapshot.

Responses and CORS are as for `/collect`.

## `GET /p.gif`

Returns a 1×1 transparent GIF with `Cache-Control: no-store`, and records one hit.

| Parameter | Meaning |
|---|---|
| `site` | Site id (required for attribution). |
| `t` | Event type (default `pageview`; e.g. `email_open`). |
| `u` | Page URL (defaults to the `Referer` header). |
| `r` | Referrer. |
| `n` | Event name. |
| `vid`, `sid`, `uid` | Identity, when known (e.g. personalised email links). |
| `title` | Page title. |

Any other parameter is stored as well. No-JS usage:

```html
<noscript><img src="https://stats.example.com/p.gif?site=example" alt="" width="1" height="1"></noscript>
```

## `GET /config.js?site=ID`

Returns JavaScript that hands the site's public configuration to the tracker through its command queue, so load order does not matter:

```js
(window.tidesight=window.tidesight||function(){(tidesight.q=tidesight.q||[]).push(arguments)})("_config",{…});
```

`/config.json?site=ID` returns the same object as JSON. The object:

| Key | Meaning |
|---|---|
| `site` | Site id. |
| `known` | `false` when the site id is not configured (the tracker still runs with defaults; hits are flagged). |
| `cv` | Config version (hash), for cache busting. |
| `features` | `{name: true/false}` for every feature in `docs/configuration.md`. |
| `opts` | Per-feature options, e.g. `{"replay": {"sample_rate": 0.1, "mask_all_inputs": true}}`. |
| `session_timeout_min`, `split_session_at_midnight`, `timezone` | Session rules. |
| `cookie_domain`, `cookie_days` | Where and how long identifiers are kept. |
| `hash_mode`, `spa_replace` | Which route changes are pageviews. |
| `heartbeat_sec`, `flush_sec` | Engagement heartbeat and send interval. |
| `search_params`, `exclude_paths`, `download_extensions` | Behaviour settings. |
| `dnt`, `gpc` | When true, the tracker stays silent for visitors sending that signal. |

Cached with `Cache-Control: public, max-age=300` and an `ETag`. The tracker also caches it in localStorage.

## `GET /health`

`200` with `{"status": "ok", "version": "…", "storage": "sqlite", "time": <ms>}` when storage is writable, `503` with `"status": "error"` otherwise. It never reveals paths.

## Client storage

Visitor-visible names used by the official tracker. Other trackers should use the same names so identities carry over.

| Name | Where | Value |
|---|---|---|
| `ts_vid` | cookie + localStorage | `<visitor id>.<created ms>` |
| `ts_ses` | cookie + localStorage | `<session id>.<start ms>.<last activity ms>.<session number>.<pageviews in session>` |
| `ts_svid` | cookie set by the collector on `server_cookie_domain` | `<visitor id>.<created ms>` |
| `ts_uid` | localStorage | Known user id. |
| `ts_q` | localStorage | Undelivered batches awaiting retry. |
| `ts_cfg_<site>` | localStorage | Cached site configuration. |
| `ts_ignore` | localStorage | `1`: this browser is excluded from tracking (`tidesight('ignore', true)`). |

## Changelog

- **v1** (2026-10): initial version.
