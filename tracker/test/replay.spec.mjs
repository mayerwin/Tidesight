// Heavy data sent to /chunk: mouse movement, session replay (rrweb) and DOM snapshots.
import { test, expect, sleep, visitorId, getHits, waitForHits, ofType, hidePage } from './helpers.mjs';
import { pluginsReady, getChunks, waitForChunks, captureChunks, chunkBodies } from './plugin-helpers.mjs';

async function open(page, request, url, ...plugins) {
  await page.goto(url);
  const vid = await visitorId(page);
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length >= 1);
  if (plugins.length) await pluginsReady(page, ...plugins);
  return { vid, pid: ofType(hits, 'pageview')[0].page_id };
}

test.describe('mouse movement', () => {
  test('pointer positions are sampled and sent as chunks, per page', async ({ page, request }) => {
    await captureChunks(page);
    const { vid, pid } = await open(page, request, '/plugins.html?site=test', 'mouse');
    await page.mouse.move(20, 20);
    await page.mouse.move(400, 300, { steps: 20 });
    await sleep(120);
    await page.mouse.move(600, 100, { steps: 10 });
    await sleep(120);
    await hidePage(page); // sent at once, uncompressed, by sendBeacon
    const rows = await waitForChunks(request, vid, (c) => c.some((x) => x.kind === 'mouse'));
    const mouse = rows.filter((c) => c.kind === 'mouse');
    expect(mouse.map((c) => [c.seq, c.page_id])).toEqual([[0, pid]]);
    expect(mouse[0].meta).toMatchObject({ site: 'test', k: 'mouse', sent_encoding: 'identity' });
    const body = (await chunkBodies(page)).filter((b) => b.k === 'mouse');
    expect(body.length).toBe(1);
    expect(body[0].how).toBe('beacon');
    const data = JSON.parse(body[0].text);
    expect(data).toMatchObject({ pw: expect.any(Number), ph: expect.any(Number), vw: 1280, vh: 720 });
    expect(data.pts.length).toBeGreaterThanOrEqual(3);
    let prev = -1;
    for (const [x, y, dt] of data.pts) {
      expect(x).toBeGreaterThanOrEqual(0);
      expect(y).toBeGreaterThanOrEqual(0);
      expect(dt).toBeGreaterThanOrEqual(prev); // ms since the chunk's ts
      prev = dt;
    }
    expect(data.pts[0][2]).toBe(0);
    expect(data.pts[data.pts.length - 1].slice(0, 2)).toEqual([600, 100]); // the resting position is kept
    // Sampled at most every interval_ms (50 ms on this site), however many move events there were.
    expect(data.pts.length).toBeLessThan(31);
    expect(body[0].ts).toBe(mouse[0].client_ts);
  });

  test('a SPA route ends the page\'s chunks; the next page starts again at seq 0', async ({ page, request }) => {
    await captureChunks(page);
    const { vid, pid } = await open(page, request, '/spa/index.html?site=test', 'mouse');
    await page.mouse.move(10, 200);
    await page.mouse.move(300, 220, { steps: 8 });
    await sleep(100);
    await page.click('#go-about');
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
    const pid2 = ofType(hits, 'pageview')[1].page_id;
    await page.mouse.move(500, 300, { steps: 8 });
    await sleep(100);
    await hidePage(page);
    const rows = await waitForChunks(request, vid, (c) => c.filter((x) => x.kind === 'mouse').length === 2);
    expect(rows.filter((c) => c.kind === 'mouse').map((c) => [c.page_id, c.seq])).toEqual([[pid, 0], [pid2, 0]]);
  });
});

test.describe('session replay', () => {
  test('rrweb chunks: one recording, ordered, compressed, masked', async ({ page, request }) => {
    const SECRET = 'hunter2-Secret';
    await captureChunks(page);
    const files = [];
    page.on('request', (r) => { if (r.url().includes('/ts/')) files.push(new URL(r.url()).pathname); });
    const { vid, pid } = await open(page, request, '/replay.html?site=replay', 'replay');
    // The first chunk (meta + full snapshot) goes out about a second after recording starts, gzip-compressed.
    let rows = await waitForChunks(request, vid, (c) => c.some((x) => x.kind === 'replay'));
    await page.click('#user');
    await page.keyboard.type('alice');
    await page.click('#pw');
    await page.keyboard.type(SECRET);
    await page.click('#add');
    await page.mouse.move(200, 200, { steps: 5 });
    await sleep(300);
    await hidePage(page); // the rest at once, uncompressed
    rows = await waitForChunks(request, vid, (c) => c.filter((x) => x.kind === 'replay').length >= 2);
    await sleep(500);
    rows = (await getChunks(request, vid)).filter((c) => c.kind === 'replay');
    expect(files).toContain('/ts/rrweb-record.js');
    const rec = rows[0].meta.rec;
    expect(rec).toMatch(/^[a-z0-9]{12}$/);
    expect(rows.map((c) => c.seq)).toEqual(rows.map((c, i) => i));
    for (const c of rows) {
      expect(c.meta.rec).toBe(rec);
      expect(c.page_id).toBe(pid);
      expect(c.session_id).toBeTruthy();
    }
    expect(rows[0].meta.sent_encoding).toBe('gzip');
    expect(rows[rows.length - 1].meta.sent_encoding).toBe('identity');

    const bodies = (await chunkBodies(page)).filter((b) => b.k === 'replay');
    expect(bodies.map((b) => b.seq)).toEqual(rows.map((c) => c.seq));
    const events = [];
    for (const b of bodies) {
      const evs = JSON.parse(b.text);
      expect(Array.isArray(evs)).toBe(true);
      expect(b.ts).toBe(evs[0].timestamp); // ts = the first event's time
      events.push(...evs);
    }
    expect(events[0].type).toBe(4); // Meta
    expect(events[0].data.href).toContain('/replay.html');
    expect(events[1].type).toBe(2); // FullSnapshot
    expect(events.some((e) => e.type === 3)).toBe(true); // incremental: input, mutation, mouse
    for (let i = 1; i < events.length; i++) expect(events[i].timestamp).toBeGreaterThanOrEqual(events[i - 1].timestamp - 50);
    const all = bodies.map((b) => b.text).join('\n');
    expect(all).not.toContain(SECRET); // passwords always masked
    expect(all).not.toContain('alice'); // all inputs masked by default
    expect(all).not.toContain('account 4242'); // .ts-mask text
    expect(all).not.toContain('Blocked widget'); // .ts-block element
    expect(all).toContain('Session replay'); // ordinary text is recorded
  });

  test('a SPA route continues the recording and marks the new page', async ({ page, request }) => {
    await captureChunks(page);
    const { vid } = await open(page, request, '/spa/index.html?site=replay', 'replay');
    await waitForChunks(request, vid, (c) => c.some((x) => x.kind === 'replay'));
    await page.click('#go-about');
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
    const pid2 = ofType(hits, 'pageview')[1].page_id;
    await sleep(200);
    await hidePage(page);
    await waitForChunks(request, vid, (c) => c.filter((x) => x.kind === 'replay').length >= 2);
    const bodies = (await chunkBodies(page)).filter((b) => b.k === 'replay');
    expect(new Set(bodies.map((b) => b.rec)).size).toBe(1);
    expect(bodies.map((b) => b.seq)).toEqual(bodies.map((b, i) => i));
    const events = bodies.flatMap((b) => JSON.parse(b.text));
    const mark = events.find((e) => e.type === 5 && e.data.tag === 'pageview');
    expect(mark.data.payload).toEqual({ pid: pid2, url: 'http://127.0.0.1:8788/spa/about?site=replay' });
    expect(events.filter((e) => e.type === 2).length).toBe(1); // still one recording, no new full snapshot
  });

  test('rrweb loads as a global even on a page with an AMD loader', async ({ page, request }) => {
    await page.addInitScript(() => {
      window.define = function (name) { window.__amdDefined = name; };
      window.define.amd = {};
    });
    const { vid } = await open(page, request, '/replay.html?site=replay', 'replay');
    await waitForChunks(request, vid, (c) => c.some((x) => x.kind === 'replay'));
    expect(await page.evaluate(() => [typeof window.rrwebRecord, window.__amdDefined])).toEqual(['object', undefined]);
  });

  test('a recording stops after max_minutes', async ({ page, request }) => {
    await page.clock.install(); // (its fake `performance` hides resource timing: wait for the first chunk instead)
    const { vid } = await open(page, request, '/replay.html?site=replay');
    await waitForChunks(request, vid, (c) => c.some((x) => x.kind === 'replay'));
    await page.clock.runFor('30:05'); // replay.max_minutes = 30
    await sleep(300);
    const before = (await getChunks(request, vid)).filter((c) => c.kind === 'replay').length;
    await page.click('#add'); // a DOM change and a click: nothing more is recorded
    await page.click('#add');
    await page.clock.runFor(15000);
    await hidePage(page);
    await sleep(1000);
    expect((await getChunks(request, vid)).filter((c) => c.kind === 'replay').length).toBe(before);
  });

  test('nothing is recorded on excluded paths', async ({ page, request }) => {
    const files = [];
    page.on('request', (r) => { if (r.url().includes('/ts/')) files.push(r.url()); });
    await page.goto('/excluded/page.html?site=replay');
    const vid = await visitorId(page);
    await page.click('#track');
    await sleep(2500);
    await hidePage(page);
    await sleep(800);
    expect(await getChunks(request, vid)).toEqual([]);
    expect(await getHits(request, { vid })).toEqual([]);
    expect(files.some((f) => f.includes('rrweb-record'))).toBe(false);
  });
});

test.describe('DOM snapshots', () => {
  test('one snapshot per page per session', async ({ page, request }) => {
    await captureChunks(page);
    const { vid, pid } = await open(page, request, '/plugins.html?site=snap', 'replay');
    let rows = await waitForChunks(request, vid, (c) => c.some((x) => x.kind === 'snapshot'));
    const snap = rows.find((c) => c.kind === 'snapshot');
    expect(snap).toMatchObject({ seq: 0, page_id: pid });
    expect(rows.some((c) => c.kind === 'replay')).toBe(false); // replay is off on this site
    const body = (await chunkBodies(page)).find((b) => b.k === 'snapshot');
    const evs = JSON.parse(body.text);
    expect(evs.map((e) => e.type)).toEqual([4, 2]); // Meta (href, width, height), FullSnapshot
    expect(evs[0].data).toMatchObject({ href: expect.stringContaining('/plugins.html'), width: 1280, height: 720 });
    expect(body.text).toContain('Behaviour plugins');
    expect(body.text).not.toContain('hidden-token-value'); // input values are masked
    // The same page again in this session: no new snapshot. Another page: one.
    await page.reload();
    await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
    await sleep(3000);
    expect((await getChunks(request, vid)).filter((c) => c.kind === 'snapshot').length).toBe(1);
    await page.goto('/page2.html?site=snap');
    rows = await waitForChunks(request, vid, (c) => c.filter((x) => x.kind === 'snapshot').length === 2);
  });

  test('taken from the running recording when replay is on', async ({ page, request }) => {
    await captureChunks(page);
    const { vid, pid } = await open(page, request, '/replay.html?site=replaysnap', 'replay');
    await waitForChunks(request, vid, (c) => c.some((x) => x.kind === 'snapshot') && c.some((x) => x.kind === 'replay'));
    await sleep(1500); // the replay's next chunk holds the extra full snapshot
    await hidePage(page);
    await sleep(800);
    const rows = await getChunks(request, vid);
    expect(rows.filter((c) => c.kind === 'snapshot').map((c) => c.page_id)).toEqual([pid]);
    const bodies = await chunkBodies(page);
    const snap = JSON.parse(bodies.find((b) => b.k === 'snapshot').text);
    expect(snap.map((e) => e.type)).toEqual([4, 2]);
    const replay = bodies.filter((b) => b.k === 'replay').flatMap((b) => JSON.parse(b.text));
    expect(replay.filter((e) => e.type === 2).length).toBe(2); // the recording's own, plus the snapshot's
    expect(new Set(bodies.filter((b) => b.k === 'replay').map((b) => b.rec)).size).toBe(1);
  });
});
