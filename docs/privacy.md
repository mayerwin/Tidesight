# Privacy mode and clean-up jobs

Tidesight is data-rich by default: it stores raw IP addresses, a persistent visitor id (cookie and localStorage) and device fingerprints, because the owner wants maximum insight and consent is not a concern today. When a site needs to collect less, privacy mode is **a configuration change plus a clean-up job**, mirroring what Plausible, Matomo and Google Analytics offer. Nothing needs to be rewritten.

This page describes the tools. It is not legal advice: what a given law requires (consent banners, records, data-subject requests) depends on the site and its audience.

## Going forward: configuration switches

Set them in `[privacy]` (all sites) or `[sites.<id>.privacy]` (one site). They apply to new data as soon as the collector reloads the configuration (within seconds).

| Switch | Effect at collection time |
|---|---|
| `truncate_ip = true` | Only the /24 (IPv4) or /48 (IPv6) network is stored, in `raw_requests.ip` and in every IP-bearing header (`X-Forwarded-For`, `Forwarded`, `CF-Connecting-IP`, and any header whose name contains `ip`, `forwarded`, `client` or `address`). City-level geolocation keeps working approximately. |
| `hash_ip = true` | A keyed hash (`h:` + 16 hex digits, HMAC-SHA256 with the deployment secret) replaces each address. Combined with `truncate_ip`, the network is hashed. Geolocation is no longer possible for new hits. |
| `honour_dnt = true` | Browsers sending Do Not Track are not tracked: the tracker stays silent and the collector drops such requests (counted in `counters` as `dropped_dnt`). |
| `honour_gpc = true` | Same for Global Privacy Control (`dropped_gpc`). |
| `drop_fingerprint = true` | Every fingerprint feature is turned off for the tracker, and `fp` events are dropped at ingest. |

Features can also be switched off one by one (`features.replay = false`, `features.mouse_moves = false`, `features.fingerprint = false`, …), and `exclude_paths` keeps the tracker silent on chosen pages. A visitor can opt out on their own browser with `tidesight('ignore')`.

The visitor id is the backbone of sessions, so there is no switch to turn it off. A site that must not store identifiers on visitors' devices without consent should load the tracker only once consent is given. Without the tracker, the no-JS pixel (`/p.gif`) and the cookieless daily hash computed at enrichment (salted hash of IP and user agent, as Plausible does) still count visitors approximately.

## Looking back: `jobs.py privacy-clean`

Rewrites data already stored. Every step is opt-in; the job is a **dry run unless `--apply` is given**, and with `--apply` it takes a backup first (unless `--no-backup`).

```sh
cd ~/tidesight/server/python
python3 -m tidesight.jobs --config ~/tidesight/tidesight.toml privacy-clean --truncate-ip            # what would change
python3 -m tidesight.jobs --config ~/tidesight/tidesight.toml privacy-clean --truncate-ip --apply    # do it
```

| Option | Effect |
|---|---|
| `--truncate-ip`, `--hash-ip` | Anonymise stored IPs (requests and chunks) and IP-bearing headers (`X-Forwarded-For`, `Via`, any header whose name contains `ip`, `forwarded`, `client` or `addr`…; a 6to4 IPv6 address is cut to its /40, the /24 of the IPv4 address inside it). Before that, each request's derived geolocation is saved in `raw_requests.geo`, so locations survive the anonymisation and later rebuilds. |
| `--drop-fingerprint` | Delete every `fp` event (fingerprints and automation signals). |
| `--drop-replay` | Delete session-replay chunks and DOM snapshots. |
| `--drop-mouse` | Delete mouse-movement chunks. |
| `--retention` | Apply the `[retention]` windows (`raw_days`, `replay_days`, `mouse_days`). Daily aggregates of deleted days are kept. |

Every option covers the yearly archive files (`jobs.py archive`) as well as the live databases.

After an `--apply`, `derived.db` is marked stale; the next `enrich` run rebuilds it from the cleaned raw data (the rebuild keeps the rollups of days whose raw data is gone). The databases are vacuumed so deleted data does not linger in free pages; backups taken before the clean-up still contain it, so rotate or delete them as needed (`data_dir/backups`).

Run the retention step from cron to enforce a window continuously:

```cron
0 5 * * * cd ~/tidesight/server/python && python3 -m tidesight.jobs -q --config ~/tidesight/tidesight.toml privacy-clean --retention --apply --no-backup
```

## What is never stored

- Values typed, copied, pasted or selected by visitors (forms, clipboard, selection: only lengths and timings).
- Password fields in session replays (always masked); other inputs and the text of rich-text editors (`contenteditable`, `role="textbox"`), with the links, images, labels and data attributes inside them, are masked by default (`replay.mask_all_inputs`); elements with the `ts-mask` class (text and those attributes masked) or `ts-block` class (not recorded) can be excluded, and `replay.mask_all_text` masks every text node (for code editors that draw what visitors type outside inputs). Click text never includes masked, editable or form content.
- Cookie values other than Tidesight's own (`ts_*`), and the values of `Authorization` and any header matching `headers.secret_patterns` (by default also `*auth*`, `*session*`, `*jwt*`, `*cookie*`…).
- Client addresses in the per-minute allowances (`rate_limits`): they are keyed by a keyed hash of the client, whatever the privacy settings.
- Secrets in URLs: the values of parameters matching `redact_params` (tokens, passwords, keys, session ids, signatures…), the parts of paths matching `redact_paths` and credentials before a host become `redacted` in every URL the tracker sends (pages, referrers, links, form actions, error messages and stacks, the URLs inside replays and snapshots) and in the `Referer` header the collector stores. Error reports (and error clicks) never include the data of a rejected promise, only its type and message. Two things are stored as sent: URLs from trackers older than 0.3 (the redaction then is that of the tracker that sent them), and the parameters of a pixel URL (`/p.gif?u=…`), which the site builds itself.

## Deleting one visitor's data

Find their visitor key in the dashboard (Visitor page) or by user id, then delete the raw hits of the linked visitor ids (`vid_map`), and rebuild:

```sh
sqlite3 ~/tidesight-data/events.db "DELETE FROM raw_hits WHERE visitor_id IN ('…','…');"
python3 -m tidesight.jobs --config ~/tidesight/tidesight.toml enrich --rebuild
```

Replay chunks are keyed by `visitor_id` and `session_id` in `replay.db` (`DELETE FROM chunks WHERE visitor_id IN (…)`).

## Deleting a whole property's data

In the dashboard, Properties → the property → Delete → "Delete its data": the property is removed at once, and the next `jobs.py cron` run deletes everything stored under its id (raw requests and hits, archives included, recordings, derived rows, its segments and notes), in short transactions. From the command line, once the site is no longer configured:

```sh
python3 -m tidesight.jobs --config ~/tidesight/tidesight.toml purge-site OLD_ID            # dry run: what would go
python3 -m tidesight.jobs --config ~/tidesight/tidesight.toml purge-site OLD_ID --apply
```

Backups made before keep the data until they are rotated out.
