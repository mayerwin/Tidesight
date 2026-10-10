# Search engines: Google Search Console and Bing Webmaster Tools

A visitor arriving from Google's organic results brings no keyword with them: Google stopped passing it on years ago
(analytics tools show "(not provided)"). Only the search engines know what was searched. Tidesight reads their figures
from **Google Search Console** and **Bing Webmaster Tools** and shows them in the **Search engines** report:

- clicks, impressions, click-through rate and average position, per day, compared with any other period;
- per **query**, per **landing page**, per **country** and **device** (Google);
- the queries behind one page, and the pages one query led to (Google);
- next to each landing page, what Tidesight recorded of the visits that came from those searches: visits, bounce
  rate, engaged time and the conversions of a goal.

Both are free. Setting them up takes about ten minutes, once: each account serves every property.

## Google Search Console

Tidesight reads Search Console with a **service account**: a Google account made for a program, which you add as a
read-only user of your Search Console property. No consent screen, no app review, nothing to renew.

You need a Google account that can open your site in [Search Console](https://search.google.com/search-console)
(as the site's owner, or a user with full permission).

1. **Create a Google Cloud project.** Open [Google Cloud: new project](https://console.cloud.google.com/projectcreate),
   name it (e.g. *Tidesight*) and click **Create**. It is free: no billing account is needed.
2. **Turn on the Search Console API.** Open the
   [Google Search Console API](https://console.cloud.google.com/apis/library/searchconsole.googleapis.com) page, check
   that the new project is selected at the top, and click **Enable**.
3. **Create a service account.** Open
   [Create service account](https://console.cloud.google.com/iam-admin/serviceaccounts/create), name it *tidesight*,
   click **Create and continue**, skip the two optional steps (no role is needed), and click **Done**.
4. **Download its key.** Click the new service account, open its **Keys** tab, then **Add key → Create new key →
   JSON → Create**. Your browser downloads a `.json` file.
   *If Google says key creation is disabled by an organization policy* (Google Workspace accounts), create the project
   with a personal Google account instead, or ask your Workspace admin to allow keys for this project.
5. **Give the key file to Tidesight.** In the dashboard: **Properties → (your property) → Settings → Search
   engines → Google Search Console**. Choose the file (or paste its content) and click **Connect**. Tidesight checks it
   with Google and shows the service account's address, e.g. `tidesight@my-project.iam.gserviceaccount.com`.
6. **Let it read your site.** In [Search Console](https://search.google.com/search-console), choose your site, then
   **Settings → Users and permissions → Add user**. Paste the service account's address (the **Copy** button next to it
   in Tidesight), keep the permission **Restricted**, and click **Add**.
7. **Choose what the property reads.** Back in Tidesight, click **Refresh the list**, choose the Search Console
   property (Tidesight suggests the one matching the property's domain) and click **Save**.

The first import starts with the next scheduled job (every couple of minutes): the last days first, then the 16 months
of history Search Console keeps, within the hour. For your other properties, only steps 6 and 7 remain.

## Bing Webmaster Tools

1. **Add your site to Bing Webmaster Tools.** Sign in to [Bing Webmaster Tools](https://www.bing.com/webmasters) and
   add your site. Once Search Console is set up, **Import from Google Search Console** adds and verifies your sites in
   one click.
2. **Copy your API key.** Click the **settings** icon (the gear, top right), then **API access → API key**. Generate
   the key if there is none, and copy it.
3. **Give the key to Tidesight.** **Settings → Search engines → Bing Webmaster Tools**: paste the key and click
   **Connect**.
4. **Choose the site** this property reads, and click **Save**.

Bing's figures cover Bing's own results. DuckDuckGo and Yahoo show Bing's results, but Bing does not report the
searches made there.

## What to know about these figures

- **They arrive late.** Google's figures for a day come two to three days later, and change for a few more days:
  Tidesight fetches the recent days again until they settle. Its days are **Pacific Time** days. Tidesight asks Bing
  for its figures twice a day.
- **Some queries are hidden.** Google keeps rare queries to itself, for its users' privacy: the totals include their
  clicks, the list of queries does not. The report says which share of the clicks comes with its query.
- **Bing groups by week.** Bing gives its per-query and per-page figures by week (dated on the Saturday), its daily
  totals by day, and never pairs a query with a page.
- **Clicks are not visits.** A click is counted by the engine; a visit, by Tidesight, when the page loaded and the
  tracker ran (ad blockers, people leaving before the page loads, privacy settings). Seeing both side by side shows the
  difference.
- **Average position** is the average rank of the site's best result for each impression (1 is the top): lower is
  better. It also grows when the site starts appearing, far down, for new queries: often a sign of growth.
- **Filters** of the report bar apply to Tidesight's own visits only: the engines' figures cannot be split by them.

## Where things are kept

- The accounts: `search-engines.json` in the data directory (readable by this account only, backed up nightly with
  the properties and users). Keys are never shown again in the dashboard; **Disconnect** forgets one.
- The figures: `search.db` (`docs/schema.md`). It is backed up nightly: Google keeps only 16 months, so after that,
  Tidesight's copy is the only one. The **Export** dialog downloads them (*Search engine queries*), and so does
  `jobs.py export --what search`.
- A property names its Search Console property and its Bing site in its settings (`search_console`, `bing_site`:
  `docs/configuration.md`). Choosing another one replaces the figures of the previous one.

## From the command line

```
python -m tidesight.jobs --config tidesight.toml search status                  # accounts, links, imports
python -m tidesight.jobs --config tidesight.toml search connect google key.json
python -m tidesight.jobs --config tidesight.toml search connect bing API_KEY
python -m tidesight.jobs --config tidesight.toml search sites google            # what the account sees
python -m tidesight.jobs --config tidesight.toml search import [--site ID]      # now, rather than at the next cron run
python -m tidesight.jobs --config tidesight.toml search disconnect bing
```

## When something goes wrong

The property's **Search engines** settings show the last import and its error, if any. A refused import is tried again
an hour later, or at once when a setting there changes; a network or server error, ten minutes later.

| Message | What to do |
|---|---|
| *Enable the Google Search Console API…* | Step 2, in the key's project. It can take a few minutes to apply. |
| *User does not have sufficient permission for site…* | Step 6: add the service account to that Search Console property (and check the property chosen in step 7 is the one you added it to: `sc-domain:example.com` and `https://www.example.com/` are different properties). |
| *Google refused the key: invalid_grant…* | The key was deleted or disabled in Google Cloud: create a new one (step 4) and connect it again. |
| *this is an OAuth client's file, not a service account key* | The file came from **Credentials → OAuth client**: create the key on the service account's **Keys** tab (step 4). |
| *Bing refused: …InvalidApiKey* | Copy the key again from Bing Webmaster Tools (it changes when regenerated). |
| No Search Console property in the list | The service account has not been added yet (step 6), or was added to another property. Click **Refresh the list** after adding it. |
