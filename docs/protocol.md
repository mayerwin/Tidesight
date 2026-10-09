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
- **Query string:** optional and stored as received. `site=ID` supplies the site id when the envelope has none; the tracker (0.3+) always sends it, so a request refused before its body is read is still counted for its site.
- **Limits:** the body may not exceed `server.max_payload_kb` on the wire (default 64 KB, announced to the tracker as `max_kb` in `/config.js`) or `server.max_decompressed_kb` after decompression (default 256 KB). Numbers too large for a double (`1e999`) make the body unreadable (`400`, `bad_json: number out of range`), as `NaN` and `Infinity` do. At most `server.max_events_per_request` events (default 200) are stored as hits; a longer batch answers `400`, its first events are stored and the raw body keeps the whole batch (`error` `too_many_events:<count>`). Per-client, flagged-traffic and per-site allowances apply (`docs/configuration.md`, "Limits and abuse protection").

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
| `drop` | int | | Batches the client had to discard (retry storage full, too many tries) and events it did not send (over its per-minute cap, or too large to ever be accepted) since the previous batch. |
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
| `st` | Status flagged by the page (e.g. `404`) with `<meta name="ts:status" content="404">`. |
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
| `ua` | `navigator.userAgent` |
| `uad` | `navigator.userAgentData` with high-entropy values: `{brands, mobile, platform, platformVersion, model, architecture, bitness, fullVersionList, formFactors, wow64, uaFullVersion}` (cached per tab in sessionStorage, `ts_uad`) |
| `dnt`, `gpc` | `navigator.doNotTrack`, `navigator.globalPrivacyControl` |

`navigator.platform`, `vendor` and `pdfViewerEnabled` travel in the fingerprint event (`fp.x`), automation signals in `fp.bot`, and ad-blocker detection in the `adblock` event.

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

#### `status`

`st`: a status the page reports after its pageview was sent (`tidesight('status', 404)`), for pages that learn it late, such as an SPA route whose content was not found. Enrichment applies it to the pageview with the same `pid` (the last one wins).

#### `fp` (fingerprint)

Sent when `features.fingerprint` or `features.bot_signals` is on. Computed in idle time after every full page load (not on SPA route changes), one component per idle period, and sent once per session and again whenever the result differs from the one last sent; the session and result last sent are remembered per tab in sessionStorage (`ts_fp`). Each component has its own switch; with `fingerprint` off and `bot_signals` on, the event carries only `bot`, `fv` and `ms`. Hashes are FNV-1a 32-bit in base 36. Fingerprints are evidence for identity stitching, never an identity on their own.

| Field | Meaning |
|---|---|
| `h` | Combined hash of `c`, `gl`, `au`, `fo` and `x` (two 32-bit hashes, concatenated). |
| `c` | Canvas hash (`canvas_fingerprint`): the data URL of a canvas with text and an emoji in two fonts, a gradient and overlapping arcs. |
| `gl` | WebGL (`webgl_fingerprint`): `{v: vendor, r: renderer, h: hash}`. `v` and `r` are the unmasked values (`WEBGL_debug_renderer_info`) when the browser exposes them; `h` covers the `getParameter` limits and versions, shader precisions, supported extensions, context attributes and maximum anisotropy. |
| `au` | Audio (`audio_fingerprint`): the sum of a window of samples of an oscillator rendered offline through a compressor (a float, kept at full precision). Omitted when rendering takes over 1 s. |
| `fo` | Installed fonts (`font_fingerprint`, *off by default*): `{h: hash of the fonts found, n: how many}`, out of about 70 common fonts, detected by text width on a canvas against the generic fallbacks (no DOM changes). |
| `x` | Other raw components: `pf` (`navigator.platform`), `vnd` (`navigator.vendor`), `pdf` (`pdfViewerEnabled`, 1/0), `pl` (hash of plugin names), `mt` (hash of MIME types), `mc` (hash of Math function results), `tz` (time zone), `lg` (languages, comma-separated), `sc` (`[screen width, height, colour depth]`), `hc` (`hardwareConcurrency`), `dm` (`deviceMemory`), `tp` (`maxTouchPoints`), `ar` (CPU architecture byte from a NaN bit pattern), `ps` (`productSub`), `os` (`oscpu`), `cg` (colour gamut: `rec2020`, `p3`, `srgb`), `hdr` (high dynamic range, 1/0), `cn` (1 when the canvas renders differently each time, e.g. randomised by the browser or an extension: `c` is then unstable). |
| `bot` | Automation signals (`bot_signals`; sent here rather than in `ctx`): `wd` (`navigator.webdriver`, 1/0), `pl` (plugin count), `lg` (language count), `oz` (outer window size is zero, 1/0), `au` (automation globals and attributes found, e.g. `__playwright__binding__`, `__pwInitScripts`, `callPhantom`, `__nightmare`, `domAutomation`, `Cypress`, `__webdriver_evaluate`, `_selenium`, `$cdc_…` keys, `@webdriver` for a `webdriver` attribute on `<html>`; omitted when none), `hl` (`HeadlessChrome` in the user agent, 1/0), `co` (`window.chrome` present, 1/0; Chrome user agents only). |
| `fv` | Algorithm version, currently `1`. It changes whenever a component's computation changes: compare hashes only within one version. |
| `ms` | Time spent computing (ms): the idle-time steps plus the audio rendering. |

#### Behaviour events (plugins)

Sent by the plugins (`ts/<name>.js`), each event only when its feature is on and sampled in for the session. A plugin starts after the page's first pageview (at once when `features.pageviews` is off), so its events carry that page's `pid`; nothing is sent on excluded paths. Elements inside `.ts-no-track` are ignored by click, form, copy/paste and selection tracking. No value a visitor types, copies, pastes or selects is ever sent.

| Type | Fields |
|---|---|
| `click` | `sel` (CSS selector), `tag`, `txt` (≤ `clicks.text_max` chars) of the clicked element: the nearest link, button, field, label, summary, `[role=button\|link\|checkbox\|menuitem\|tab]` or `[onclick]` element, else the target itself (then only its own text). `txt` is never a typed value: fields give none, submit/button inputs their label. `btn` (mouse button), `mod` (modifier keys: alt 1, ctrl 2, meta 4, shift 8; omitted when none), `vw`, `vh` (viewport). `x`, `y` (page px) and `vx`, `vy` (viewport px), or `kb: 1` for a click without a pointer position (keyboard or script). Links: `href`, `lk` (`out` another host, `dl` a `download_extensions` extension or a `download` attribute, `mail`, `tel`), `lk` only when its feature (`outbound` for out/mail/tel, `downloads` for dl) is on. With `heatmap_clicks`: `ex`, `ey` (position inside the element, 0–1, 3 decimals), `ew`, `eh` (element size), `pw`, `ph` (page size). Middle clicks on links are sent too (`btn: 1`). With `clicks` off, link clicks are still sent when their `lk` feature is on. A click a `<label>` forwards to its control is not counted twice. |
| `rage` | `sel` (element of the first click), `n` (clicks), `ms` (first to last click), `x`, `y` (first click, page px). A burst starts with `rage_clicks.count` clicks within `window_ms` and `radius_px` of each other and lasts while further clicks follow within `window_ms` and `radius_px`; one event when it ends (or the page is hidden or left). The clicks are also sent as `click` events. |
| `dead` | `sel`, `tag`, `x`, `y` (page px): a click on a clickable-looking element (link, button, `[role=button\|link]`, submit or button input, summary, label, `[onclick]`, or `cursor: pointer`) followed within `dead_clicks.wait_ms` by no DOM change, URL change, focus change, scroll, input, change, submit, window blur or page exit. Form fields, links the browser follows (unless a script prevents it) and links opened in a new tab are never dead. One check at a time. |
| `errclick` | `sel`, `msg`: a JS error or unhandled rejection within 1 s after a click (once per click). |
| `scroll` | `d` (milestone %), `max` (maximum depth so far, %), `ph` (page height px), `vh` (viewport height px). Depth = (scrollY + viewport height) / page height. One event per newly crossed milestone of `scroll.marks` (default 25, 50, 75, 90, 100); when several are crossed at once (a short page at load, a jump to the end), only the highest is sent and the lower ones are implied. Per page: SPA route changes start again. The maximum is also added to `engagement` events as `sd`. |
| `form` | `a` (action), `f` (form: its `id`, `name` or action path, else a selector; fields outside a `<form>` belong to the closest `[data-ts-form]` container, named by that attribute, else to `_page`), `fld` (field `name`, else `id`, else selector), `ft` (field type: `text`, `email`, `password`, `checkbox`, `select-one`, `textarea`…). Actions: `start` (first field focused or changed in the form, with `fld`), `blur` (leaving a field: `fld`, `ft`, `ms` time in the field, `filled` 1/0), `submit` (`n` fields touched, `ms` since `start`), `invalid` (HTML5 validation failed: `fld`, `ft`, `why` = the first failing `ValidityState` flag, e.g. `valueMissing`, `typeMismatch`; at most 50 per page), `reset`, `abandon` (page left or SPA route after a `start` without a `submit`: `n`, `last` field touched). Hidden, submit, button, reset and image inputs are skipped. Values are never sent. |
| `vital` | `n` (`LCP`, `CLS`, `INP`, `FCP`, `TTFB`), `val` (ms, rounded; CLS unitless, 4 decimals), `r` (`good`, `ni`, `poor` by the web-vitals thresholds: LCP 2500/4000, CLS 0.1/0.25, INP 200/500, FCP 1800/3000, TTFB 800/1800), `el` (selector: the LCP element, the largest shift's source, the INP target), `nt` (navigation type of the page load: `navigate`, `reload`, `back_forward`, `prerender`), `upd` (1 on a re-sent, changed value: the last value per page and metric wins). Definitions follow the web-vitals library (CLS: largest session window, shifts < 1 s apart, ≤ 5 s, without recent input; INP: worst interaction, one in 50 ignored; TTFB: responseStart − activationStart). For the initial page load only, never SPA soft navigations: `pid` and `url` are that page's even when sent after route changes. TTFB and FCP are sent when known, LCP at the first key press or click (or when hidden), CLS and INP when the page is first hidden or left; a later hide re-sends a value that changed. |
| `perf` | Navigation Timing of the page load, once after `load`, ms rounded: durations `dns`, `tcp`, `tls`, `dl` (response download), `rt` (redirects); times since navigation start `ttfb` (responseStart), `req` (requestStart), `dom` (domInteractive), `dcl` (domContentLoadedEventEnd), `load` (loadEventEnd); `rc` (redirect count), `tsz` (transfer size, bytes), `ebs`, `dbs` (encoded/decoded body size), `proto` (next-hop protocol), `nt` (navigation type), `act` (prerender activation time, when prerendered), `srt` (Server-Timing entries `[[name, duration, description]]`, when present). |
| `longtask` | `n` (count), `tot` (total ms), `max` (longest, ms), `tbt` (blocking time: the part of each task over 50 ms) of the long tasks since the previous report for the page, sent when the page is hidden, left or changed (SPA); sum them per page *(off by default)*. |
| `error` | `k` (`error`, `rejection`, `resource`), `msg`, `src`, `line`, `col`, `stack` (≤ 4 KB) for JS errors and rejections; `tag` and `res` (URL) for failed resources (`resource_errors`). `ts` is when it happened, including errors raised before the plugin loaded. Identical errors (same `k`, `msg`, `src` or `res`, `line`) are sent once per page; when the page is hidden, left or changed, an update with the identifying fields, `n` (total occurrences on the page) and `upd: 1` (no stack) follows for any error seen again. At most `errors.max_per_page` distinct errors per page. |
| `resize` | `vw`, `vh`, `or` (screen orientation type), 500 ms after the last resize or orientation change. |
| `vis` | `s`: `hidden`, `visible` (tab), `blur`, `focus` (window). At most 50 per page. |
| `copy`, `cut`, `paste` | `sel` (target element), `len` (characters; never the text) *(off by default)*. |
| `select` | `sel` (element containing the selection), `len`: a text selection of at least 3 characters, 1 s after it last changed *(off by default)*. |
| `adblock` | `ab`: 1 when a bait element with ad-like class names was hidden or removed, else 0. Once per session (remembered per tab in sessionStorage, `ts_ab`); sent as this event rather than in `ctx` *(off by default)*. |
| `search` | `q`, `n` (result count), `cat`: site searches reported by the page (`tidesight.search()`). Searches in the URL are extracted at enrichment. |

### Response

| Status | Meaning | Tracker action |
|---|---|---|
| `204` | Stored. | Done. |
| `400` | Malformed; stored with an error flag. | Drop (retrying cannot help). |
| `413` | Too large; metadata stored. | Drop. |
| `429` | An allowance is used up (per client, flagged traffic, or the site's daily quota); counted. Malformed and oversize requests keep their `400`/`413`, except from a client already over its allowance for the minute: its requests are refused before their body is read. | Retry later (`Retry-After`). |
| `503` | Storage unavailable, or too little disk space (`Retry-After: 300`). | Retry later. |

Responses carry `Cache-Control: no-store`, CORS headers, and `Accept-CH` (when `server.accept_ch`). When a site sets `server_cookie_domain`, a successful response also sets `ts_svid` (see below).

### CORS

`/collect` and `/chunk` answer with `Access-Control-Allow-Origin: <request Origin>`, `Access-Control-Allow-Credentials: true` and `Vary: Origin`. With `server.cors = "reflect"` (default) any origin is reflected: the endpoints are write-only and the hit is flagged anyway when its origin is not allowed. With `server.cors = "allowed"`, only the site's allowed origins are reflected. Preflight (`OPTIONS`) answers `204` with `Access-Control-Allow-Methods: POST, GET, OPTIONS`, `Access-Control-Allow-Headers: Content-Type, Content-Encoding` and `Access-Control-Max-Age: 86400`.

## `POST /chunk`

Heavy data goes to `replay.db`. The metadata travels in the query string, so the collector stores the body **without parsing it**; every query parameter is kept in the chunk's metadata.

`POST /chunk?site=ID&k=replay&vid=…&sid=…&pid=…&seq=0&ts=…&url=…&rec=…`

| Parameter | Meaning |
|---|---|
| `site`, `vid`, `sid`, `pid` | As in `/collect`; `pid` is the page instance shown when the chunk was sent. |
| `k` | Kind: `replay` (rrweb events), `mouse` (mouse-movement points), `snapshot` (DOM snapshot). |
| `seq` | Chunk number, from 0: per page instance (`pid`) for `mouse`; per recording (`rec`) for `replay`, continuing across SPA route changes; always 0 for `snapshot`. Chunks can arrive out of order: sort by `seq`. |
| `ts` | Client time of the first item in the chunk (ms). |
| `url` | Page URL (optional). |
| `rec` | `replay` only: the recording id, one per page load. SPA route changes continue the same recording. |

The body is JSON, up to `server.max_chunk_kb` (default 1 MB). While the page is open it is gzip-compressed (sniffed); when the page is hidden or left, whatever is pending is sent at once, uncompressed, by a `keepalive` fetch (`sendBeacon` in browsers without it) within the browser's budget for requests that outlive the page (about 64 KB in flight, shared with the event batches), else by an ordinary `fetch`. Payload formats:

- `replay`: a JSON array of rrweb events (`@rrweb/record` 2.x). A recording starts with a Meta event (type 4) and a FullSnapshot (type 2) and takes a new full snapshot every 5 minutes; an SPA route change adds a custom event (type 5) `{"tag": "pageview", "payload": {"pid", "url"}}`. Inputs are masked (all of them unless `replay.mask_all_inputs` is false, passwords always), and so is the text of rich-text editors (`contenteditable`, `role="textbox"`) unless `mask_all_inputs` is false; text inside `replay.mask_text_class` (`ts-mask`) is masked, elements with `replay.block_class` (`ts-block`) and hidden inputs are left out. URL attributes (`href`, `src`, `srcset`, `action`, `formaction`, `poster`, `data`…) are redacted like every URL the tracker sends. Chunks are sent every 10 s, at 200 events or 500 KB, and about 1 s after a full snapshot; when the page is hidden or left, in pieces of at most 48 KB. A recording stops after `replay.max_minutes`, or when an SPA route enters an excluded path (a later tracked page starts a new recording).
- `mouse`: `{"pw", "ph", "vw", "vh", "pts": [[x, y, dt], …]}`: page coordinates, at most one point per `mouse_moves.interval_ms` while a mouse or pen moves (the resting position included), `dt` in ms since `ts`; page and viewport size when sent. A chunk every 10 s or 500 points, at most `mouse_moves.max_points` points per page.
- `snapshot`: a JSON array of two rrweb events, Meta (`href`, `width`, `height`) and FullSnapshot: the page as rendered about 1.5 s after load or an SPA route change, once per page (path, plus the hash of `#/` routes) per session, for heatmap backgrounds. Same masking as `replay`. Taken with a short rrweb recording, or from the running replay recording (which then also holds that full snapshot).

Responses and CORS are as for `/collect`. A chunk of a kind the site does not record (`replay` needs the `replay` feature, `mouse` needs `mouse_moves`, `snapshot` needs `dom_snapshots`) is stored flagged (`site_ok = 0`) within the flagged-traffic allowance; `/chunk` has its own per-client allowances (`chunk_rate_limit_per_minute`, `chunk_kb_per_minute`).

## `GET /p.gif`

Returns a 1×1 transparent GIF with `Cache-Control: no-store`, and records one hit.

| Parameter | Meaning |
|---|---|
| `site` | Site id (required for attribution). |
| `t` | Event type (default `pageview`; e.g. `email_open`). |
| `u` | Page URL, stored as sent (defaults to the `Referer` header, which is stored with its URL secrets redacted). |
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
(typeof window.tidesight=="function"?window.tidesight:window.tidesight=function(){(tidesight.q=tidesight.q||[]).push(arguments)})("_config",{…});
```

(An element with `id="tidesight"` in the page's content cannot make it throw.)

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
| `redact_params` | URL parameter patterns whose values the tracker replaces by `redacted` in every URL it sends. |
| `redact_paths` | Path patterns whose `*` parts the tracker replaces by `redacted` in every URL it sends (since tracker 0.3). |
| `dnt`, `gpc` | When true, the tracker stays silent for visitors sending that signal. |
| `max_kb` | The collector's `max_payload_kb`: the tracker keeps each request body under it. |

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
| `ts_uad` | sessionStorage | High-entropy User-Agent Client Hints, fetched once per tab. |
| `ts_fp` | sessionStorage | Session and fingerprint last sent (the `fp` event is sent once per session). |
| `ts_ab` | sessionStorage | Ad-blocker result already sent this session. |
| `ts_snap` | sessionStorage | Pages whose DOM snapshot was already taken this session. |

## Changelog

- **v1** (2026-10): initial version. Clarifications made while the plugins were built, all backward compatible:
  bot signals moved from `ctx.bot` to `fp.bot`, ad-blocker detection from `ctx.ab` to the `adblock` event;
  `perf` transfer size is `tsz` (`ts` is every event's client time); `vital` and `error` updates carry `upd: 1`;
  replay chunks carry `rec`. Collectors fill the `name` column only from a string `n` (several event types use
  `n` as a count). `tidesight('status')` sends a `status` event rather than setting `st` on a later pageview.
  Limits (2026-10): `/config.js` announces `max_kb`; a batch over `max_events_per_request` answers 400 (its first
  events stored, the raw body kept); allowances per client (IPv6 by /64), for flagged traffic and per site and day
  answer 429; low disk space answers 503.
