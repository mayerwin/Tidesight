// Core tracker: pageviews, identity, sessions, SPA routes, events, engagement, delivery, robustness.
import { readFileSync } from 'node:fs';
import { test, expect, API, sleep, visitorId, getHits, waitForHits, ofType, named, hidePage } from './helpers.mjs';

test('pageview carries page, context, identity and envelope fields', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  const pv = ofType(hits, 'pageview')[0];
  const d = pv.data;
  expect(pv.site_id).toBe('core');
  expect(pv.site_status).toBe('ok');
  expect(d.url).toBe('http://127.0.0.1:8788/index.html?site=core');
  expect(d.title).toBe('Home');
  expect(d.nav).toBe('navigate');
  expect(d.canon).toBe('http://127.0.0.1:8788/canonical-home');
  expect(d.lang).toBe('en');
  expect([d.ns, d.sn, d.pn, d.new, d.idsrc]).toEqual([1, 1, 1, 1, 'new']);
  expect(d.id).toMatch(/^[a-z0-9]{12}$/);
  expect(d.ctx).toMatchObject({ tz: expect.any(String), lang: expect.any(String), ck: 1, ls: 1, pwa: 'browser', ifr: 0 });
  for (const k of ['sw', 'sh', 'vw', 'vh', 'dpr', 'cd', 'hc', 'tzo']) expect(typeof d.ctx[k]).toBe('number');
  expect(d.ctx.uad && d.ctx.uad.platform).toBeTruthy();
  expect(d.ctx.uad.fullVersionList).toBeTruthy(); // high-entropy hints were awaited
  expect(pv.envelope).toMatchObject({ v: 1, site: 'core', vid, lib: expect.stringMatching(/^ts\.js\//), try: 0 });
  expect(pv.envelope.sid).toBe(pv.session_id);
  expect(pv.headers['user-agent']).toBeTruthy();
  expect(pv.headers.origin).toBe('http://127.0.0.1:8788');
  expect(pv.page_id).toBe(d.pid);
  const ses = await page.evaluate(() => localStorage.getItem('ts_ses'));
  expect(ses.split('.')[0]).toBe(pv.session_id);
});

test('identity and session continue on the next page', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await page.click('#to-page2-site');
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
  const [a, b] = ofType(hits, 'pageview');
  expect(b.session_id).toBe(a.session_id);
  expect(b.data).toMatchObject({ pn: 2, sn: 1, idsrc: 'cookie', title: 'Page 2', ref: 'http://127.0.0.1:8788/index.html?site=core' });
  expect(b.data.ns).toBeUndefined();
  expect(b.data.new).toBeUndefined();
  // Leaving the first page reported its engagement.
  expect(hits.some((h) => h.type === 'engagement' && h.page_id === a.page_id)).toBeTruthy();
});

test('cookie and localStorage restore each other', async ({ page, context, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await context.clearCookies();
  await page.reload();
  let hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
  expect(ofType(hits, 'pageview')[1].data.idsrc).toBe('ls');
  expect(await page.evaluate(() => document.cookie)).toContain('ts_vid=' + vid);
  await page.evaluate(() => { localStorage.removeItem('ts_vid'); });
  await page.reload();
  hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 3);
  expect(ofType(hits, 'pageview')[2].data.idsrc).toBe('cookie');
  expect(await page.evaluate(() => localStorage.getItem('ts_vid'))).toContain(vid);
});

test('identity conflict between cookie and localStorage keeps the oldest id', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  const older = 'older' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  await page.evaluate((id) => localStorage.setItem('ts_vid', id + '.1000'), older);
  await page.reload();
  const hits = await waitForHits(request, { vid: older }, (h) => ofType(h, 'pageview').length === 1);
  expect(ofType(hits, 'pageview')[0].data.idc).toEqual({ cookie: vid, ls: older });
  expect(await page.evaluate(() => document.cookie)).toContain('ts_vid=' + older + '.1000');
});

test('a new session starts after 30 minutes of inactivity', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  const first = ofType(await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1), 'pageview')[0];
  await page.evaluate(() => {
    const p = localStorage.getItem('ts_ses').split('.');
    p[2] = (Date.now() - 31 * 60000).toString(36);
    const v = p.join('.');
    localStorage.setItem('ts_ses', v);
    document.cookie = 'ts_ses=' + v + ';path=/';
  });
  await page.reload();
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
  const pv = ofType(hits, 'pageview')[1];
  expect(pv.session_id).not.toBe(first.session_id);
  expect(pv.data).toMatchObject({ sn: 2, pn: 1 });
  // The first activity after the gap (here the old page's last engagement report) opens the session.
  const opener = hits.find((h) => h.data.ns === 1 && h.session_id === pv.session_id);
  expect(opener).toBeTruthy();
});

test('a new session starts at midnight in the site time zone', async ({ page, request }) => {
  await page.clock.install({ time: new Date('2026-06-15T21:58:00Z') }); // 23:58 in Paris (CEST)
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  const first = ofType(await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1), 'pageview')[0];
  await page.clock.fastForward('03:00'); // 00:01 the next day, only 3 minutes later
  await page.click('#track');
  const hits = await waitForHits(request, { vid }, (h) => named(h, 'clicked_button').length === 1);
  const ev = named(hits, 'clicked_button')[0];
  expect(ev.session_id).not.toBe(first.session_id);
  expect(hits.some((h) => h.data.ns === 1 && h.session_id === ev.session_id)).toBeTruthy();
});

test('SPA History API navigation', async ({ page, request }) => {
  await page.goto('/spa/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await page.click('#go-about');
  let hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
  let pvs = ofType(hits, 'pageview');
  expect(pvs[1].data).toMatchObject({ nav: 'spa', title: 'About', pn: 2, url: 'http://127.0.0.1:8788/spa/about?site=core',
    ref: 'http://127.0.0.1:8788/spa/index.html?site=core' });
  expect(pvs[1].data.ctx).toBeTruthy();
  expect(hits.some((h) => h.type === 'engagement' && h.data.why === 'route' && h.page_id === pvs[0].page_id &&
    h.data.url === pvs[0].data.url)).toBeTruthy();
  await page.click('#tab2'); // replaceState on the same path: not a pageview
  await sleep(400);
  await page.click('#replace-other'); // replaceState to another path: a pageview
  hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 3);
  expect(ofType(hits, 'pageview')[2].data).toMatchObject({ url: 'http://127.0.0.1:8788/spa/other?site=core', title: 'Other',
    ref: 'http://127.0.0.1:8788/spa/about?site=core&tab=2' });
  await page.goBack(); // popstate
  hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 4);
  expect(ofType(hits, 'pageview')[3].data.url).toBe('http://127.0.0.1:8788/spa/index.html?site=core');
  await page.click('#redirect'); // pushState immediately followed by replaceState: one pageview
  hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 5);
  await sleep(500);
  pvs = ofType(await getHits(request, { vid }), 'pageview');
  expect(pvs.length).toBe(5);
  expect(pvs[4].data).toMatchObject({ url: 'http://127.0.0.1:8788/spa/b?site=core', title: 'B' });
});

test('hash routes count, plain anchors do not', async ({ page, request }) => {
  await page.goto('/hash.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await page.click('#h-anchor'); // a plain in-page anchor is not a route
  await sleep(400);
  expect(ofType(await getHits(request, { vid }), 'pageview').length).toBe(1);
  await page.click('#h-home');
  let hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
  expect(ofType(hits, 'pageview')[1].data).toMatchObject({ nav: 'hash', url: 'http://127.0.0.1:8788/hash.html?site=core#/home',
    ref: 'http://127.0.0.1:8788/hash.html?site=core#section' });
  await page.click('#h-about');
  hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 3);
  expect(ofType(hits, 'pageview')[2].data).toMatchObject({ nav: 'hash', ref: 'http://127.0.0.1:8788/hash.html?site=core#/home' });
});

test('custom events, the early command queue and identify', async ({ page, request }) => {
  await page.goto('/early.html?site=core');
  const vid = await visitorId(page);
  let hits = await waitForHits(request, { vid }, (h) => named(h, 'early_event').length === 1 && ofType(h, 'identify').length === 1);
  const pv = ofType(hits, 'pageview')[0];
  const early = named(hits, 'early_event')[0];
  expect(early.data.p).toEqual({ a: 1 });
  expect(early.page_id).toBe(pv.page_id);
  expect(ofType(hits, 'identify')[0].data.uid).toBe('early-user');
  await page.goto('/index.html?site=core');
  await page.click('#track');
  await page.click('#identify');
  hits = await waitForHits(request, { vid }, (h) => named(h, 'clicked_button').length === 1 && ofType(h, 'identify').length === 2);
  const ev = named(hits, 'clicked_button')[0];
  expect(ev.data).toMatchObject({ n: 'clicked_button', p: { color: 'blue', n: 3 }, val: 9.5 });
  expect(ev.user_id).toBe('early-user'); // queued before identify(): sent with the previous user id
  const later = ofType(hits, 'identify')[1];
  expect(later.data).toMatchObject({ uid: 'user-42', traits: { plan: 'pro' } });
  expect(later.envelope.uid).toBe('user-42');
});

test('engagement is counted while visible and reported on hide and by heartbeat', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'engagement').some((e) => e.data.why === 'hb'), 8000);
  const hb = ofType(hits, 'engagement').find((e) => e.data.why === 'hb');
  expect(hb.data.ms).toBeGreaterThanOrEqual(1000);
  await sleep(1200);
  await hidePage(page);
  const after = await waitForHits(request, { vid }, (h) => ofType(h, 'engagement').some((e) => e.data.why === 'hidden'));
  const eng = ofType(after, 'engagement');
  const hidden = eng.find((e) => e.data.why === 'hidden');
  expect(hidden.data.tot).toBeGreaterThan(hb.data.tot);
  expect(hidden.data.tot).toBeGreaterThanOrEqual(2200);
  // Reports are deltas that add up to the running total.
  expect(eng.reduce((sum, e) => sum + e.data.ms, 0)).toBe(hidden.data.tot);
});

test('engagement stops counting when the visitor is idle', async ({ page, request }) => {
  await page.clock.install();
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await page.clock.runFor(10 * 60 * 1000); // 10 minutes without any input
  await hidePage(page);
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'engagement').some((e) => e.data.why === 'hidden'));
  const tot = ofType(hits, 'engagement').find((e) => e.data.why === 'hidden').data.tot;
  expect(tot).toBeGreaterThanOrEqual(299000);
  expect(tot).toBeLessThanOrEqual(301000); // capped at idle_sec (300 s)
});

test('events are delivered when the page is left (beacon)', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await page.evaluate(() => tidesight('track', 'last_words'));
  await page.goto('about:blank'); // pagehide before the flush timer fires
  const hits = await waitForHits(request, { vid }, (h) => named(h, 'last_words').length === 1);
  expect(hits.some((h) => h.type === 'engagement' && ['leave', 'hidden'].includes(h.data.why))).toBeTruthy();
});

test('offline batches are kept and retried', async ({ page, context, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await context.setOffline(true);
  await page.evaluate(() => { tidesight('track', 'while_offline'); tidesight('flush'); });
  await expect.poll(() => page.evaluate(() => (JSON.parse(localStorage.getItem('ts_q') || '[]')).length)).toBe(1);
  await context.setOffline(false);
  const hits = await waitForHits(request, { vid }, (h) => named(h, 'while_offline').length === 1);
  expect(named(hits, 'while_offline')[0].envelope.try).toBeGreaterThanOrEqual(1);
  await expect.poll(() => page.evaluate(() => (JSON.parse(localStorage.getItem('ts_q') || '[]')).length)).toBe(0);
});

test('failed deliveries (server errors) are retried', async ({ page, request }) => {
  let failures = 0;
  await page.route(API + '/collect?*', (route) => {
    if (failures < 2) { failures++; return route.fulfill({ status: 503, body: '' }); }
    return route.continue();
  });
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length >= 1, 15000);
  expect(failures).toBe(2);
  expect(ofType(hits, 'pageview')[0].envelope.try).toBeGreaterThanOrEqual(1);
});

test('large batches are gzip-compressed', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await page.click('#many');
  const hits = await waitForHits(request, { vid }, (h) => named(h, 'bulk').length === 30);
  expect(named(hits, 'bulk').some((h) => h.body_encoding === 'gzip')).toBeTruthy();
});

test('the site configuration is cached between pages', async ({ page }) => {
  const configLoads = [];
  page.on('request', (r) => { if (r.url().includes('/config.js')) configLoads.push(r.url()); });
  await page.goto('/index.html?site=core');
  await page.waitForFunction(() => !!localStorage.getItem('ts_cfg_core'));
  await page.goto('/page2.html?site=core');
  await page.waitForFunction(() => !!localStorage.getItem('ts_vid'));
  await sleep(300);
  expect(configLoads.length).toBe(1);
});

test('pages flagged as 404 are marked', async ({ page, request }) => {
  await page.goto('/notfound.html?site=core');
  const vid = await visitorId(page);
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  expect(ofType(hits, 'pageview')[0].data.st).toBe(404);
});

test('excluded paths send nothing', async ({ page, request }) => {
  await page.goto('/excluded/page.html?site=core');
  const vid = await visitorId(page);
  await page.click('#track');
  await sleep(1800);
  expect(await getHits(request, { vid })).toEqual([]);
});

test('ignore opts a browser out', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await page.evaluate(() => tidesight('ignore'));
  await page.click('#track');
  await page.reload();
  await sleep(1800);
  const hits = await getHits(request, { vid });
  expect(hits.map((h) => h.type)).toEqual(['pageview']);
  expect(await page.evaluate(() => localStorage.getItem('ts_ignore'))).toBe('1');
});

test('Do Not Track is honoured when the site asks for it', async ({ page, request }) => {
  await page.addInitScript(() => Object.defineProperty(Navigator.prototype, 'doNotTrack', { get: () => '1' }));
  const posts = [];
  page.on('request', (r) => { if (r.url().startsWith(API + '/collect')) posts.push(r.url()); });
  await page.goto('/index.html?site=private');
  await page.waitForFunction(() => !!localStorage.getItem('ts_cfg_private'));
  await page.click('#track');
  await sleep(1800);
  expect(posts).toEqual([]);
});

test('the server cookie restores the visitor id', async ({ page, context, request }) => {
  await page.goto('/index.html?site=scookie');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await expect.poll(async () => (await context.cookies()).some((c) => c.name === 'ts_svid' && c.value.startsWith(vid))).toBeTruthy();
  await page.evaluate(() => { localStorage.removeItem('ts_vid'); document.cookie = 'ts_vid=; max-age=0; path=/'; });
  await page.reload();
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
  expect(ofType(hits, 'pageview')[1].data.idsrc).toBe('server');
});

test('a hostile environment never breaks the page', async ({ page }) => {
  await page.goto('/broken.html?site=core');
  await sleep(500);
  await page.click('#track');
  await expect.poll(() => page.evaluate(() => window.clicked === true)).toBeTruthy();
  expect(await page.evaluate(() => typeof window.tidesight === 'function' && !!window.tidesight.v)).toBeTruthy();
});

test('the API never throws on bad input', async ({ page }) => {
  await page.goto('/index.html?site=core');
  await visitorId(page);
  const threw = await page.evaluate(() => {
    try {
      const circ = {}; circ.me = circ;
      tidesight(); tidesight(null); tidesight('nope', 1); tidesight('track'); tidesight('track', '', {});
      tidesight('track', 'x', circ); tidesight('track', { not: 'a string' }, 'props?');
      tidesight.identify(); tidesight.identify({}); tidesight.search(); tidesight.page(42); tidesight.status();
      tidesight('_config', 'garbage'); tidesight('_plugin', 'x', 'not a function');
      return false;
    } catch (e) { return String(e); }
  });
  expect(threw).toBe(false);
});

test('batches stay under the collector size limit', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  // 30 events of about 4.6 KB: 140 KB in all, more than one request may carry (max_kb 64).
  await page.evaluate(() => { for (let i = 0; i < 30; i++) tidesight('track', 'big', { i, pad: 'x'.repeat(450), more: Array(10).fill('y'.repeat(400)) }); });
  const hits = await waitForHits(request, { vid }, (h) => named(h, 'big').length === 30);
  expect(new Set(named(hits, 'big').map((h) => h.data.p.i)).size).toBe(30); // none refused
  const perRequest = {};
  for (const h of named(hits, 'big')) perRequest[h.request_id] = (perRequest[h.request_id] || 0) + 1;
  expect(Object.keys(perRequest).length).toBeGreaterThanOrEqual(3);
  expect(Math.max(...Object.values(perRequest))).toBeLessThanOrEqual(13); // 13 × 4.6 KB < 59 KB
});

test('a page sending events in a loop is capped and the drop is reported', async ({ page, request }) => {
  await page.clock.install({ time: new Date('2026-06-15T10:00:05Z') });
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await page.evaluate(() => { for (let i = 0; i < 400; i++) tidesight('track', 'loop', { i }); });
  await waitForHits(request, { vid }, (h) => named(h, 'loop').length >= 290);
  await page.clock.fastForward('01:00'); // the next minute: events flow again
  await page.evaluate(() => tidesight('track', 'after'));
  const hits = await waitForHits(request, { vid }, (h) => named(h, 'after').length === 1, 15000);
  const loops = named(hits, 'loop').length;
  expect(loops).toBeLessThan(300); // 300 a minute, the pageview and plugin events included
  expect(loops).toBeGreaterThan(250);
  // The count rides on the next batch built after the drops (here, the end of the burst).
  expect(Math.max(...hits.map((h) => h.envelope.drop || 0))).toBeGreaterThanOrEqual(100);
});

test('properties are bounded and never end in half a character', async ({ page, request }) => {
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await page.evaluate(() => {
    const p = {};
    for (let i = 0; i < 60; i++) p['k' + i] = 'v';
    tidesight('track', 'many', p);
    tidesight('track', 'nested', { plan: 'pro', items: Array(200).fill({ item_name: 'z'.repeat(300) }) });
    tidesight('track', 'x'.repeat(199) + '\u{1F389}', { s: 'y'.repeat(499) + '\u{1F389}' });
  });
  const hits = await waitForHits(request, { vid }, (h) => named(h, 'many').length && named(h, 'nested').length && h.some((x) => x.name && x.name.startsWith('xxx')));
  expect(Object.keys(named(hits, 'many')[0].data.p).length).toBe(50);
  expect(named(hits, 'nested')[0].data.p).toEqual({ plan: 'pro', _trunc: 1 });
  const emoji = hits.find((x) => x.name && x.name.startsWith('xxx'));
  expect(emoji.name).toBe('x'.repeat(199)); // the emoji did not fit: dropped whole
  expect(emoji.data.p.s).toBe('y'.repeat(499));
});

test('secrets in URLs never leave the browser', async ({ page, request }) => {
  await page.goto('/index.html?site=core&utm_source=news&reset_token=S3CRET&key=K3Y#access_token=T0K&state=1');
  const vid = await visitorId(page);
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  const url = ofType(hits, 'pageview')[0].data.url;
  expect(url).toBe('http://127.0.0.1:8788/index.html?site=core&utm_source=news&reset_token=redacted&key=redacted#access_token=redacted&state=1');
  expect(JSON.stringify(hits)).not.toMatch(/S3CRET|K3Y|T0K/);
});

test('URL redaction follows the shared test vectors (the collector\'s redact.py)', async ({ page, request }) => {
  const v = JSON.parse(readFileSync(new URL('../../server/python/tests/redact_vectors.json', import.meta.url), 'utf8'));
  await page.goto('/index.html?site=core');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  // A plugin receives the core's API, which includes the redaction used for every URL the tracker sends.
  const groups = [v].concat(v.more || []);
  const out = await page.evaluate((groups) => new Promise((resolve) => {
    tidesight('_plugin', 'probe', (api) => resolve(groups.map((g) => g.cases.map((c) => api.redact(c[0], g.redact_params, g.redact_paths)))));
  }), groups);
  expect(out).toEqual(groups.map((g) => g.cases.map((c) => c[1])));
});

test('a hostile URL cannot freeze the page', async ({ page, request }) => {
  // 100 KB fragment: with patterns compiled to regular expressions, each event took seconds of main thread.
  await page.goto('/index.html?site=core#' + 'token'.repeat(20000) + '%0A=1');
  const vid = await visitorId(page);
  const pv = ofType(await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1), 'pageview')[0];
  expect(pv.data.url.length).toBeLessThanOrEqual(8000); // cut, so the event can still be sent
  expect(pv.data.url).toMatch(/^http:\/\/127\.0\.0\.1:8788\/index\.html\?site=core#tokentoken/);
  const ms = await page.evaluate(() => {
    const t = performance.now();
    for (let i = 0; i < 50; i++) tidesight('track', 'tick', { i });
    return performance.now() - t;
  });
  expect(ms).toBeLessThan(250);
  await waitForHits(request, { vid }, (h) => named(h, 'tick').length === 50);
});

test('markup on the page cannot redirect, load scripts into or break the tracker (DOM clobbering)', async ({ page, request }) => {
  const elsewhere = [];
  page.on('request', (r) => { if (r.url().startsWith('http://127.0.0.1:8799/') && !r.url().endsWith('/img.png')) elsewhere.push(r.url()); });
  await page.goto('/clobber.html?site=replay');
  const vid = await visitorId(page);
  const pv = ofType(await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1), 'pageview')[0];
  expect(pv.site_id).toBe('replay');
  expect(pv.data.title).toBe('Clobbered'); // document.title is shadowed by <img name="title">: read through its getter
  expect(pv.data.ref).toBe('');
  expect(await page.evaluate(() => typeof window.tidesight === 'function' && !!window.tidesight.v)).toBe(true);
  // The identity cookie was written: through the accessor, as <img name="cookie"> shadows document.cookie.
  expect(await page.evaluate(() => typeof document.cookie)).toBe('object');
  expect(await page.evaluate(() => Object.getOwnPropertyDescriptor(Document.prototype, 'cookie').get.call(document)))
    .toContain('ts_vid=' + vid);
  // The replay plugin loads rrweb from the collector, not from <img name="currentScript">, and records.
  await expect.poll(async () => (await (await request.get(API + '/__test/chunks?since=0')).json())
    .some((c) => c.visitor_id === vid && c.kind === 'replay'), { timeout: 10000 }).toBe(true);
  await page.evaluate(() => tidesight('track', 'still_works'));
  await waitForHits(request, { vid }, (h) => named(h, 'still_works').length === 1);
  expect(elsewhere).toEqual([]);
});

test('the single-file bundle copes with clobbering markup too', async ({ page, request }) => {
  await page.goto('/clobber.html?site=core&bundle=ts.all.js');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  expect(await page.evaluate(() => typeof window.tidesight === 'function' && !!window.tidesight.v)).toBe(true);
});

test('requests send only the origin as Referer, even when the page allows more', async ({ page, request }) => {
  await page.goto('/referrer.html?site=core&x=PRIVATE-123');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
  await page.evaluate(() => tidesight('track', 'last_words'));
  await page.goto('about:blank'); // the unload batch: a keepalive fetch, not a beacon
  const hits = await waitForHits(request, { vid }, (h) => named(h, 'last_words').length === 1);
  for (const h of hits) expect(h.headers.referer).toBe('http://127.0.0.1:8788/');
  expect(JSON.stringify(hits.map((h) => h.headers))).not.toContain('PRIVATE-123');
});

test('the single-file bundle works too', async ({ page, request }) => {
  await page.goto('/index.html?site=core&bundle=ts.all.js');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
});
