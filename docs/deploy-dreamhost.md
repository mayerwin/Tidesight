# Deploying on Dreamhost (shared hosting)

This guide installs Tidesight on a Dreamhost shared plan: the collector, dashboard and API run under Passenger on a subdomain such as `stats.example.com`, and cron runs the jobs. The same steps work on most Passenger hosts. Without Passenger, use the CGI fallback at the end.

Layout used below (replace `USER` and the domain names):

```
~/tidesight/                      code (git clone), never served
~/tidesight/tidesight.toml        configuration (outside the web root)
~/tidesight-data/                 databases, spool, backups, geo databases (outside the web root)
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
```

Edit at least:

```toml
[server]
data_dir = "/home/USER/tidesight-data"
storage_mode = "sqlite"            # from the Phase 0 recommendation
journal_mode = "wal"               # idem
secret = "…"                       # python3 -c "import secrets; print(secrets.token_hex(32))"

[dashboard]
users = { admin = "pbkdf2_sha256$…" }   # see below

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

In the `<head>` of every page:

```html
<script async src="https://stats.example.com/ts.js" data-site="example"></script>
```

Optional extras:

```html
<!-- Calls made before the script loads are queued -->
<script>window.tidesight=window.tidesight||function(){(tidesight.q=tidesight.q||[]).push(arguments)};</script>
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

## CGI fallback (no Passenger)

Copy `server/python/collect.cgi` into the web directory of `stats.example.com`, `chmod 755` it, and add a `.htaccess` next to it:

```apache
Options +ExecCGI
AddHandler cgi-script .cgi
CGIPassAuth On
RewriteEngine On
RewriteCond %{REQUEST_FILENAME} !-f
RewriteRule ^(.*)$ collect.cgi/$1 [L,QSA]
```

`CGIPassAuth On` (Apache 2.4.13+) lets the dashboard's Basic authentication through; on older Apache use `RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]` instead. CGI starts a process per request: fine for small sites. Copy the tracker files into the web directory to avoid a CGI run per script download.

## Troubleshooting

- **`/health` fails:** look at `~/stats.example.com/` error log (panel → logs) or set `log_file` in `[server]`. A configuration error is reported there.
- **Nothing recorded:** open the site with the browser's network panel: `ts.js`, `config.js` and `collect` must answer 200/204. Check `origins` (hits from other origins are stored but flagged: see the dashboard's Status page).
- **Dashboard asks for a password forever:** the user name or hash is wrong; hashes are created with `jobs.py hash-password`.
- **`database is locked`:** the collector falls back to the spool automatically; if it happens often, use `journal_mode = "delete"` or `storage_mode = "spool"` per the Phase 0 test.
- **Stale data:** check `~/tidesight-data/cron.log` and `jobs.py status` (last enrichment time).
