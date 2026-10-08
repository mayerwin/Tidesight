# Data-point catalogue

Everything Tidesight can record, where it comes from, and whether it is on by default. Defaults are **on** unless marked *(off)*. Heavy features are also *sampled*. Each family has a per-site switch (see `docs/configuration.md`); wire field names are defined in `docs/protocol.md`.

## Server side (every request)

- **Timing and address.** Receive time, raw remote IP (`REMOTE_ADDR`) **plus** the forwarding chain (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`, `CF-Connecting-IP`). The client IP is decided at enrichment, not at ingest.
- **Request.** Method, path, query, TLS/HTTP protocol version (when exposed by the host).
- **All request headers.** Values are stored as received, except:
  - `Cookie`: only the values of Tidesight's own cookies (`ts_*`), plus a flag that other cookies existed and how many;
  - `Authorization` and `Proxy-Authorization`: presence flag only;
  - any header matching a configurable secret list (`*token*`, `*key*`, `*secret*`, `*password*`).

  This covers, among others:
  - `User-Agent`, `Accept-Language`, `Accept`, `Accept-Encoding`;
  - `Referer`, `Origin`;
  - `Sec-CH-UA*` (all Client Hints: brands, mobile, platform, platform version, model, architecture, bitness, full version list);
  - `Sec-Fetch-*`, `DNT`, `Sec-GPC`, `Save-Data`;
  - `Viewport-Width`, `DPR`, `Device-Memory`, `RTT`, `Downlink`, `ECT`;
  - `Via`, plus CDN and geo headers when present (e.g. `CF-IPCountry`).
- **Client Hints opt-in.** The collector sends `Accept-CH` / `Critical-CH`; the tracker also reads high-entropy hints through `navigator.userAgentData`, which works without delegation.

## Client side — page and context (each pageview)

- **Page.** URL (full, including hash), path, query, hash route, title, canonical URL, `document.referrer`, and navigation type (navigate/reload/back_forward/prerender, bfcache restore, SPA route change).
- **Campaign parameters.** `utm_*`, and click IDs `gclid`, `gbraid`, `wbraid`, `fbclid`, `msclkid`, `ttclid`, `twclid`, `li_fat_id`, `dclid`, `yclid` (parsed from the URL at enrichment).
- **Screen and display.** Screen size, available size, viewport, device pixel ratio, colour depth, orientation.
- **Locale.** Language(s), time zone and UTC offset.
- **User preferences.** Colour scheme, reduced motion, contrast, forced colours.
- **Hardware.** Touch support and max touch points, `hardwareConcurrency`, `deviceMemory`.
- **Network.** Connection type, effective type, downlink, RTT, save-data.
- **Browser state.** Cookies enabled, localStorage available, PWA/standalone display mode, iframe embedding, DNT and GPC signals.
- **Bot signals.** `navigator.webdriver` and other headless or automation signals.
- **Ad blocker.** Detected or not *(off)*.

## Identity

- **Persistent visitor id** (random 128-bit id): first-party cookie **and** localStorage, with whichever survives restoring the other; optionally a server-set cookie as a third copy.
- **Session id and counters.** Session id with a 30-minute idle timeout and a new one at midnight in the site's time zone (configurable). Session count and pageview count in session.
- **Known user.** `tidesight.identify(userId, traits)` for sites with logged-in users.
- **Fingerprint components**, each stored as raw values plus a combined hash:
  - UA data, screen, time zone, languages, platform;
  - hardware;
  - installed-font probe *(off)*;
  - canvas hash, WebGL vendor/renderer and parameter hash, audio hash.
- **Cookieless daily hash** (computed at enrichment): a hash of a daily salt + site + IP + UA.

## Behaviour

- **Engagement time** (page visible and focused) and heartbeats.
- **Scroll depth** (25/50/75/90/100 % milestones and maximum reached).
- **Clicks:** coordinates (page, viewport and element-relative), element selector, text and href.
- **Rage, dead and error clicks.**
- **Outbound links, downloads, mailto and tel links.**
- **Form interactions**, never values: form start, field focus and time, submit, abandonment, validation errors.
- **Copy/paste events** *(off)*.
- **Text selection** *(off)*.
- **Window resize and orientation change.**
- **Visibility changes and tab focus.**
- **Site search** queries (configurable URL parameters).
- **404 pages** (the site flags them).
- **Custom events and properties.**
- **Revenue / ecommerce events** (GA4-style `purchase` schema) *(off until needed)*.

## Performance and errors

- **Performance:** Web Vitals (LCP, CLS, INP, FCP, TTFB), Navigation Timing, and long tasks *(off)*.
- **Errors:** JS errors and unhandled rejections; failed resource loads.

## Heavy (each off or sampled by default, per site)

- Session replay (rrweb) *(off)*.
- Mouse-movement capture *(off)*.
- Click heatmaps — cheap enough to be **on** by default, because they reuse the click events.
- Full DOM snapshots for heatmap backgrounds *(off)*.
