// The README's and the website's screenshots, taken from the demo data (tools/demo.py) in a real browser.
//
//   python tools/demo.py --out demo-data --public-url https://stats.example.com/
//   node tools/screenshots.mjs --data demo-data            # writes docs/images/*.png (--only name,name)
//
// Needs the tracker's Playwright (cd tracker && npm install && npx playwright install chromium). The dashboard is
// served by server/python/devserver.py: without sign-in for the reports, and a second, empty installation for the
// first-account form. Set PYTHON to the interpreter to use (default: python3, or python on Windows).
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(join(REPO, 'tracker', 'package.json'));
const { chromium } = require('@playwright/test');

const arg = (name, fallback) => { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : fallback; };
const DATA = resolve(arg('--data', 'demo-data'));
const OUT = resolve(arg('--out', join(REPO, 'docs', 'images')));
const ONLY = arg('--only', '');
const PY = (process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3')).split(' ');
const OPEN = 8811, FRESH = 8812;

function server(config, port, extra = []) {
  const p = spawn(PY[0], [...PY.slice(1), join(REPO, 'server', 'python', 'devserver.py'), '--config', config, '--port', String(port), ...extra],
    { stdio: ['ignore', 'ignore', 'inherit'] });
  return p;
}
async function up(port) {
  for (let i = 0; i < 120; i++) {
    try { if ((await fetch('http://127.0.0.1:' + port + '/health')).ok) return; } catch (e) { /* not yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error('the dev server on port ' + port + ' did not start');
}

const fresh = mkdtempSync(join(tmpdir(), 'tidesight-fresh-'));
mkdirSync(join(fresh, 'data'));
writeFileSync(join(fresh, 'tidesight.toml'), '[server]\ndata_dir = "' + join(fresh, 'data').replace(/\\/g, '/') + '"\n');
const procs = [server(join(DATA, 'tidesight.toml'), OPEN, ['--no-auth']), server(join(fresh, 'tidesight.toml'), FRESH)];
const stop = () => procs.forEach((p) => { try { p.kill(); } catch (e) { /* gone */ } });
process.on('exit', stop);

try {
  await Promise.all([up(OPEN), up(FRESH)]);
  const browser = await chromium.launch();
  const base = 'http://127.0.0.1:' + OPEN + '/dashboard/';
  const context = async (scheme = 'light') => {
    const c = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: scheme,
      locale: 'en-US', timezoneId: 'Europe/Paris' }); // the same pictures wherever they are taken
    await c.addInitScript(() => { try { localStorage.clear(); } catch (e) { /* none */ } });
    return c;
  };
  const shots = [];
  const shot = (name, fn) => { if (!ONLY || ONLY.split(',').includes(name)) shots.push([name, fn]); };
  const settle = async (page, ms = 900) => { await page.waitForLoadState('networkidle'); await page.waitForTimeout(ms); };
  const go = async (page, hash, ms) => { await page.goto(base + hash); await settle(page, ms); };
  const save = (page, name, clip) => page.screenshot({ path: join(OUT, name + '.png'), clip });

  shot('overview', async (page) => { await go(page, '#/overview?site=demo'); await save(page, 'overview'); });
  shot('overview-dark', async (_p, ctx) => {
    const c = await context('dark');
    const page = await c.newPage();
    await go(page, '#/overview?site=demo');
    await save(page, 'overview-dark');
    await c.close();
  });
  shot('realtime', async (page) => { await go(page, '#/realtime?site=demo', 1500); await save(page, 'realtime'); });
  shot('acquisition', async (page) => { await go(page, '#/sources?site=demo'); await save(page, 'acquisition'); });
  shot('filters', async (page) => {
    await go(page, '#/overview?site=journal');
    await page.locator('[data-add-filter]').click();
    await page.locator('.fb input[type=search]').fill('coun');
    await page.locator('.fb-dim', { hasText: 'Country' }).click();
    await page.locator('.fb-option').nth(0).click();
    await page.locator('.fb-option').nth(4).click();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(400);
    await save(page, 'filters');
    await page.keyboard.press('Escape');
  });
  shot('locations', async (page) => { await go(page, '#/geo?site=journal', 1500); await save(page, 'locations'); });
  shot('heatmap', async (page) => {
    await go(page, '#/heatmaps?site=demo&page=' + encodeURIComponent('/products/coldwater-5-4-hooded'), 2500);
    await save(page, 'heatmap');
  });
  shot('replay', async (page) => {
    await go(page, '#/replays?site=demo');
    const rows = page.locator('table tbody tr'); // a recording of several pages
    let pick = 0;
    for (let i = 0; i < Math.min(await rows.count(), 40); i++) {
      if (Number((await rows.nth(i).locator('td').nth(2).innerText()).trim()) >= 3) { pick = i; break; }
    }
    await rows.nth(pick).click();
    await page.waitForTimeout(1500);
    await page.locator('.rr-controller__btns button').first().click(); // play
    await page.waitForTimeout(4200);
    await page.locator('.rr-controller__btns button').first().click(); // pause, the pointer mid-way
    await page.waitForTimeout(400);
    await save(page, 'replay');
  });
  shot('visit', async (page) => {
    // A visit that bought something: its timeline tells the whole story.
    await go(page, '#/sessions?site=demo&filters=' + encodeURIComponent(JSON.stringify([['goal', 'is', ['purchase']]])));
    await page.locator('table tbody tr').first().click();
    await settle(page, 1200);
    await save(page, 'visit');
  });
  shot('funnels', async (page) => {
    await page.goto(base + '#/overview?site=demo');
    await page.evaluate(() => localStorage.setItem('ts_dash_funnel_demo', JSON.stringify([
      { type: 'page', value: '/' }, { type: 'page', value: '/products/*' }, { type: 'event', value: 'add_to_cart' },
      { type: 'page', value: '/checkout' }, { type: 'event', value: 'purchase' }])));
    await go(page, '#/funnels?site=demo');
    await save(page, 'funnels');
  });
  shot('retention', async (page) => { await go(page, '#/retention?site=harbor'); await save(page, 'retention'); });
  shot('performance', async (page) => { // the 60 days of data: Web Vitals improve after the product photos were resized
    const day = (n) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
    await go(page, '#/performance?site=demo&from=' + day(59) + '&to=' + day(0), 1200);
    await save(page, 'performance');
  });
  shot('properties', async (page) => { await go(page, '#/properties'); await save(page, 'properties'); });
  shot('add-property', async (page) => {
    await go(page, '#/properties');
    await page.getByRole('button', { name: 'Add property' }).click();
    await page.locator('.wizard input').first().fill('www.example.com');
    await page.waitForTimeout(500);
    await save(page, 'add-property');
    await page.locator('.dialog-foot .btn.primary').click();
    await page.waitForTimeout(500);
    await page.locator('.dialog-foot .btn.primary').click(); // create it: the tracking code, and the live check
    await page.locator('.wait-box').waitFor();
    await page.waitForTimeout(800);
    await save(page, 'tracking-code');
  });
  shot('users', async (page) => { await go(page, '#/users'); await save(page, 'users'); });
  // The picture shown where the repository or the website is linked (GitHub's social preview, og:image): 1280 × 640.
  shot('social-preview', async (page) => {
    const file = (p) => 'file:///' + p.replace(/\\/g, '/');
    const font = (name) => file(join(REPO, 'dashboard', 'vendor', 'fonts', name));
    const html = `<!doctype html><meta charset="utf-8"><style>
      @font-face { font-family: Fraunces; src: url("${font('fraunces-latin-italic.woff2')}"); font-style: italic; font-weight: 100 900; }
      @font-face { font-family: Plex; src: url("${font('plex-sans-latin.woff2')}"); font-weight: 100 700; }
      body { margin: 0; width: 1280px; height: 640px; overflow: hidden; color: #e9eef3; font-family: Plex, sans-serif;
        background: radial-gradient(800px 420px at 90% -10%, #1d4b72 0%, transparent 60%), radial-gradient(700px 500px at -10% 110%, #14395a 0%, transparent 60%), #0f2236; }
      .copy { position: absolute; left: 78px; top: 92px; width: 560px; }
      .brand { display: flex; align-items: center; gap: 16px; font: italic 600 44px Fraunces, serif; }
      .brand svg { width: 58px; height: 58px; }
      h1 { margin: 54px 0 0; font: italic 600 68px/1.02 Fraunces, serif; letter-spacing: -.02em; color: #fff; }
      h1 em { color: #9fd1ff; font-weight: 500; }
      p { margin: 28px 0 0; font-size: 25px; line-height: 1.45; color: #b9cadb; }
      .pill { display: inline-block; margin-top: 36px; padding: 9px 18px; border-radius: 999px; border: 1px solid rgba(159,209,255,.3); color: #9fd1ff; font-size: 19px; }
      .shot { position: absolute; left: 690px; top: 70px; width: 900px; border-radius: 14px; overflow: hidden; border: 1px solid rgba(255,255,255,.15);
        box-shadow: 0 40px 100px -30px rgba(0,0,0,.8), 0 0 80px -20px rgba(106,183,255,.35); transform: perspective(1600px) rotateY(-14deg) rotateX(3deg); transform-origin: 0 50%; }
      .shot img { display: block; width: 100%; }
      svg.tides { position: absolute; inset: 0; opacity: .12; }
    </style>
    <svg class="tides" viewBox="0 0 1280 640" preserveAspectRatio="none"><g fill="none" stroke="#9fd1ff">
      <path d="M0 470 C 200 420, 400 520, 640 470 S 1080 420, 1280 470"/><path d="M0 530 C 220 480, 420 580, 660 530 S 1100 480, 1280 530"/>
      <path d="M0 590 C 180 540, 380 640, 620 590 S 1060 540, 1280 590"/></g></svg>
    <div class="copy">
      <div class="brand"><svg viewBox="0 0 34 34"><circle cx="17" cy="17" r="15.5" fill="none" stroke="#9fd1ff" stroke-opacity=".5"/><circle cx="17" cy="17" r="4" fill="#9fd1ff"/><path d="M3 22c3.5-3 7-3 10.5 0s7 3 10.5 0 7-3 7 0" fill="none" stroke="#e9eef3" stroke-width="1.6" stroke-linecap="round"/><path d="M5 26.5c3-2.4 6-2.4 9 0s6 2.4 9 0 4.5-2 6 0" fill="none" stroke="#9fd1ff" stroke-width="1.2" stroke-linecap="round" opacity=".7"/></svg>Tidesight</div>
      <h1>Analytics that keeps <em>every wave</em>.</h1>
      <p>Open-source, self-hosted web analytics. Every hit kept raw, on hosting you already have.</p>
      <span class="pill">Live demo · MIT · Python + SQLite</span>
    </div>
    <div class="shot"><img src="${file(join(OUT, 'overview.png'))}"></div>`;
    const tmp = join(mkdtempSync(join(tmpdir(), 'tidesight-og-')), 'og.html');
    writeFileSync(tmp, html);
    await page.setViewportSize({ width: 1280, height: 640 });
    await page.goto(file(tmp));
    await page.waitForTimeout(600);
    await page.screenshot({ path: join(OUT, 'social-preview.png') });
  });
  shot('setup', async (page) => {
    await page.goto('http://127.0.0.1:' + FRESH + '/dashboard/');
    await settle(page, 1000);
    await save(page, 'setup');
  });

  const ctx = await context();
  for (const [name, fn] of shots) {
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await fn(page, ctx);
    if (errors.length) console.warn(name + ': errors in the page: ' + errors.join('; '));
    console.log('  ' + name + '.png');
    await page.close();
  }
  await browser.close();
} finally {
  stop();
}
