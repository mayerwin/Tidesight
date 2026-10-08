// Core tracker: pageviews, identity, sessions, SPA routes, events, engagement, delivery, robustness.
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
  await page.route(API + '/collect', (route) => {
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

test('the single-file bundle works too', async ({ page, request }) => {
  await page.goto('/index.html?site=core&bundle=ts.all.js');
  const vid = await visitorId(page);
  await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 1);
});
