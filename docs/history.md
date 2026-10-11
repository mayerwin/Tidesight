# Importing history from Google Analytics 4, Plausible or Matomo

Moving to Tidesight from another tool? Import its figures, and the reports go back as far as it did: **Property
settings → Import history**. Each import runs once, in the scheduled jobs, a little every few minutes; the list on
that page shows its progress.

## What is imported, and how the reports use it

Other tools keep **daily totals**, not visits. An import brings, for each day: visitors, visits, pageviews, bounces
and visit time, in total and per page, entry page, exit page (Plausible, Matomo), source, medium, campaign, channel,
referrer, country, region and city (GA4, Matomo), device, browser and operating system. Names are translated into
Tidesight's: sources and channels as Tidesight classifies them (GA4's default channel group), ISO country codes,
Tidesight's browser and system names.

- Imported days **fill the days before the property's first day of Tidesight data**. Days that both tools recorded
  count Tidesight's visits only: nothing is counted twice, and the switch leaves no gap.
- The overview, the trend charts, the reports of the dimensions above (Acquisition, Content, Locations, Technology,
  the Explorer), comparisons, exports and the map include them; a note under the chart says so.
- **Filters, segments and goals** apply to visits, which imported days do not have: a filtered report shows
  Tidesight's own data, and says so. So do the reports of other dimensions (page titles, languages, screens…) and of
  two dimensions at once.
- **Visitors** of a period are the sum of each day's visitors: someone who came on two days counts twice (each tool
  counts unique visitors per day in its export).
- Funnels, paths, retention, replays, heatmaps and visit timelines need visits: they start with Tidesight's data.
- Importing days that an earlier import covered **replaces** them. Deleting an import removes its figures (and the
  reports go back to what they were).

## Plausible

1. In Plausible, open the site's **Site settings → Imports & Exports** and click **Export to CSV**. Plausible
   prepares a ZIP and sends you a link by email (or offers it on the page): download it.
2. In Tidesight, **Import history → Plausible**: choose the ZIP (no need to unpack it) and click **Import**.

The dashboard takes files up to 16 MB. For a larger export, copy it to the server and run, from `app/server/python`:

```
python -m tidesight.jobs --config ../../tidesight.toml history plausible export.zip --site PROPERTY_ID
```

## Google Analytics 4

Tidesight reads Google Analytics with the Google service account of the search engines
([`docs/search-engines.md`](search-engines.md), steps 1 to 5). Then:

1. **Turn on the Analytics Data API**: open
   [Google Analytics Data API](https://console.cloud.google.com/apis/library/analyticsdata.googleapis.com) in the
   service account's Google Cloud project and click **Enable**.
2. **Let the service account read the property**: in [Google Analytics](https://analytics.google.com/), open
   **Admin → Property access management → + → Add users**, paste the service account's address (shown, with a Copy
   button, in Tidesight), choose the role **Viewer**, and click **Add**.
3. **Enter the property id** in Tidesight: the number in **Admin → Property details** (not the `G-…` measurement id),
   and click **Import**. Tidesight checks the access at once.

Universal Analytics (the version before GA4) cannot be imported: Google deleted its data in 2024.

## Matomo

1. **Create an auth token**: in Matomo, click the gear icon, then **Personal → Security → Auth tokens → Create new
   token**. Your user needs view access to the site.
2. **Find the site id**: **Administration → Websites → Manage**, the ID column.
3. In Tidesight, **Import history → Matomo**: enter Matomo's address, the site id and the token, and click **Import**.
   Tidesight checks them at once, uses the token for this import only, and forgets it when the import ends.

## Which days

By default an import takes every day the tool has, up to the day before Tidesight's first day for the property.
**Which days** (under each form) narrows it; so do `--from` and `--to` on the command line.

## From the command line

```
python -m tidesight.jobs --config tidesight.toml history list --site ID
python -m tidesight.jobs --config tidesight.toml history plausible export.zip --site ID [--from DAY] [--to DAY]
python -m tidesight.jobs --config tidesight.toml history ga4 PROPERTY_ID --site ID
python -m tidesight.jobs --config tidesight.toml history matomo https://matomo.example.com/ SITE_ID TOKEN --site ID
python -m tidesight.jobs --config tidesight.toml history delete JOB_ID --site ID
```

These start the import at once (for up to 10 minutes; the scheduled jobs carry on with the rest).

## Where it is kept

`history.db` in the data directory (`docs/schema.md`): the imported days, and the imports with their progress. It is
backed up nightly with the other databases that cannot be rebuilt. An uploaded export waits in `data/imports/` and is
deleted when its import ends.

## When something goes wrong

The import's line says why it failed. Network and server errors are retried ten minutes later; other errors stop the
import: fix the cause, then import again.

| Message | What to do |
|---|---|
| *Enable the Google Analytics Data API…* | Step 1 of Google Analytics 4. It can take a few minutes to apply. |
| *…Property access management: add … as a Viewer* | Step 2, on the property whose id you entered. |
| *the GA4 property id is a number…* | Admin → Property details: the number, not `G-…`. |
| *Matomo refused: …token…* | Create a new token, for a user with view access to the site. |
| *This is not Plausible's export* | Upload the ZIP Plausible sent, as it is. |
| *the export has no day between …* | The export covers only days Tidesight already has. |
