# Deploying on Dreamhost (shared hosting)

This guide installs Tidesight on a Dreamhost shared plan: the collector, dashboard and API run under Passenger on a subdomain such as `stats.example.com`, and cron runs the jobs. The same steps work on most Passenger hosts. Without Passenger, or in a folder of an existing site, use CGI (at the end).

## Passenger or CGI?

Both run the same application; the difference is how long its process lives.

| | Passenger (a subdomain) | CGI (a subdomain or a folder) |
|---|---|---|
| Process | Starts once, then serves request after request (Passenger stops it when idle and starts it again on the next request). | A new Python process for every request: about 0.1–0.3 s of start-up each time on shared hosting. |
| Tracker requests | Answered in a few milliseconds. | Each pageview and event batch pays the start-up: fine for small sites, heavy for busy ones. |
| Caches | The configuration, open database connections and checked passwords stay in memory. | Rebuilt for every request (allowances and sign-in throttles are kept in SQLite, so they still hold). |
| Where | Dreamhost runs Passenger per domain: it takes over the whole (sub)domain, which therefore needs its own website in the panel. | Anywhere CGI scripts run, including a folder of a site you already host (`example.com/tidesight/`). |

Use Passenger on a subdomain such as `stats.example.com` when you can; CGI is the fallback for a folder of an existing site or a host without Passenger. The dashboard works the same in both (signing in sets a session cookie, so the password is checked once, not on every request).

**A folder of a site hosted elsewhere** (for example a domain whose web pages are served by Cloudflare Pages, Netlify or Vercel) is not reachable on Dreamhost, even if the folder exists there: requests for that domain never reach Dreamhost. Point a subdomain at Dreamhost instead (a DNS record where the domain's DNS is managed, plus the subdomain added in the Dreamhost panel), or have the other host forward the folder to that subdomain (a reverse proxy such as a Cloudflare Worker or Pages Function). With a proxy, set `[server] public_url` to the address visitors use, and make the proxy pass the visitor's address in a header (`rate_limit_key`, `rate_limit_proxies`).

Layout used below (replace `USER` and the domain names):

```
~/tidesight/                      code (git clone), never served
~/tidesight/tidesight.toml        configuration (outside the web root)
~/tidesight-data/                 databases, spool, backups, geo databases (outside the web root: the collector
                                  refuses to run when it finds them inside the web server's document root)
~/stats.example.com/              Passenger app root
    passenger_wsgi.py             copied from ~/tidesight/server/python/
    public/                       web root (optional static copies of the tracker)
    tmp/restart.txt               touch to restart the app
```

## 1. Prepare the account

1. In the panel, make sure your user is a **shell user** (SSH access).
2. Add the subdomain `stats.example.com` as a hosted website, and enable **Passenger** for it (the web directory becomes `~/stats.example.com/public`).
3. Turn on HTTPS for the subdomain (free Let's Encrypt certificate). Tracking from an `https` site needs an `https` collector.

## 2. Check the host (Phase 0)

```sh
ssh USER@example.com
git clone https://github.com/<you>/tidesight.git ~/tidesight
python3 --version
python3 ~/tidesight/tools/sqlite_host_test.py --data-dir ~/tidesight-data/_hosttest
```

It ends with `OVERALL` and a `RECOMMENDATION` line such as `storage_mode = "sqlite", journal_mode = "wal"`. Use that in the configuration. If it says `spool`, set `storage_mode = "spool"` (cron then loads the spool into SQLite). If `python3` is older than 3.9, install a newer Python with pyenv and use it below (step 5).

## 3. Configure

```sh
cp ~/tidesight/config/tidesight.example.toml ~/tidesight/tidesight.toml
chmod 600 ~/tidesight/tidesight.toml
mkdir -p ~/tidesight-data && chmod 700 ~/tidesight-data   # only your account may read the data
```

Tidesight creates its files readable by your account only. Files left by older versions may be more open: `chmod -R go-rwx ~/tidesight-data` once fixes them.

Edit at least:

```toml
[server]
data_dir = "/home/USER/tidesight-data"
storage_mode = "sqlite"            # from the Phase 0 recommendation
journal_mode = "wal"               # idem
secret = "…"                       # python3 -c "import secrets; print(secrets.token_hex(32))"

[dashboard]
users = { admin = "pbkdf2_sha256$…" }   # see below
```

Sites ("properties") are easiest to add in the dashboard once it runs (Properties → Add property: the domain, a time zone, what to record, and the tracking code to paste). They can also be defined in the file, where they stay read-only in the dashboard:

```toml
[sites.example]
name = "Example"
origins = ["https://example.com", "https://www.example.com"]
timezone = "Europe/Paris"
```

Create the dashboard password hash:

```sh
cd ~/tidesight/server/python && python3 -m tidesight.jobs hash-password --user admin
```

Check the configuration: `python3 -m tidesight.jobs --config ~/tidesight/tidesight.toml status`.

## 4. Install the Passenger app

```sh
cp ~/tidesight/server/python/passenger_wsgi.py ~/stats.example.com/
mkdir -p ~/stats.example.com/tmp && touch ~/stats.example.com/tmp/restart.txt
```

`passenger_wsgi.py` finds the code in `~/tidesight/server/python` and the configuration in `~/tidesight/tidesight.toml`. Open `https://stats.example.com/health`: it must answer `{"status": "ok", …}`. The dashboard is at `https://stats.example.com/dashboard/`.

**Optional, faster static files.** Passenger serves files that exist in `public/` directly, without Python. To serve the tracker that way:

```sh
cp -r ~/tidesight/tracker/build/ts.js ~/tidesight/tracker/build/ts.all.js ~/tidesight/tracker/build/ts ~/stats.example.com/public/
```

Repeat after each update (or skip it: the app serves `/ts.js` and `/ts/…` itself, with long cache headers).

## 5. Use another Python (only if needed)

If the system `python3` is too old, build one with pyenv, then point the app at it:

```sh
echo "$HOME/.pyenv/versions/3.12.7/bin/python3" > ~/stats.example.com/.tidesight-python
touch ~/stats.example.com/tmp/restart.txt
```

Use the same interpreter in the cron lines below.

## 6. Cron jobs

`crontab -e` (or the panel's cron page):

```cron
# Load the spool (if any), enrich new hits, update rollups: every 2 minutes.
*/2 * * * * cd ~/tidesight/server/python && python3 -m tidesight.jobs -q --config ~/tidesight/tidesight.toml cron >> ~/tidesight-data/cron.log 2>&1
# Nightly backup, 14 kept.
15 3 * * * cd ~/tidesight/server/python && python3 -m tidesight.jobs -q --config ~/tidesight/tidesight.toml backup --keep 14 >> ~/tidesight-data/cron.log 2>&1
# Monthly geo database refresh (DB-IP Lite, plus MaxMind GeoLite2 when a licence key is configured).
30 4 2 * * cd ~/tidesight/server/python && python3 -m tidesight.jobs -q --config ~/tidesight/tidesight.toml geo-update >> ~/tidesight-data/cron.log 2>&1
# Weekly: optimise the databases.
45 4 * * 0 cd ~/tidesight/server/python && python3 -m tidesight.jobs -q --config ~/tidesight/tidesight.toml vacuum >> ~/tidesight-data/cron.log 2>&1
```

Run `geo-update` once by hand right away so the first enrichment has locations. Geolocation by DB-IP (CC BY 4.0) requires the attribution the dashboard shows. For MaxMind, create a free GeoLite2 account and set `maxmind_account_id` and `maxmind_license_key` in `[enrich]`.

## 7. Add the tracker to your site

The dashboard shows each property's tracking code (Properties → the property → Tracking code), and turns green when the first hit arrives. In the `<head>` of every page:

```html
<script async src="https://stats.example.com/ts.js" data-site="example"></script>
```

Optional extras:

```html
<!-- Calls made before the script loads are queued -->
<script>window.tidesight=typeof tidesight=="function"?tidesight:function(){(tidesight.q=tidesight.q||[]).push(arguments)};</script>
<!-- Visitors without JavaScript -->
<noscript><img src="https://stats.example.com/p.gif?site=example" alt="" width="1" height="1"></noscript>
<!-- Mark error pages -->
<meta name="ts:status" content="404">
```

Custom events: `tidesight('track', 'signup', {plan: 'pro'})`. Known users: `tidesight('identify', 'user-42')`. Exclude your own browser: run `tidesight('ignore')` in its console. The other commands and options are in `docs/tracker.md`.

When the collector shares the site's registrable domain (`stats.example.com` for `example.com`), set `server_cookie_domain = ".example.com"` for the site: the collector then also keeps the visitor id in a server-set cookie, which browsers keep longer than script-set ones.

## 8. Updating

```sh
cd ~/tidesight && git pull
touch ~/stats.example.com/tmp/restart.txt
```

Database migrations run automatically. When an update changes how data is derived, the next enrichment rebuilds `derived.db` in the background and swaps it in.

## CGI (no Passenger, or a folder of an existing site)

Copy `server/python/collect.cgi` into the web directory of `stats.example.com` (or into a folder of a site, e.g. `~/example.com/tidesight/`), `chmod 755` it, and add a `.htaccess` next to it:

```apache
Options +ExecCGI
AddHandler cgi-script .cgi
CGIPassAuth On
RewriteEngine On
RewriteCond %{REQUEST_FILENAME} !-f
RewriteRule ^(.*)$ collect.cgi/$1 [L,QSA]
```

In a folder, the addresses gain its name: the tracker is `https://example.com/tidesight/ts.js` and the dashboard `https://example.com/tidesight/dashboard/` (set `public_url = "https://example.com/tidesight/"` so the tracking code the dashboard shows is right). Keep `tidesight.toml` and the data directory outside the web root as above: the collector refuses to run when they are inside it.

`CGIPassAuth On` (Apache 2.4.13+) lets HTTP Basic credentials through, for scripts using the API (the dashboard itself signs in with a cookie); on older Apache use `RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]` instead. CGI starts a process per request: fine for small sites. Copy the tracker files into the web directory to avoid a CGI run per script download.

## Troubleshooting

- **`/health` fails:** look at `~/stats.example.com/` error log (panel → logs) or set `log_file` in `[server]`. A configuration error is reported there.
- **Nothing recorded:** open the site with the browser's network panel: `ts.js`, `config.js` and `collect` must answer 200/204. Check `origins` (hits from other origins are stored but flagged: see the dashboard's Status page).
- **Signing in fails:** the user name or hash is wrong; hashes are created with `jobs.py hash-password`. After 10 failures a client waits 10 minutes (`429`).
- **A property says "No data":** its tracking code is missing, or the site runs on a domain the property does not list: its Tracking code tab shows the refused origins, with a button to allow each.
- **`database is locked`:** the collector falls back to the spool automatically; if it happens often, use `journal_mode = "delete"` or `storage_mode = "spool"` per the Phase 0 test.
- **Stale data:** check `~/tidesight-data/cron.log` and `jobs.py status` (last enrichment time).
