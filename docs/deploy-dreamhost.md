# Deploying on Dreamhost (shared hosting)

This guide installs Tidesight on a Dreamhost shared plan, on a subdomain such as `stats.example.com`: the collector, dashboard and API run as a FastCGI program, and cron runs the jobs. The same steps work on most shared hosts running Apache with FastCGI (mod_fcgid) or CGI.

## FastCGI, CGI or Passenger?

All three run the same application; the difference is how long its process lives. Dreamhost removed Passenger from its shared plans in 2024, but its Apache still runs FastCGI programs: a file whose name ends in `.fcgi` is started on the first request and then answers request after request, as a Passenger app did.

| | FastCGI (`tidesight.fcgi`) | CGI (`collect.cgi`) |
|---|---|---|
| Process | Started on the first request, then kept: it answers request after request. The web server stops it after a few idle minutes (about five on Dreamhost) and starts one again on the next request (or several, when requests overlap). | A new Python process for every request. |
| Time per request (measured on Dreamhost, October 2026, `/health` from the same server) | About 10 ms; the first request after a pause, which starts the process, up to about a second. | About 150 ms, every time. |
| Caches | The configuration, open databases and checked passwords stay in memory. | Rebuilt for every request (allowances and sign-in throttles are kept in SQLite, so they still hold). |
| Updates | A process notices new code within seconds and makes way for a fresh one, after the request in progress. | Every request runs the current code. |

Use FastCGI. `tidesight.fcgi` also answers as plain CGI when a server runs it that way; `collect.cgi` is for hosts that run only `.cgi` files. On hosts that still offer Passenger, `passenger_wsgi.py` works as well (at the end).

**A folder of a site hosted elsewhere** (for example a domain whose web pages are served by Cloudflare Pages, Netlify or Vercel) is not reachable on Dreamhost, even if the folder exists there: requests for that domain never reach Dreamhost. Point a subdomain at Dreamhost instead (a DNS record where the domain's DNS is managed, plus the subdomain added in the Dreamhost panel), or have the other host forward the folder to that subdomain (a reverse proxy such as a Cloudflare Worker or Pages Function). With a proxy, set `[server] public_url` to the address visitors use, and make the proxy pass the visitor's address in a header (`rate_limit_key`, `rate_limit_proxies`).

Layout used below (replace `USER` and the domain names):

```
~/tidesight/                      code (git clone), never served
~/tidesight/tidesight.toml        configuration (outside the web root)
~/tidesight-data/                 databases, spool, backups, geo databases, users, log (outside the web root: the
                                  collector refuses to run when it finds them inside the web server's document root)
~/stats.example.com/              the subdomain's web directory
    tidesight.fcgi                copied from ~/tidesight/server/python/
    .htaccess                     sends every request to tidesight.fcgi
    robots.txt                    keeps search engines out (Dreamhost serves its own when there is none)
```

## 1. Prepare the account

1. In the panel, make sure your user is a **shell user** (SSH access).
2. Add the subdomain `stats.example.com` as a hosted website. Its web directory is `~/stats.example.com`.
3. **If the domain's DNS is managed elsewhere** (Cloudflare, a registrar), add an `A` record for the subdomain there, pointing at the address Dreamhost gives it (`dig +short stats.example.com @ns1.dreamhost.com` prints it; add an `AAAA` record too if `dig AAAA` shows one). On Cloudflare, make it **DNS only** (grey cloud), at least until the certificate is issued.
4. Turn on HTTPS for the subdomain (the free Let's Encrypt certificate) once its DNS points at Dreamhost. Tracking from an `https` site needs an `https` collector.
5. **Turn off Extra Web Security** for the subdomain (in the website's settings in the panel). It is Dreamhost's web application firewall (ModSecurity with the OWASP rules): it takes the tracker's compressed batches and session-replay chunks for attacks and refuses them (`418`, or a `500` error page), so most hits would be lost. Tidesight checks and bounds everything it receives itself. Step 4 below shows how to check.

**Behind Cloudflare's proxy** (orange cloud, optional): use the SSL mode "Full (strict)" (the Let's Encrypt certificate stays on Dreamhost), and keep the two `rate_limit_*` lines of step 3: the visitor's address is then taken from Cloudflare's header, believed only when the request comes from one of Cloudflare's addresses, so the same lines are right with and without the proxy. Geolocation follows the same rule by itself (`[enrich] ip_header = "auto"`).

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
public_url = "https://stats.example.com/"
storage_mode = "sqlite"                    # from the Phase 0 recommendation
journal_mode = "wal"                       # idem
log_file = "/home/USER/tidesight-data/tidesight.log"   # see below
rate_limit_key = "cf-connecting-ip"        # only if the domain is on Cloudflare (step 1)
rate_limit_proxies = ["cloudflare"]
```

`log_file` matters under FastCGI: Dreamhost does not put a FastCGI program's own messages in the site's error log, so without it Tidesight's errors are nowhere to be seen. The secret used for hashing is generated in the data directory on first use (`[server] secret` overrides it).

Add yourself as a dashboard user (it asks for the password twice and keeps only its hash, in `~/tidesight-data/users.json`):

```sh
cd ~/tidesight/server/python && python3 -m tidesight.jobs user set admin
```

`user set` on an existing user changes the password (and ends that user's sessions), `user remove NAME` removes one and `user list` shows them all. Users can also be written in the configuration file (`[dashboard] users`, hashes from `jobs.py hash-password`): those win, and are changed there. With several users, `[dashboard] admins` names those who may change properties.

Sites ("properties") are easiest to add in the dashboard once it runs (Properties → Add property: the domain, a time zone, what to record, and the tracking code to paste). They can also be defined in the file, where they stay read-only in the dashboard:

```toml
[sites.example]
name = "Example"
origins = ["https://example.com", "https://www.example.com"]
timezone = "Europe/Paris"
```

Check the configuration: `python3 -m tidesight.jobs --config ~/tidesight/tidesight.toml status`.

## 4. Install the web entry point

```sh
cp ~/tidesight/server/python/tidesight.fcgi ~/stats.example.com/
chmod 755 ~/stats.example.com/tidesight.fcgi
printf 'User-agent: *\nDisallow: /\n' > ~/stats.example.com/robots.txt
cat > ~/stats.example.com/.htaccess <<'EOF'
Options +ExecCGI -Indexes
CGIPassAuth On
RewriteEngine On
# The dashboard and share pages only over HTTPS: passwords and session cookies never travel in clear.
RewriteCond %{HTTPS} !=on
RewriteRule ^(dashboard|share)(/.*)?$ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]
RewriteCond %{REQUEST_FILENAME} !-f
RewriteRule ^(.*)$ tidesight.fcgi/$1 [L,QSA]
EOF
```

The tracker's addresses answer over HTTP too (pages served over HTTP can still be measured); the dashboard is sent to HTTPS, so it opens only once the certificate exists. Behind Cloudflare's proxy, the SSL mode must be "Full (strict)": with "Flexible", Cloudflare would reach Dreamhost over HTTP and the redirect would loop.

Dreamhost runs `.fcgi` files with FastCGI as they are. On another Apache host with mod_fcgid, add `AddHandler fcgid-script .fcgi` to the `.htaccess`, but only where mod_fcgid is installed: without it, Apache would send the file instead of running it. `CGIPassAuth On` lets HTTP Basic credentials reach the app, for scripts using the API (the dashboard signs in with a cookie).

`tidesight.fcgi` finds the code in `~/tidesight/server/python` and the configuration in `~/tidesight/tidesight.toml`. Open `https://stats.example.com/health`: it must answer `{"status": "ok", …}`. The dashboard is at `https://stats.example.com/dashboard/`.

Then check that the web application firewall lets compressed data through (step 1.5). `200` is right; `418` or `500` means Extra Web Security is still on:

```sh
gzip -c ~/tidesight/tracker/build/ts.js | curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Content-Type: text/plain' --data-binary @- https://stats.example.com/health
```

## 5. Use another Python (only if needed)

`tidesight.fcgi` runs with the `python3` on the server's path (Python 3.12 on Dreamhost in 2026). To use another interpreter, built with pyenv for instance, change the first line of `~/stats.example.com/tidesight.fcgi` to its path:

```
#!/home/USER/.pyenv/versions/3.12.7/bin/python3
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

The backup keeps the raw data (`events.db`), the dashboard's own data (`web.db`: saved segments, notes) and the settings of the data directory (`properties.json`, `users.json`); `--with-replay` and `--with-derived` add the other databases.

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
```

That is all: within seconds, each running FastCGI process notices the new code and makes way for a fresh one (after the request it is answering), and the next request starts it. `touch ~/stats.example.com/tidesight.fcgi` does the same on demand. Database migrations run automatically. When an update changes how data is derived, the next enrichment rebuilds `derived.db` in the background and swaps it in.

## CGI (hosts without FastCGI)

Copy `server/python/collect.cgi` into the web directory of `stats.example.com` (or into a folder of a site, e.g. `~/example.com/tidesight/`), `chmod 755` it, and add a `.htaccess` next to it:

```apache
Options +ExecCGI -Indexes
AddHandler cgi-script .cgi
CGIPassAuth On
RewriteEngine On
RewriteCond %{REQUEST_FILENAME} !-f
RewriteRule ^(.*)$ collect.cgi/$1 [L,QSA]
```

In a folder, the addresses gain its name (with FastCGI as well): the tracker is `https://example.com/tidesight/ts.js` and the dashboard `https://example.com/tidesight/dashboard/` (set `public_url = "https://example.com/tidesight/"` so the tracking code the dashboard shows is right). Keep `tidesight.toml` and the data directory outside the web root as above: the collector refuses to run when they are inside it.

`CGIPassAuth On` needs Apache 2.4.13 or later; on older Apache use `RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]` instead. CGI starts a process per request: fine for small sites. Copy the tracker files into the web directory to avoid a CGI run per script download.

## Passenger (other hosts)

Where Passenger still runs Python apps, copy `server/python/passenger_wsgi.py` into the app root (`~/stats.example.com/`): it finds the code and the configuration as `tidesight.fcgi` does. After an update, `touch ~/stats.example.com/tmp/restart.txt`. To use another interpreter, write its path in `~/stats.example.com/.tidesight-python`. Passenger serves the files of `public/` directly: copying the tracker there (`ts.js`, `ts.all.js`, `ts/`) saves Python a request per script download.

## Troubleshooting

- **`/health` fails:** read `~/tidesight-data/tidesight.log` (with `log_file` set) and the site's error log (`~/logs/stats.example.com/http/error.log`). `python3 -m tidesight.jobs status` checks the configuration.
- **Tracker requests fail with `418` or `500` while `/health` works:** Extra Web Security is on for the subdomain (step 1.5); the error log then shows `ModSecurity: Access denied with code 418`.
- **Nothing recorded:** open the site with the browser's network panel: `ts.js`, `config.js` and `collect` must answer 200/204. Check `origins` (hits from other origins are stored but flagged: see the dashboard's Status page).
- **The dashboard says it is off:** no user exists yet: `python3 -m tidesight.jobs user set NAME` (step 3).
- **Signing in fails:** check the name with `jobs.py user list`, and set a new password with `jobs.py user set NAME`. After 10 failures a client waits 10 minutes (`429`).
- **A property says "No data":** its tracking code is missing, or the site runs on a domain the property does not list: its Tracking code tab shows the refused origins, with a button to allow each.
- **`database is locked`:** the collector falls back to the spool automatically; if it happens often, use `journal_mode = "delete"` or `storage_mode = "spool"` per the Phase 0 test.
- **Stale data:** check `~/tidesight-data/cron.log` and `jobs.py status` (last enrichment time).
- **Which processes run:** `ps -u $USER -o pid,etime,rss,args | grep tidesight.fcgi` (about 25 MB each).
