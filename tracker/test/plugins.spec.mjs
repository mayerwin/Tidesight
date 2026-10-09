// Behaviour plugins: fingerprint and bot signals, clicks and links, rage/dead/error clicks, scroll, forms,
// Web Vitals and timing, errors, window signals, ad-blocker detection, sampling and the single-file bundle.
import { test, expect, sleep, visitorId, getHits, waitForHits, ofType, hidePage } from './helpers.mjs';
import { pluginsReady } from './plugin-helpers.mjs';

const COMMON = ['t', 'id', 'ts', 'pid', 'url', 'sid', 'vid', 'ns'];
const own = (d) => Object.keys(d).filter((k) => !COMMON.includes(k)).sort();
const pageviewId = (hits) => ofType(hits, 'pageview')[0].page_id;

// Open a fixture page and wait until its pageview is stored and the named plugins have run.
async function open(page, request, url, ...plugins) {
  await page.goto(url);
  const vid = await visitorId(page);
  const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length >= 1);
  if (plugins.length) await pluginsReady(page, ...plugins);
  return { vid, pid: pageviewId(hits) };
}

test.describe('fingerprint', () => {
  test('device fingerprint and automation signals, sent once per session and again when they change', async ({ page, request }) => {
    const { vid, pid } = await open(page, request, '/plugins.html?site=test');
    let hits = await waitForHits(request, { vid }, (h) => ofType(h, 'fp').length === 1);
    const fp = ofType(hits, 'fp')[0], d = fp.data;
    expect(fp.page_id).toBe(pid);
    expect(d.fv).toBe(1);
    expect(d.h).toMatch(/^[0-9a-z]{8,14}$/);
    expect(d.c).toMatch(/^[0-9a-z]+$/);
    expect(d.gl).toMatchObject({ v: expect.any(String), r: expect.any(String), h: expect.stringMatching(/^[0-9a-z]+$/) });
    expect(d.gl.r.length).toBeGreaterThan(3);
    expect(typeof d.au).toBe('number');
    expect(d.au).toBeGreaterThan(0);
    expect(d.fo).toBeUndefined(); // the font probe is off by default
    expect(d.x).toMatchObject({ pf: expect.any(String), vnd: expect.any(String), tz: expect.any(String), lg: expect.any(String),
      pl: expect.any(String), mt: expect.any(String), mc: expect.any(String), ps: expect.any(String) });
    expect(d.x.sc).toEqual([expect.any(Number), expect.any(Number), expect.any(Number)]);
    expect(d.x.cn).toBeUndefined(); // the canvas renders identically twice
    // Playwright drives Chromium with navigator.webdriver set.
    expect(d.bot).toMatchObject({ wd: 1, pl: expect.any(Number), lg: expect.any(Number), oz: 0, hl: expect.any(Number) });
    expect(d.ms).toBeGreaterThanOrEqual(0);

    // Same session, same result: not sent again.
    await page.reload();
    await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
    await sleep(2500);
    expect(ofType(await getHits(request, { vid }), 'fp').length).toBe(1);

    // When the result differs from the one reported (simulated here), it is sent again.
    await page.evaluate(() => sessionStorage.setItem('ts_fp', sessionStorage.getItem('ts_fp').replace(/\.[^.]*$/, '.other')));
    await page.reload();
    hits = await waitForHits(request, { vid }, (h) => ofType(h, 'fp').length === 2);
    expect(ofType(hits, 'fp')[1].data.h).toBe(d.h); // the fingerprint itself is stable
  });

  test('only automation signals when fingerprinting is off', async ({ page, request }) => {
    const { vid } = await open(page, request, '/page2.html?site=botonly');
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'fp').length === 1);
    const d = ofType(hits, 'fp')[0].data;
    expect(own(d)).toEqual(['bot', 'fv', 'ms']);
    expect(d.bot.wd).toBe(1);
  });
});

test.describe('clicks', () => {
  test('clicks carry page, viewport and element-relative positions', async ({ page, request }) => {
    const { vid, pid } = await open(page, request, '/plugins.html?site=test', 'clicks');
    const box = await page.locator('#heat').boundingBox();
    await page.click('#heat', { position: { x: 100, y: 150 } }); // a 400×200 box: (0.25, 0.75)
    await page.click('#live');
    await page.click('#secret-btn'); // inside .ts-no-track: ignored
    await page.click('label[for=agree]'); // one click, although the label forwards one to its checkbox
    await page.click('#submit', { modifiers: ['Alt'] });
    await page.locator('#live').press('Enter'); // a keyboard click: no position
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'click').length >= 5);
    await sleep(1200);
    const clicks = ofType(await getHits(request, { vid }), 'click');
    expect(clicks.map((c) => c.data.sel)).toEqual(['#heat', '#live', '#signup>label', '#submit', '#live']);
    const heat = clicks[0].data;
    expect(clicks[0].page_id).toBe(pid);
    expect(heat).toMatchObject({ tag: 'div', btn: 0, vw: 1280, vh: 720, ex: 0.25, ey: 0.75, ew: 400, eh: 200 });
    expect(heat.vx).toBe(Math.round(box.x + 100));
    expect(heat.vy).toBe(Math.round(box.y + 150));
    expect([heat.x, heat.y]).toEqual([heat.vx, heat.vy]); // not scrolled
    expect(heat.pw).toBeGreaterThanOrEqual(1280);
    expect(heat.ph).toBeGreaterThanOrEqual(720);
    expect(heat.txt).toBeUndefined(); // only the box's own text nodes, and it has none
    expect(clicks[1].data).toMatchObject({ tag: 'button', txt: 'Live button' });
    expect(clicks[2].data).toMatchObject({ tag: 'label', txt: 'I agree' });
    expect(clicks[3].data).toMatchObject({ tag: 'button', txt: 'Sign up', mod: 1 });
    expect(clicks[4].data).toMatchObject({ kb: 1, txt: 'Live button' });
    expect(clicks[4].data.x).toBeUndefined();
    expect(hits.some((h) => JSON.stringify(h.data).includes('secret-btn'))).toBe(false);
  });

  test('links: outbound, downloads, mailto and tel, with plain clicks and heatmaps off', async ({ page, request }) => {
    await page.route('https://outbound.example/**', (r) => r.fulfill({ status: 200, contentType: 'text/html', body: '<title>Out</title>' }));
    await page.route('**/files/report.pdf', (r) => r.fulfill({ status: 204 }));
    await page.route('**/export?id=3', (r) => r.fulfill({ status: 200, contentType: 'text/csv', body: 'a,b\n' }));
    const { vid } = await open(page, request, '/plugins.html?site=links', 'clicks');
    await page.click('#heat'); // a plain click: not reported on this site
    await page.click('#dl');
    await page.click('#dl-attr');
    await page.click('#mail');
    await page.click('#tel');
    await sleep(300);
    await page.click('#out'); // leaves the page: delivered by the pagehide beacon
    await page.waitForURL('https://outbound.example/**');
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'click').length >= 5);
    const clicks = ofType(hits, 'click').map((h) => h.data);
    expect(clicks.map((c) => [c.sel, c.lk, c.href])).toEqual([
      ['#dl', 'dl', 'http://127.0.0.1:8788/files/report.pdf'],
      ['#dl-attr', 'dl', 'http://127.0.0.1:8788/export?id=3'],
      ['#mail', 'mail', 'mailto:hello@example.com'],
      ['#tel', 'tel', 'tel:+15551234567'],
      ['#out', 'out', 'https://outbound.example/landing?x=1']
    ]);
    for (const c of clicks) {
      expect(c.ex).toBeUndefined(); // heatmap_clicks is off
      expect(typeof c.x).toBe('number');
    }
  });

  test('rage clicks: one event per burst', async ({ page, request }) => {
    const { vid } = await open(page, request, '/plugins.html?site=test', 'clicks');
    const box = await page.locator('#heat').boundingBox();
    for (let i = 0; i < 5; i++) await page.mouse.click(box.x + 200 + i * 3, box.y + 100);
    let hits = await waitForHits(request, { vid }, (h) => ofType(h, 'rage').length === 1);
    const rage = ofType(hits, 'rage')[0].data;
    expect(rage).toMatchObject({ sel: '#heat', n: 5, x: Math.round(box.x + 200), y: Math.round(box.y + 100) });
    expect(rage.ms).toBeLessThan(1000);
    await page.mouse.click(box.x + 20, box.y + 20); // two clicks: not a rage
    await page.mouse.click(box.x + 20, box.y + 20);
    await sleep(2000);
    hits = await getHits(request, { vid });
    expect(ofType(hits, 'rage').length).toBe(1);
    expect(ofType(hits, 'click').length).toBe(7);
  });

  test('dead clicks: clickable-looking elements that do nothing', async ({ page, request }) => {
    const { vid } = await open(page, request, '/plugins.html?site=test', 'clicks');
    await waitForHits(request, { vid }, (h) => ofType(h, 'adblock').length === 1); // its bait is a DOM change
    await page.click('#dead');
    await waitForHits(request, { vid }, (h) => ofType(h, 'dead').length === 1);
    await page.click('#fake'); // cursor: pointer
    await waitForHits(request, { vid }, (h) => ofType(h, 'dead').length === 2);
    await page.click('#hash-link'); // a link whose script prevents the navigation and does nothing else
    await waitForHits(request, { vid }, (h) => ofType(h, 'dead').length === 3);
    await page.click('#live'); // changes the page
    await page.click('#name'); // a field: focusing it is its effect
    await sleep(1500);
    const dead = ofType(await getHits(request, { vid }), 'dead').map((h) => h.data);
    expect(dead.map((d) => [d.sel, d.tag])).toEqual([['#dead', 'button'], ['#fake', 'div'], ['#hash-link', 'a']]);
    expect(typeof dead[0].x).toBe('number');
  });

  test.describe(() => {
    test.use({ allowedPageErrors: [/boom from click/] });
    test('error clicks: a click followed by a JS error', async ({ page, request }) => {
      const { vid } = await open(page, request, '/plugins.html?site=test', 'clicks', 'errors');
      await page.click('#boom');
      const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'errclick').length === 1 && ofType(h, 'error').length === 1);
      expect(ofType(hits, 'errclick')[0].data).toMatchObject({ sel: '#boom', msg: expect.stringContaining('boom from click') });
    });
  });
});

test.describe('scroll', () => {
  test('milestones (the highest of several crossed at once) and the maximum on engagement', async ({ page, request }) => {
    const { vid, pid } = await open(page, request, '/scroll.html?site=test', 'scroll'); // 4000 px; 720 visible: 18 %
    await page.evaluate(() => window.scrollTo(0, 1280)); // (1280 + 720) / 4000 = 50 %: 25 and 50 at once
    await waitForHits(request, { vid }, (h) => ofType(h, 'scroll').length === 1);
    await page.evaluate(() => window.scrollTo(0, 4000)); // the end: 75, 90 and 100
    let hits = await waitForHits(request, { vid }, (h) => ofType(h, 'scroll').length === 2);
    expect(ofType(hits, 'scroll').map((h) => h.data)).toEqual([
      expect.objectContaining({ d: 50, max: 50, ph: 4000, vh: 720 }),
      expect.objectContaining({ d: 100, max: 100, ph: 4000, vh: 720 })
    ]);
    expect(ofType(hits, 'scroll')[0].page_id).toBe(pid);
    await hidePage(page);
    hits = await waitForHits(request, { vid }, (h) => ofType(h, 'engagement').some((e) => e.data.why === 'hidden'));
    expect(ofType(hits, 'engagement').find((e) => e.data.why === 'hidden').data.sd).toBe(100);
  });

  test('a short page is fully seen at load; depth restarts on SPA routes', async ({ page, request }) => {
    const { vid, pid } = await open(page, request, '/spa/index.html?site=test', 'scroll');
    await waitForHits(request, { vid }, (h) => ofType(h, 'scroll').length === 1);
    await page.click('#go-about');
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'scroll').length === 2 && ofType(h, 'pageview').length === 2);
    const pvs = ofType(hits, 'pageview'), sc = ofType(hits, 'scroll');
    expect(sc.map((h) => [h.page_id, h.data.d])).toEqual([[pid, 100], [pvs[1].page_id, 100]]);
    const route = ofType(hits, 'engagement').find((e) => e.data.why === 'route');
    if (route) expect(route.data.sd).toBe(100);
  });
});

test.describe('forms', () => {
  test('start, blur, validation and submit, never a value', async ({ page, request }) => {
    const SECRET = 'Sup3r-Secret-Pa55';
    const { vid } = await open(page, request, '/plugins.html?site=test', 'forms');
    await page.click('#name');
    await page.keyboard.type('Alice Example');
    await page.click('#pw');
    await page.keyboard.type(SECRET);
    await page.click('#email');
    await page.keyboard.type('not-an-email');
    await page.click('#submit'); // invalid e-mail: the browser blocks the submit
    await waitForHits(request, { vid }, (h) => ofType(h, 'form').some((f) => f.data.a === 'invalid'));
    await page.fill('#email', 'alice@example.com');
    await page.selectOption('#plan', 'pro');
    await page.click('#submit');
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'form').some((f) => f.data.a === 'submit'));
    const forms = ofType(hits, 'form').map((h) => h.data);
    expect(forms[0]).toMatchObject({ a: 'start', f: 'signup', fld: 'name' });
    const blur = (fld) => forms.find((f) => f.a === 'blur' && f.fld === fld);
    expect(blur('name')).toMatchObject({ f: 'signup', ft: 'text', filled: 1, ms: expect.any(Number) });
    expect(blur('pw')).toMatchObject({ ft: 'password', filled: 1 });
    expect(blur('name').ms).toBeGreaterThan(0);
    expect(forms.find((f) => f.a === 'invalid')).toMatchObject({ f: 'signup', fld: 'email', ft: 'email', why: 'typeMismatch' });
    const submit = forms.find((f) => f.a === 'submit');
    expect(submit).toMatchObject({ f: 'signup' });
    expect(submit.n).toBeGreaterThanOrEqual(4); // name, pw, email, plan
    expect(submit.ms).toBeGreaterThan(0);
    expect(forms.some((f) => f.fld === 'token')).toBe(false); // hidden fields are skipped
    await page.evaluate(() => document.getElementById('signup').reset());
    await waitForHits(request, { vid }, (h) => ofType(h, 'form').some((f) => f.data.a === 'reset' && f.data.f === 'signup'));
    // Nothing typed, nor the hidden field's value, is ever stored.
    const all = JSON.stringify(await getHits(request, { vid }));
    for (const v of [SECRET, 'Alice Example', 'not-an-email', 'alice@example.com', 'hidden-token-value']) expect(all).not.toContain(v);
  });

  test('abandoned forms, fields outside forms, ignored forms', async ({ page, request }) => {
    const { vid } = await open(page, request, '/plugins.html?site=test', 'forms');
    await page.click('#nl');
    await page.keyboard.type('x@example.com');
    await page.click('#search');
    await page.keyboard.type('tides');
    await page.click('#private'); // inside a .ts-no-track form
    await page.keyboard.type('private stuff');
    await page.click('#name');
    await page.goto('/page2.html?site=test'); // leave without submitting
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'form').filter((f) => f.data.a === 'abandon').length === 3);
    const forms = ofType(hits, 'form').map((h) => h.data);
    expect(forms.filter((f) => f.a === 'start').map((f) => f.f)).toEqual(['newsletter', '_page', 'signup']);
    const ab = forms.filter((f) => f.a === 'abandon');
    expect(ab).toEqual(expect.arrayContaining([
      expect.objectContaining({ f: 'newsletter', n: 1, last: 'nl-email' }),
      expect.objectContaining({ f: '_page', n: 1, last: 'q' }),
      expect.objectContaining({ f: 'signup', n: 1, last: 'name' })
    ]));
    expect(forms.some((f) => f.fld === 'private' || f.f === 'private-form')).toBe(false);
    expect(JSON.stringify(hits)).not.toContain('private stuff');
  });
});

test.describe('performance', () => {
  test('Web Vitals, navigation timing and long tasks for the page load', async ({ page, request }) => {
    const { vid, pid } = await open(page, request, '/vitals.html?site=test', 'perf');
    await page.waitForFunction(() => window.shifted);
    await sleep(300);
    await page.click('#slow'); // a 150 ms handler: INP, a long task; the first input also makes LCP final
    await sleep(300);
    await hidePage(page);
    const names = ['TTFB', 'FCP', 'LCP', 'CLS', 'INP'];
    const hits = await waitForHits(request, { vid }, (h) => names.every((n) => ofType(h, 'vital').some((v) => v.data.n === n)) &&
      ofType(h, 'perf').length === 1 && ofType(h, 'longtask').length >= 1);
    const vitals = ofType(hits, 'vital');
    expect(vitals.length).toBe(5); // each once
    const v = {};
    for (const h of vitals) {
      v[h.data.n] = h.data;
      expect(h.page_id).toBe(pid);
      expect(h.data.nt).toBe('navigate');
    }
    const LIMITS = { LCP: [2500, 4000], CLS: [0.1, 0.25], INP: [200, 500], FCP: [1800, 3000], TTFB: [800, 1800] };
    for (const n of names) {
      const t = LIMITS[n], x = v[n];
      expect(x.r).toBe(x.val <= t[0] ? 'good' : x.val <= t[1] ? 'ni' : 'poor');
    }
    expect(v.TTFB.val).toBeGreaterThanOrEqual(0);
    expect(v.FCP.val).toBeGreaterThan(0);
    expect(v.LCP.val).toBeGreaterThanOrEqual(v.FCP.val);
    expect(v.LCP.el).toBe('#hero');
    expect(v.CLS.val).toBeGreaterThan(0.01); // the late banner
    expect(v.CLS.val).toBeLessThan(1);
    expect(typeof v.CLS.el).toBe('string');
    expect(v.INP.val).toBeGreaterThanOrEqual(100);
    expect(v.INP.el).toBe('#slow');

    const perf = ofType(hits, 'perf')[0];
    expect(perf.page_id).toBe(pid);
    expect(perf.data).toMatchObject({ nt: 'navigate', rc: 0, proto: expect.any(String) });
    for (const k of ['dns', 'tcp', 'ttfb', 'req', 'dl', 'dom', 'dcl', 'load', 'rt', 'tsz', 'ebs', 'dbs']) expect(typeof perf.data[k]).toBe('number');
    expect(perf.data.load).toBeGreaterThanOrEqual(perf.data.dcl);
    expect(perf.data.dcl).toBeGreaterThanOrEqual(perf.data.dom);
    expect(perf.data.dbs).toBeGreaterThan(0);
    expect(perf.data.ts).toBeGreaterThan(1e12); // the event's client time, not a size (that is tsz)

    const lt = ofType(hits, 'longtask')[0].data;
    expect(lt.n).toBeGreaterThanOrEqual(1);
    expect(lt.max).toBeGreaterThanOrEqual(100);
    expect(lt.tot).toBeGreaterThanOrEqual(lt.max);
    expect(lt.tbt).toBeGreaterThanOrEqual(50);
  });
});

test.describe('errors', () => {
  test.use({ allowedPageErrors: [/early boom|kaboom|nope/] });
  test('JS errors, rejections and failed resources, each once per page with a final count', async ({ page, request }) => {
    const { vid, pid } = await open(page, request, '/errors.html?site=test', 'errors');
    // Raised before the plugin loaded: kept by the core, sent by the plugin with its original time.
    let hits = await waitForHits(request, { vid }, (h) => ofType(h, 'error').length === 1);
    const early = ofType(hits, 'error')[0];
    expect(early.data).toMatchObject({ k: 'error', msg: expect.stringContaining('early boom'), src: expect.stringContaining('/errors.html'),
      line: expect.any(Number), col: expect.any(Number), stack: expect.stringContaining('early boom') });
    expect(early.page_id).toBe(pid);
    for (let i = 0; i < 3; i++) { await page.click('#err'); await sleep(50); }
    await page.click('#rej');
    await page.click('#res');
    hits = await waitForHits(request, { vid }, (h) => ofType(h, 'error').length === 4);
    const errs = ofType(hits, 'error').map((h) => h.data);
    expect(errs[1]).toMatchObject({ k: 'error', msg: expect.stringContaining('kaboom'), stack: expect.stringContaining('kaboom') });
    expect(errs[1].n).toBeUndefined();
    expect(errs[2]).toMatchObject({ k: 'rejection', msg: 'nope', stack: expect.stringContaining('nope') });
    expect(errs[3]).toMatchObject({ k: 'resource', tag: 'img', res: 'http://127.0.0.1:8788/missing-image.png' });
    await hidePage(page);
    hits = await waitForHits(request, { vid }, (h) => ofType(h, 'error').some((e) => e.data.upd === 1));
    const upd = ofType(hits, 'error').filter((e) => e.data.upd === 1).map((e) => e.data);
    expect(upd).toEqual([expect.objectContaining({ k: 'error', msg: errs[1].msg, line: errs[1].line, n: 3 })]);
  });
});

test.describe('window signals', () => {
  test('resize, visibility, copy, paste and selection, never the text', async ({ page, request }) => {
    const { vid } = await open(page, request, '/plugins.html?site=test', 'ux');
    await page.setViewportSize({ width: 1000, height: 700 });
    await waitForHits(request, { vid }, (h) => ofType(h, 'resize').length === 1);
    await page.evaluate(() => {
      const r = document.createRange();
      r.selectNodeContents(document.getElementById('para'));
      getSelection().removeAllRanges();
      getSelection().addRange(r);
    });
    await waitForHits(request, { vid }, (h) => ofType(h, 'select').length === 1);
    await page.keyboard.press('ControlOrMeta+C');
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.setData('text/plain', 'pasted words');
      document.getElementById('search').dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true }));
      window.dispatchEvent(new Event('blur'));
      window.dispatchEvent(new Event('focus'));
    });
    await hidePage(page);
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'copy').length === 1 && ofType(h, 'paste').length === 1 &&
      ofType(h, 'vis').some((v) => v.data.s === 'hidden'));
    const text = await page.locator('#para').textContent();
    expect(ofType(hits, 'resize')[0].data).toMatchObject({ vw: 1000, vh: 700 });
    expect(ofType(hits, 'select')[0].data).toMatchObject({ sel: '#para', len: text.length });
    expect(ofType(hits, 'copy')[0].data).toMatchObject({ len: text.length });
    expect(ofType(hits, 'paste')[0].data).toMatchObject({ sel: '#search', len: 'pasted words'.length });
    expect(ofType(hits, 'vis').map((v) => v.data.s)).toEqual(['blur', 'focus', 'hidden']);
    const all = JSON.stringify(hits);
    expect(all).not.toContain('quick brown fox');
    expect(all).not.toContain('pasted words');
  });
});

test.describe('ad blocker', () => {
  test('not detected without one; checked once per session', async ({ page, request }) => {
    const { vid } = await open(page, request, '/page2.html?site=test');
    await waitForHits(request, { vid }, (h) => ofType(h, 'adblock').length === 1);
    await page.reload();
    await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
    await sleep(1500);
    const ab = ofType(await getHits(request, { vid }), 'adblock');
    expect(ab.map((h) => h.data.ab)).toEqual([0]);
  });

  test('detected when the bait is hidden', async ({ page, request }) => {
    await page.addInitScript(() => document.addEventListener('DOMContentLoaded', () => {
      const s = document.createElement('style');
      s.textContent = '.adsbox { display: none !important; }'; // what a cosmetic filter list does
      document.head.appendChild(s);
    }));
    const { vid } = await open(page, request, '/page2.html?site=test');
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'adblock').length === 1);
    expect(ofType(hits, 'adblock')[0].data.ab).toBe(1);
    expect(await page.locator('.adsbox').count()).toBe(0); // the bait is removed
  });
});

test.describe('loading', () => {
  test('plugins sampled out (rate 0) or switched off are never requested', async ({ page, request }) => {
    const files = [];
    page.on('request', (r) => { if (r.url().includes('/ts/')) files.push(r.url()); });
    const { vid } = await open(page, request, '/plugins.html?site=sampled0');
    await page.mouse.move(10, 10);
    await page.mouse.move(300, 200, { steps: 5 });
    await page.click('#live');
    await sleep(1200);
    expect(files).toEqual([]);
    expect(ofType(await getHits(request, { vid }), 'pageview').length).toBe(1);
  });

  test('the single-file bundle runs its plugins and never loads them again', async ({ page, request }) => {
    const files = [];
    page.on('request', (r) => { if (r.url().includes('/ts/')) files.push(r.url()); });
    const { vid } = await open(page, request, '/plugins.html?site=test&bundle=ts.all.js');
    await sleep(300);
    await page.click('#live');
    await waitForHits(request, { vid }, (h) => ofType(h, 'click').length === 1);
    await page.reload(); // the site configuration is cached now: the core starts at once
    await waitForHits(request, { vid }, (h) => ofType(h, 'pageview').length === 2);
    await sleep(300);
    await page.click('#live');
    const hits = await waitForHits(request, { vid }, (h) => ofType(h, 'click').length === 2);
    expect(ofType(hits, 'click')[1].page_id).toBe(ofType(hits, 'pageview')[1].page_id);
    expect(files).toEqual([]);
  });

  test('plugins never break a hostile page', async ({ page }) => {
    // Storage and cookies throw, no fetch, no sendBeacon, no CompressionStream, Intl broken (see broken.html).
    for (const site of ['test', 'replaysnap']) {
      await page.goto('/broken.html?site=' + site);
      await sleep(800);
      await page.mouse.move(20, 20);
      await page.mouse.move(200, 100, { steps: 4 });
      await page.click('#track');
      await page.click('#track');
      await page.click('#track');
      await expect.poll(() => page.evaluate(() => window.clicked === true)).toBeTruthy();
      await sleep(2500); // DOM snapshot time
      await hidePage(page);
      await sleep(500);
    }
  });
});
