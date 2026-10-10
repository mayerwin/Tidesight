# Deploying on Dreamhost (shared hosting)

This guide installs Tidesight on a Dreamhost shared plan, on a subdomain such as `stats.example.com`, in one folder: the subdomain's web folder holds the code, the configuration and the data. The collector, dashboard and API run as a FastCGI program, and cron runs the jobs. The same steps work on most shared hosts running Apache with FastCGI (mod_fcgid) or CGI.

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

## One folder for everything

Layout used below (replace `USER` and the domain names):

```
~/stats.example.com/              the subdomain's web folder, and Tidesight's
    .htaccess                     every request goes to the app: no file of this folder is ever handed out
    tidesight.fcgi                the entry point (a copy of app/server/python/tidesight.fcgi)
    robots.txt                    keeps search engines out (Dreamhost serves its own when there is none)
    tidesight.toml                the configuration
    app/                          the code (git clone)
    data/                         databases, users, backups, geo databases, log (with an .htaccess of its own)
```

The databases hold raw IP addresses and the users' password hashes, so they must never be downloadable. As with WordPress, `.htaccess` rules keep them private, and Tidesight also checks that the rules work:

1. **Everything goes to the app.** The folder's `.htaccess` sends every request, for files that exist too, to `tidesight.fcgi`, which serves only its own public files (the tracker scripts, the dashboard's pages), and its rules apply in every subfolder before any `.htaccess` of their own. `https://stats.example.com/tidesight.toml` reaches the app, which answers `404`.
2. **The data folder refuses everything.** Tidesight writes `data/.htaccess` (`Require all denied`), in case the main `.htaccess` is ever changed.
3. **Files for your account only.** Every file Tidesight writes is readable by your account alone, and the setup below makes `app/`, `data/` and `tidesight.toml` so; on Dreamhost the web server reads files as another user, so it could not hand them out anyway.
4. **Checked from the outside.** Every 10 minutes the cron job makes sure `public_url` leads to this installation, then puts a file with random content in each of its folders (`data/`, `app/`, the web folder itself) and asks the website for it, as anyone could. If one comes back, the collector and the dashboard stop (`503`, logged, and said in the dashboard) until none does. The dashboard's Status page and `jobs.py status` show the last check, or why it could not conclude.

WordPress keeps its database in MySQL and its secrets in a PHP file that runs rather than downloads; Tidesight's data are plain files, so the protection is verified rather than assumed. If you would rather keep Tidesight's files out of the web folder altogether, that works too: the code in `~/tidesight` with `tidesight.toml` beside it, `data_dir` outside the code's folder (`"~/tidesight-data"`, so that updates can replace the code), and only `tidesight.fcgi` and the `.htaccess` in the web folder: `tidesight.fcgi` finds the code in `~/tidesight/server/python` and the configuration in `~/tidesight/tidesight.toml` when there is none beside it.

## 1. Prepare the account

1. In the panel, make sure your user is a **shell user** (SSH access).
2. Add the subdomain `stats.example.com` as a hosted website. Its web folder is `~/stats.example.com`.
3. **If the domain's DNS is managed elsewhere** (Cloudflare, a registrar), add an `A` record for the subdomain there, pointing at the address Dreamhost gives it (`dig +short stats.example.com @ns1.dreamhost.com` prints it; add an `AAAA` record too if `dig AAAA` shows one). On Cloudflare, make it **DNS only** (grey cloud), at least until the certificate is issued.
4. Turn on HTTPS for the subdomain once its DNS points at Dreamhost: Manage Websites → the subdomain's menu → **SSL Security** → Add → the free Let's Encrypt certificate (it works within 10 to 30 minutes). Tracking from an `https` site needs an `https` collector. Dreamhost answers the certificate checks (`/.well-known/acme-challenge/`) itself, so Tidesight's `.htaccess` does not get in their way.
5. **Turn off the Web Application Firewall** for the subdomain (formerly called Extra Web Security): Websites → Manage Websites → **Manage** on the subdomain → **Security** tab → Website Security → switch off **Web Application Firewall** → Save Changes. It is ModSecurity with the OWASP rules, set per website (other sites keep theirs), and cannot be adjusted from `.htaccess`. It takes the tracker's compressed batches (every batch of 1 KB or more) and session-replay chunks for attacks and refuses them (`418`, or a `500` error page), so most hits would be lost; even uncompressed, a page title or an error message that looks like SQL or a script would be refused now and then. What it guards against (attacks on PHP applications) does not apply: Tidesight runs no PHP, uses parameterised SQL only, and checks and bounds everything it receives itself. Step 4 below shows how to check.

**Behind Cloudflare's proxy** (orange cloud, optional): use the SSL mode "Full (strict)" (the Let's Encrypt certificate stays on Dreamhost), and keep the two `rate_limit_*` lines of step 3: the visitor's address is then taken from Cloudflare's header, believed only when the request comes from one of Cloudflare's addresses, so the same lines are right with and without the proxy. Geolocation follows the same rule by itself (`[enrich] ip_header = "auto"`).

## 2. Get the code and check the host (Phase 0)

```sh
ssh USER@example.com
cd ~/stats.example.com
curl -L https://github.com/mayerwin/Tidesight/releases/latest/download/tidesight.tar.gz | tar xz   # unpacks app/
python3 --version
python3 app/tools/sqlite_host_test.py --data-dir ~/tidesight-hosttest && rm -rf ~/tidesight-hosttest
chmod 700 app   # the code is for the app, not for the web server
```

`app/` holds the latest release; the next ones are installed from the dashboard (step 9). A `git clone https://github.com/mayerwin/Tidesight.git app` works too, and is then updated with `git pull`.

The test ends with `OVERALL` and a `RECOMMENDATION` line such as `storage_mode = "sqlite", journal_mode = "wal"`. Use that in the configuration. If it says `spool`, set `storage_mode = "spool"` (cron then loads the spool into SQLite). If `python3` is older than 3.9, install a newer Python with pyenv and use it below (step 5).

## 3. Configure

```sh
cd ~/stats.example.com
cp app/config/tidesight.example.toml tidesight.toml
chmod 600 tidesight.toml
mkdir -p data && chmod 700 data   # only your account may read the data
```

Tidesight creates its files readable by your account only. Files left by older versions may be more open: `chmod -R go-rwx data` once fixes them.

Edit at least:

```toml
[server]
data_dir = "data"                          # relative to this file: ~/stats.example.com/data
public_url = "https://stats.example.com/"  # also what the outside check asks
storage_mode = "sqlite"                    # from the Phase 0 recommendation
journal_mode = "wal"                       # idem
log_file = "data/tidesight.log"            # see below
rate_limit_key = "cf-connecting-ip"        # only if the domain is on Cloudflare (step 1)
rate_limit_proxies = ["cloudflare"]
```

`log_file` matters under FastCGI: Dreamhost does not put a FastCGI program's own messages in the site's error log, so without it Tidesight's errors are nowhere to be seen. The secret used for hashing is generated in the data directory on first use (`[server] secret` overrides it). People and properties are added in the dashboard (steps 4, 7 and 8).

Properties can also be defined in the file, where they stay read-only in the dashboard:

```toml
[sites.example]
name = "Example"
origins = ["https://example.com", "https://www.example.com"]
timezone = "Europe/Paris"
```

Check the configuration: `cd ~/stats.example.com/app/server/python && python3 -m tidesight.jobs status` (it finds `tidesight.toml` beside `app/`).

## 4. Install the web entry point

```sh
cd ~/stats.example.com
cp app/server/python/tidesight.fcgi .
cp app/server/python/web.htaccess .htaccess
chmod 755 tidesight.fcgi
printf 'User-agent: *\nDisallow: /\n' > robots.txt
```

The `.htaccess`:

```apache
Options -Indexes -MultiViews +ExecCGI
DirectoryIndex disabled
CGIPassAuth On
RewriteEngine On
# /.well-known/ belongs to the web server (certificate checks, security.txt).
RewriteRule ^\.well-known/ - [L]
# The dashboard and share pages only over HTTPS: passwords and session cookies never travel in clear.
RewriteCond %{HTTPS} !=on
RewriteRule ^(dashboard|share)(/.*)?$ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]
# Everything else is the app's.
RewriteRule ^tidesight\.fcgi(/|$) - [L]
RewriteRule ^(.*)$ tidesight.fcgi/$1 [L,QSA]
```

The tracker's addresses answer over HTTP too (pages served over HTTP can still be measured); the dashboard is sent to HTTPS, so it opens only once the certificate exists, and Tidesight refuses any password that comes over plain HTTP. Behind Cloudflare's proxy, the SSL mode must be "Full (strict)": with "Flexible", Cloudflare would reach Dreamhost over HTTP and the redirect would loop. Dreamhost runs `.fcgi` files with FastCGI as they are. On another Apache host with mod_fcgid, add `AddHandler fcgid-script .fcgi`, but only where mod_fcgid is installed: without it, Apache would send the file instead of running it. `CGIPassAuth On` lets HTTP Basic credentials reach the app, for scripts using the API (the dashboard signs in with a cookie).

Open `https://stats.example.com/health`: it must answer `{"status": "ok", …}`. The dashboard is at `https://stats.example.com/dashboard/`.

**Your account.** The first time, the dashboard offers to create the first account: a user name, a password, and the setup code that Tidesight wrote on the server when the page first opened:

```sh
cat ~/stats.example.com/data/setup-code.txt
```

Or run `cd ~/stats.example.com/app/server/python && python3 -m tidesight.jobs setup-link`: it prints the dashboard's address with the code filled in. The code proves the installation is yours. A form that let the first visitor create the account would not: every new HTTPS certificate is published within minutes (Certificate Transparency), and bots try the setup pages of the sites they find before their owners do. The code (16 characters, 80 random bits) is readable by your account only, attempts are limited like passwords, and it is deleted once the account exists.

Then check that the web application firewall lets compressed data through (step 1.5). `200` is right; `418` or `500` means the firewall is still on:

```sh
gzip -c ~/stats.example.com/app/tracker/build/ts.js | curl -s -o /dev/null -w '%{http_code}\n' \
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
# Load the spool (if any), enrich new hits, update rollups, check that the data cannot be downloaded: every 2 minutes.
*/2 * * * * cd ~/stats.example.com/app/server/python && python3 -m tidesight.jobs -q --config ~/stats.example.com/tidesight.toml cron >> ~/stats.example.com/data/cron.log 2>&1
# Nightly backup, 14 kept.
15 3 * * * cd ~/stats.example.com/app/server/python && python3 -m tidesight.jobs -q --config ~/stats.example.com/tidesight.toml backup --keep 14 >> ~/stats.example.com/data/cron.log 2>&1
# Monthly geo database refresh (DB-IP Lite, plus MaxMind GeoLite2 when a licence key is configured).
30 4 2 * * cd ~/stats.example.com/app/server/python && python3 -m tidesight.jobs -q --config ~/stats.example.com/tidesight.toml geo-update >> ~/stats.example.com/data/cron.log 2>&1
# Weekly: optimise the databases.
45 4 * * 0 cd ~/stats.example.com/app/server/python && python3 -m tidesight.jobs -q --config ~/stats.example.com/tidesight.toml vacuum >> ~/stats.example.com/data/cron.log 2>&1
```

Run `geo-update` once by hand right away so the first enrichment has locations. Geolocation by DB-IP (CC BY 4.0) requires the attribution the dashboard shows. For MaxMind, create a free GeoLite2 account and set `maxmind_account_id` and `maxmind_license_key` in `[enrich]`.

The backup keeps the raw data (`events.db`), the dashboard's own data (`web.db`: saved segments, notes) and the settings of the data directory (`properties.json`, `users.json`); `--with-replay` and `--with-derived` add the other databases.

## 7. People

Admins manage people on the dashboard's **Users** page; viewers read every report.

- **Invite someone:** Users → Invite someone, with a user name and a role. Tidesight sends no email: it gives a link to pass on (by email, chat…). Whoever opens it chooses the password and is signed in; the link works once, for a week.
- **Forgotten password:** an admin opens the person's actions on the Users page → Password link, and passes the link on. It works once, for a day; the current password keeps working until then, and using it signs the person out everywhere else.
- **Your own password:** your menu (bottom left) → Change password.
- **Roles and removal:** on the Users page. Nobody changes their own role or removes themselves (another admin does), and Tidesight always keeps an admin who can sign in.
- **Locked out** (no admin can sign in any more): on the server, `cd ~/stats.example.com/app/server/python && python3 -m tidesight.jobs user link NAME` prints a password link for NAME. Whoever can run it controls the server anyway.

Why links and no email? No mail server to configure, no address stored, nothing a stranger can trigger: a password reset form that mails links can be used to flood people, and its emails are the first thing phishing imitates. People can also be written in the configuration file (`[dashboard] users`, hashes from `jobs.py hash-password`): they are listed on the Users page and changed in the file, and `[dashboard] admins` makes the named users admins.

## 8. Add the tracker to your site

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

## 9. Updating

Once a day the cron job asks GitHub for Tidesight's latest release. When a newer one exists, admins see **Update** next to the version (bottom left) and on the Status page, with what is new; **Update to …** asks for it, and the next cron run (within a few minutes) installs it: it downloads the release from GitHub over HTTPS, checks it (the version it announces, Python that compiles), puts it in place of `app/` and keeps the old code as `app.previous`. The copies of `tidesight.fcgi` and `.htaccess` in the web folder follow, unless they were edited (the Status page then says so). Nothing else is needed: within seconds, each running FastCGI process notices the new code and makes way for a fresh one (after the request it is answering), database migrations run automatically, and when an update changes how data is derived, the next enrichment rebuilds `derived.db` in the background and swaps it in. The sites' tracking code never changes: `ts.js` comes from this host, so the tracker follows (browsers keep it an hour at most).

From the server instead (same steps, at once):

```sh
cd ~/stats.example.com/app/server/python
python3 -m tidesight.jobs update --check        # what is available
python3 -m tidesight.jobs update --install      # install the latest release now
python3 -m tidesight.jobs update --rollback     # back to the previous code (app.previous)
```

A `git clone` is updated with `cd ~/stats.example.com/app && git pull` instead (the dashboard then only tells you a release exists). `touch ~/stats.example.com/tidesight.fcgi` restarts the processes on demand.

## CGI (hosts without FastCGI)

Copy `server/python/collect.cgi` into the web folder instead of `tidesight.fcgi`, `chmod 755` it, and use the same `.htaccess` with `collect.cgi` in place of `tidesight.fcgi`, plus `AddHandler cgi-script .cgi`. A folder of an existing site works too (`~/example.com/tidesight/`, with `app/`, `data/` and `tidesight.toml` inside): the addresses then gain its name, the tracker is `https://example.com/tidesight/ts.js` and the dashboard `https://example.com/tidesight/dashboard/` (set `public_url = "https://example.com/tidesight/"`).

`CGIPassAuth On` needs Apache 2.4.13 or later; on older Apache use `RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]` instead. CGI starts a process per request: fine for small sites.

## Passenger (other hosts)

Where Passenger still runs Python apps, copy `server/python/passenger_wsgi.py` into the app root (`~/stats.example.com/`): it finds the code and the configuration as `tidesight.fcgi` does. After an update, `touch ~/stats.example.com/tmp/restart.txt`. To use another interpreter, write its path in `~/stats.example.com/.tidesight-python`. Passenger serves the files of `public/` directly, so keep `app/`, `data/` and `tidesight.toml` outside `public/`.

## Troubleshooting

- **`/health` fails:** read `~/stats.example.com/data/tidesight.log` (with `log_file` set) and the site's error log (`~/logs/stats.example.com/https/error.log`; `http/` for plain HTTP). `python3 -m tidesight.jobs status` checks the configuration.
- **"Tidesight has stopped: its files could be downloaded":** the outside check fetched a file of one of Tidesight's folders from the website. Check that the folder's `.htaccess` is `web.htaccess` (step 4) and that `data/.htaccess` exists; the next cron run checks again and Tidesight goes on by itself once the file is out of reach. The Status page and `jobs.py status` show what was asked (`web_check`).
- **The Status page says the folder is "Not verified":** the check could not conclude, and says why: no `public_url`, the website unreachable from the server, or `public_url` leading somewhere else than this installation.
- **Tracker requests fail with `418` or `500` while `/health` works:** the Web Application Firewall is on for the subdomain (step 1.5); the error log then shows `ModSecurity: Access denied with code 418`.
- **Nothing recorded:** open the site with the browser's network panel: `ts.js`, `config.js` and `collect` must answer 200/204. Check `origins` (hits from other origins are stored but flagged: see the dashboard's Status page).
- **The dashboard asks for a setup code:** no user exists yet; the code is in `data/setup-code.txt` (step 4).
- **Signing in fails:** an admin makes a password link on the Users page; without any admin, `jobs.py user link NAME` on the server (step 7). After 10 failures a client waits 10 minutes (`429`).
- **A property says "No data":** its tracking code is missing, or the site runs on a domain the property does not list: its Tracking code tab shows the refused origins, with a button to allow each.
- **`database is locked`:** the collector falls back to the spool automatically; if it happens often, use `journal_mode = "delete"` or `storage_mode = "spool"` per the Phase 0 test.
- **Stale data:** check `data/cron.log` and `jobs.py status` (last enrichment time).
- **Which processes run:** `ps -u $USER -o pid,etime,rss,args | grep tidesight.fcgi` (about 25 MB each).
