# Design principles and decisions

This document records **why** Tidesight is built the way it is. Read it before changing behaviour, storage, the wire protocol or defaults. `AGENTS.md` turns these principles into working rules; `docs/architecture.md` describes how the pieces fit together.

## What Tidesight is for

Tidesight is a self-hosted web analytics system for any number of sites:

- **Collect.** A small JavaScript tracker records what visitors do.
- **Ingest.** A small server-side script receives each hit and appends it to a SQLite database. Python comes first, because it runs on Dreamhost shared hosting; other connectors (PHP, Node, Cloudflare Worker) speak the same wire protocol and write the same schema.
- **Analyse.** Re-runnable enrichment jobs derive sessions, geography, devices, bots and identity from the raw data, and a dashboard presents it.

The goals, in the owner's words:

- **Record every useful data point** that the leading tools record (Google Analytics 4, Matomo, Plausible, Umami, PostHog, Hotjar, Microsoft Clarity, Fathom): request headers, client environment, behaviour, performance, errors, and optionally session replay and heatmaps.
- **Store it simply**, in SQLite, through a small ingest script.
- **Keep the raw data forever**, so all derived data can be recomputed.
- **Make everything configurable** per site and per feature, with a privacy mode for later.
- **Show it in a dashboard**, once collection is solid.

Tidesight is generic. Cap Côtier was the first motivation, but it is just one client: nothing may assume a particular site.

## Principles

### 1. Raw first, derive later

Every hit is stored as received: raw IP, request headers and the full client payload. Everything else is derived by re-runnable jobs: geolocation, user-agent parsing, sessions, visitor identity, bot flags, channels.

*Why:* parsers, geo databases and bot lists improve over time, and analysis questions change. If ingest stored only derived values, history could never be recomputed. Storage is cheap; lost raw data is gone for good.

*Consequences:*
- Ingest never depends on enrichment. It parses only enough to validate and to fill a few indexed columns.
- Raw fields are never discarded at ingest unless a config switch says so.
- Every derived row carries the version of the job that produced it, so a parser change triggers a recompute.
- Derived data lives in its own database file (`derived.db`) that can be deleted and rebuilt at any time.

### 2. Collect what the leading tools collect

GA4, Matomo, Plausible, Umami, PostHog, Hotjar/Clarity and Fathom are the reference for data points, identity techniques and behavioural features. If one of them records something useful for reliable tracking, Tidesight should be able to record it. The full catalogue is in `docs/datapoints.md`.

### 3. Everything is configurable

Every data-point family and every heavy feature (session replay, heatmaps, mouse movement, rage clicks, Web Vitals, error capture…) has a per-site switch, and behaviour plugins have a per-site sampling rate. Defaults are **data-rich**: everything useful is on, and only heavy or invasive features are off or sampled.

### 4. Privacy mode exists, but is off by default

Raw IPs, the persistent visitor ID (cookie + localStorage) and fingerprint signals are **on by default**. GDPR and consent are not a concern for now. A privacy mode (config) plus clean-up jobs can later hash or drop IPs, drop fingerprints, purge replay data, apply retention windows and honour DNT/GPC, mirroring what Plausible, Matomo and GA do.

*Why:* the owner wants maximum insight today, but a site may grow large enough to need privacy controls. Building features so that privacy mode is "a config change plus a script" avoids a rewrite later.

### 5. Ingestion never loses data and never breaks the host site

- The tracker fails silently. No exception may ever reach the host page.
- The collector answers fast (no enrichment, no network calls inline), validates and bounds input, and appends.
- Malformed requests, unknown sites and wrong origins are still **stored and flagged**, never silently dropped. The only deliberate refusals are oversize bodies (their metadata is kept) and per-IP rate limiting (counted).
- The tracker queues events, retries from localStorage, and every event carries a unique id so that duplicates from retries are removed at enrichment, not at ingest.

### 6. Runs on cheap shared hosting

The Python collector uses the **standard library only** (`sqlite3`, `json`, `gzip`, `wsgiref`) and runs as FastCGI, CGI or under Passenger WSGI. There are no long-running daemons: background work runs from cron. Optional heavier libraries (geo readers, UA parsers) are only used by enrichment jobs and degrade gracefully when absent; Tidesight ships pure-Python fallbacks for the important ones.

### 7. Neutral, friendly naming in anything a visitor can see

Script names, cookie names, storage keys and endpoints use the neutral `ts` prefix (`ts.js`, `ts_vid`, `/collect`). Nothing that sounds like surveillance.

## Decision log

Decisions taken by the owner during planning (2026-10-08), with the reasoning behind them.

| Topic | Decision | Reasoning |
|---|---|---|
| Name | **Tidesight**: traffic ebbs and flows like a tide, and "sight" means insight. Used everywhere (repo, package, docs). | |
| Storage | **SQLite.** A Phase 0 hosting test verifies it on Dreamhost (`tools/sqlite_host_test.py`). | Zero administration, single file, fast enough for append-heavy workloads; no database server on shared hosting. |
| Storage fallback | If SQLite fails the hosting test, the collector appends one JSON line per hit to a spool file and a cron job loads it into SQLite. It is a config switch (`storage_mode = "spool"`). | Appending to a file never contends for a lock. Per-process spool files avoid interleaving even on network filesystems. |
| First connector | **Python, standard library only**, via Passenger WSGI (CGI as a fallback). Since 2026-10-09: FastCGI, because Dreamhost no longer offers Passenger on shared plans. | Runs on Dreamhost without installing anything. |
| Raw IP | **Stored by default.** An option hashes or drops it (privacy mode). | Raw first; geolocation and bot detection improve if the IP is kept. |
| Persistent visitor ID | **On by default:** first-party cookie + localStorage, each restoring the other. | Same approach as GA's `_ga`; survives third-party-cookie blocking. |
| Cookieless ID | Also computed, at enrichment: a daily-salted hash of IP + UA + site, as Plausible does. | Links hits from visitors whose storage is blocked or cleared. |
| Fingerprinting | **Allowed.** Device and browser signals plus optional canvas/WebGL/audio hashes, each switchable. | Reliable identity stitching, as leading tools do. |
| Headers | Capture **all** request headers, except the values of secrets (`Cookie` other than Tidesight's own, `Authorization`, configurable secret patterns). | Don't throw away potentially useful data; never store credentials. |
| Heavy features | Session replay, mouse-movement heatmaps, click heatmaps, rage and dead clicks: all supported, each configurable and sampleable. | Parity with Hotjar/Clarity without making every site pay the cost. |
| GDPR / consent | Not a concern for now. An easy privacy mode plus clean-up scripts come later. | See principle 4. |
| Scope | Generic and multi-site. | Tidesight serves any number of sites from one deployment. |
| Licence | MIT (suggested during planning; adopted provisionally). | Compatible with rrweb (MIT). GeoLite2 data has its own licence and is never bundled. |

## Design choices made during implementation

These follow from the principles above. Each one is a trade-off worth knowing before changing it.

- **Three database files.** `events.db` holds raw requests and hits and is written only by the collector. `replay.db` holds heavy chunks (replay, mouse movement) so the main database stays small. `derived.db` holds everything the jobs compute and can be rebuilt from scratch. Separate files also mean an enrichment job never holds the write lock the collector needs.
- **One row per request plus one row per event.** `raw_requests` keeps what arrived over HTTP (IP, headers, envelope); `raw_hits` keeps each event of a batch as received, with a few typed columns for indexing. Shared request data is stored once per batch rather than once per event.
- **Client IP decided at enrichment.** Ingest stores `REMOTE_ADDR` and every forwarding header; which one is the visitor's address depends on the deployment and is decided later (`enrich.ip_header`, `enrich.trusted_proxies`).
- **CORS reflects the origin on ingest endpoints.** The ingest endpoints are write-only and return nothing sensitive, so reflecting any `Origin` lets the tracker read status codes (and retry on 5xx) even when a site's allowed-origins list is out of date. The hit is still flagged when its origin is not allowed. `server.cors = "allowed"` restricts this.
- **`text/plain` bodies.** The tracker posts JSON as `text/plain` (optionally gzip, detected by magic bytes), so browsers never send a CORS preflight.
- **Tracker core plus lazy plugins.** The core (pageviews, identity, sessions, engagement, delivery, context) stays small; each behaviour feature is a separate file loaded only when the site's config enables it and the session is sampled in. A single-file bundle is also built for sites that prefer one request.
- **Sampling per session.** A plugin's sampling decision is a deterministic function of the session id, so a session is entirely in or entirely out.
- **Session ids from the client, rebuilt on the server.** The tracker keeps a session id (30-minute idle timeout, optional split at midnight in the site's time zone); enrichment trusts it when present and rebuilds sessions from the cookieless hash for hits without one (pixel hits).
- **Fingerprints are evidence, not identity.** Identity stitching links ids through the persistent visitor id and known user ids first; fingerprint and cookieless hashes only link visitors together with other evidence, and every link carries a confidence score.
- **Abuse limits favour availability over completeness.** Anyone can send hits, so every write is bounded per client and minute (requests and kilobytes, with smaller allowances for traffic that would only be stored flagged, per client and for all clients together), the client being the connecting address (a forwarding header counts only from configured proxies). Requests over an allowance are counted, not stored. A daily quota per site exists but is off by default: a flood would use it up and lock real visitors out. Writes stop before the disk fills.
- **Refuse before working.** Decompressing and parsing a body is the costly part of a request, so a client already over its allowance for the minute is refused before its body is looked at; the same holds for enrichment and the API, whose work on stored data is bounded whatever that data holds (a session of more than 20,000 hits is built once, as a bot; an API call decodes a bounded number of objects).
- **Short write transactions.** Maintenance jobs (retention, privacy clean-up) write in batches of a few thousand rows, so the collector never waits long for the database; and after one failed write the collector sends the next 30 seconds of writes straight to the spool rather than making each request wait for a lock.
- **Secrets in URLs are redacted where they appear, not derived later.** Raw first stops at secrets (principle 5 of `AGENTS.md`): the tracker redacts tokens, passwords and session ids in every URL it sends, and the collector applies the same rules (`redact.py`, shared test vectors) to the `Referer` header it stores. Patterns are matched in linear time, never compiled to regular expressions, so a hostile URL cannot stall a page or the collector.
- **FastCGI instead of Passenger.** Dreamhost removed Passenger from shared plans in 2024; its Apache still runs `.fcgi` programs through mod_fcgid, which keeps a process for request after request (about 10 ms a request on Dreamhost, against 150 ms for CGI). Python's standard library has no FastCGI server, so `tidesight/fcgi.py` implements the responder role (under 500 lines, one request per connection as mod_fcgid sends them, no multiplexing) rather than depend on flup. mod_fcgid does not restart a program whose files change, so each process watches its own code and stops after the request in progress when it changes: an update needs no restart step. The same file answers as plain CGI when a server runs it that way.
- **Everything in one folder, protected and checked.** The data directory may live in the website's folder, next to the code and the configuration, as owners expect from WordPress and its kin; until 2026-10 Tidesight refused to run there. WordPress is safe there because its database is elsewhere and its secrets run as PHP; Tidesight's databases are plain files, so the protection is layered and verified: the folder's `.htaccess` sends every request, for files that exist too, to the app (which serves only its own public files); the data directory has its own `.htaccess` refusing everything; files are readable by the account only; and `jobs.py cron` asks the website, as an outsider, for a file put in each of Tidesight's folders, stopping the collector and the dashboard while one can be downloaded. The check first proves that `public_url` reaches this very installation (its `/health` answers a digest keyed by the installation's secret): a `public_url` pointing elsewhere would otherwise answer `404` and pass every time. A check that cannot conclude never clears an exposure it found before, and says why. A refusal on a mere path comparison protected nothing more, and kept the simple layout out of reach.
- **People invited with links, no email.** Admins invite people and help those who forgot their password with one-time links that they pass on themselves (an invitation for a week, a reset for a day), and anyone can change their own password. Tidesight sends no email: no mail server to set up, no address to keep, and no form a stranger can use to send mail in someone's name (reset forms that mail links are used to flood inboxes, and their messages are what phishing imitates). When no admin can sign in, `jobs.py user link NAME` on the server makes the link: whoever can run it controls the server anyway. Roles are stored with each person in `users.json`; nobody changes their own role or removes themselves, and Tidesight never runs out of admins who can sign in.
- **The first account needs a code from the server.** A form that lets whoever arrives first create the admin account is how many self-hosted apps start (WordPress, Matomo), and how they get taken over: every new HTTPS certificate is public within minutes (Certificate Transparency), and bots try the setup pages of the sites they discover. Tidesight's form asks for a setup code that it writes in the data directory (mode 600), as Jenkins asks for its initial admin password: whoever can read it controls the server anyway. The code has 80 random bits, attempts count like failed sign-ins, and it is deleted once a user exists. Default credentials (admin/admin) were ruled out: they are the first thing bots try.
- **Dashboard users in the data directory.** The people managed in the dashboard (and with `jobs.py user`) are kept in `data_dir/users.json` (password hashes and roles), merged with the configuration file's `[dashboard] users` (which win), for the same reason properties have a file of their own: the dashboard can change people without rewriting the owner's file, and passwords are typed by their owners, never seen by anyone else.
- **Properties in a file of their own.** Sites added in the dashboard go to `data_dir/properties.json`, never into the configuration file: the file stays the owner's (comments, layout, secrets untouched), a dashboard bug cannot break it, and the collector merges both at load with the file winning. The JSON is written atomically under a lock, from what is on disk at that moment, so two admins saving at once lose nothing. Deleting a property's data is queued and done by `jobs.py cron`: a web request never runs a job's worth of deletes.
- **Sessions for people, Basic for scripts.** The dashboard signs in once and holds a random token in an HttpOnly, `SameSite=Strict` cookie scoped to the dashboard's path (so the collector, which keeps `ts_*` cookies, never receives it; it also refuses to keep it); the database keeps only the token's hash and a digest of the password hash, so a copy of `web.db` signs nobody in and a password change ends every session. Checking a password costs a PBKDF2 run; with sessions it happens once per sign-in, which makes the dashboard usable under CGI too. Changes require a custom header and the dashboard's own origin, which a forged cross-site request cannot provide.
- **Reports are computed on the fly from `derived.db`.** Filters, comparisons, second dimensions, goals and paging are SQL over the sessions, pageviews and events tables (with their `site_id, day` indexes), every column taken from allow-lists and every value bound: no precomputed cube to keep in sync, and any dimension combines with any filter. Goals are matched at query time (`GLOB` on event names and paths), so adding or editing a goal applies to past data at once.
- **The tracker trusts nothing on the host page.** Pages often render user content: the tracker only accepts a plain configuration object, reads its own script element and document properties through their accessors (markup cannot shadow them), and loads scripts only from its own `http(s)` base URL. Its requests send only the origin as `Referer`.
