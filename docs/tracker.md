# Using the tracker

How to add Tidesight to a site and send it more than pageviews. What the tracker sends is specified in `docs/protocol.md`; what each feature records, and its default, in `docs/datapoints.md`; how to switch features per site, in `docs/configuration.md`.

## Install

Add one tag to the `<head>` of every page, with your collector's host and the property id (the dashboard shows the whole tag in the property's "Tracking code" tab; for a site of the configuration file, the id is the `[sites.<id>]` table's name):

```html
<script async src="https://stats.example.com/ts.js" data-site="example"></script>
```

The core (`ts.js`, about 8 KB gzip) records pageviews, SPA route changes, custom events, engagement time and the client context. It then fetches the site's configuration (`/config.js`; a copy kept in localStorage is used for up to a day and refreshed in the background after five minutes) and loads the behaviour plugins the site has switched on and the session is sampled into, from `/ts/<plugin>.js`. Nothing is loaded from any other host.

**Nothing to update on the sites.** The tag never changes: `ts.js` and its plugins are served by the Tidesight host, so updating Tidesight updates the tracker of every site. Browsers keep `ts.js` for an hour at most (plugins are fetched with the core's version in their address, so a new core never meets old plugins), and the site's configuration for five minutes. A tracker still cached from an older version keeps working: collectors accept every version of the protocol forever (`docs/protocol.md`). Only a copy of `ts.js` put on the site itself (`data-api`) has to be replaced by hand.

**One file instead.** `ts.all.js` bundles the core with every plugin except session replay: one request instead of several, at the cost of downloading plugins a site may not use. It takes the same attributes.

**Calls made before the script loads** are queued if the page defines the stub first:

```html
<script>window.tidesight=typeof tidesight=="function"?tidesight:function(){(tidesight.q=tidesight.q||[]).push(arguments)};</script>
```

(Written this way, an element with `id="tidesight"` in the page's own content cannot get in the way.)

**Visitors without JavaScript** can be counted with the pixel (no visitor id, sessions are approximated at enrichment):

```html
<noscript><img src="https://stats.example.com/p.gif?site=example" alt="" width="1" height="1"></noscript>
```

### Script attributes

| Attribute | Meaning |
|---|---|
| `data-site` | The site id. Required. |
| `data-api` | The collector's base URL, when `ts.js` is served from somewhere else (for example copied onto the site itself). By default it is the folder `ts.js` was loaded from. |
| `data-auto="false"` | No automatic pageviews and no SPA route tracking: the page calls `tidesight('page')` itself. |
| `data-debug` | Log what the tracker does to the browser console. |

The same settings can be given as `window.tidesightConfig = {site, api, auto, debug}` before the script.

Markup on the page is never trusted for these: an element with `id="tidesightConfig"` or `name="currentScript"` in user content (DOM clobbering) cannot point the tracker elsewhere. The configuration object counts only if it is a plain object, the tracker finds its own `<script>` through the document's accessor, and it only loads scripts (configuration, plugins, the replay recorder) from its own `http(s)` base URL.

### Content Security Policy

On a site with a CSP, allow the collector in `script-src` (the tracker, its configuration and plugins are scripts) and `connect-src` (events are posted with `fetch`, or `sendBeacon` in older browsers), and in `img-src` for the pixel. A `nonce` on the tracker's tag is copied to the scripts it adds. Listing the collector's origin (a host allowlist) rather than relying on the nonce alone is the stronger setup: it also limits where any script on the page can send data.

## Commands

Every command is available as `tidesight('name', …)` and, once the script has loaded, as `tidesight.name(…)`. Commands never throw.

| Command | Effect |
|---|---|
| `tidesight('track', name, props, value)` | A custom event. `props` is an object (up to 50 keys; string values up to 500 characters; nested values such as ecommerce `items` while the whole stays under 24,000 characters); `value` is an optional number, summed per event in the dashboard. Use GA4's ecommerce names (`purchase`, `add_to_cart`, `refund`…) and parameters for revenue. |
| `tidesight('page', props)` | A pageview for the current URL. Needed only with `data-auto="false"`, or to attach properties to a pageview. |
| `tidesight('identify', userId, traits)` | A known user (after login). The id is kept in localStorage and sent with every event from then on; it links the visitor's devices and browsers at enrichment. |
| `tidesight('reset')` | Forget the user id (on logout). |
| `tidesight('search', query, results, category)` | A site search, when the query is not in the URL. Queries in the URL parameters listed in `search_params` (`q`, `s`, `search`, …) are picked up without any call. |
| `tidesight('status', 404)` | Flag the current page's status after its pageview was sent, for example an SPA route whose content was not found. |
| `tidesight('ignore')` | Stop tracking this browser (remembered in localStorage); `tidesight('ignore', false)` resumes. Run it once in your own browser's console to keep your visits out of the statistics. |
| `tidesight('flush')` | Send queued events now. Events are otherwise batched every few seconds and when the page is hidden or left. |

## Marking up the page

| Markup | Effect |
|---|---|
| `<meta name="ts:status" content="404">` | Flags an error page (the dashboard lists such pages). Most sites add it to their 404 template. |
| `class="ts-no-track"` | Clicks, form interactions, copy/paste and text selections inside the element are not recorded. |
| `class="ts-mask"` | Text inside the element is masked in session replays and not captured as click text. Text typed into rich-text editors (`contenteditable`, `role="textbox"`) is masked like inputs without it. |
| `class="ts-block"` | The element is not recorded at all in session replays (a placeholder of the same size is shown). |
| `data-ts-form="name"` | Groups form fields that are not inside a `<form>` (for example a multi-step widget) and names them in form analytics. |

Form analytics never record what visitors type, and session replays never record password fields; other inputs, and the text of rich-text editors, are masked by default (`replay.mask_all_inputs`).

## Single-page applications

Route changes through the History API (`pushState`, `replaceState`, back and forward) are pageviews, and so are hash routes (`#/…`, `#!…`) by default. The site's `hash_mode` and `spa_replace` settings refine this (`docs/configuration.md`). On each route change the tracker reports the engaged time of the page being left, so time on page stays right in an SPA.

## Reliability

The tracker never lets an error reach the page. Events are batched, compressed when the browser supports it, sent with a `keepalive` request when the page is hidden or closed (`sendBeacon` in browsers without it), and kept in localStorage for a later retry when the network is down (bounded, with backoff, a few batches at a time). Every event carries a unique id, so a batch sent twice is counted once.

It also protects the collector from the page: batches stay under the collector's size limit (`max_kb`) and the browsers' allowance for requests that outlive the page (about 64 KB); at most 300 events and 60 uploads (replay, mouse, snapshots) a minute leave a page, so a script calling `track()` in a loop cannot flood anything (the events dropped are counted and reported in the next batch as `drop`); text is cut without splitting a character, URLs at 8,000 characters, and properties are bounded (50 keys, 500-character strings, nested values dropped beyond 24,000 characters, marked `_trunc: 1`).

## Secrets in URLs

Links that log people in or reset passwords often carry a token in the URL. Before anything leaves the browser, the tracker redacts every URL it sends: page, referrer and canonical URLs, links, form actions, resource URLs and the URLs inside error messages and stacks, and the URLs in recorded pages (links, images, forms… in replays and snapshots).

- The values of parameters named in the site's `redact_params` (tokens, passwords, keys, session ids, signatures… see `docs/configuration.md`) become `redacted`: in the query string, in the `#fragment` (`#access_token=…`, or a hash route's `?query`) and in path parameters (`;jsessionid=…`).
- Paths matching `redact_paths`, for links with the secret in the path (`/reset-password/*`): what each `*` matched becomes `redacted`, in the path and in hash routes.
- Credentials before the host (`https://user:pass@…`) become `redacted@`.
- A parameter value that is itself a URL with a secret, as login pages carry (`?next=%2Freset%3Ftoken%3D…`), is replaced whole, two levels of nesting deep.
- WebSocket URLs (`ws:`, `wss:`) are handled like web URLs; other schemes (`data:`, `mailto:`…) are left alone.
- Error messages and stacks, and the text of clicked elements, have their URLs and their `name=value` pairs with secret names redacted the same way. Click text never includes text from masked (`ts-mask`) or editable elements, or form fields.

The patterns are globs matched in linear time, so a hostile link cannot slow the page down. The tracker's requests send only the site's origin as `Referer`, whatever the page's referrer policy; the collector applies the same redaction to any `Referer` it stores (older browsers' beacons, the pixel). Pages that should not be tracked at all go in `exclude_paths`.

## Excluding visits

- `exclude_paths` (per site) keeps the tracker silent on matching pages, e.g. `["/admin/*"]`; it also applies to hash routes (`/#/admin/…`).
- `tidesight('ignore')` excludes one browser.
- With `honour_dnt` or `honour_gpc` set (`[privacy]`), browsers sending Do Not Track or Global Privacy Control are not tracked.
- Visits from `internal_ips` (`[enrich]`) are recorded but kept out of the statistics.
