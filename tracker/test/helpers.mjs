// Shared helpers: every test gets a fresh browser context (so a fresh visitor) and fails if any
// exception reaches the host page. Stored data is read back through the collector's /__test/ API.
import { test as base, expect } from '@playwright/test';

export const API = 'http://127.0.0.1:8787';

export const test = base.extend({
  allowedPageErrors: [[], { option: true }],
  page: async ({ page, allowedPageErrors }, use) => {
    const errors = [];
    page.on('pageerror', (e) => {
      if (!allowedPageErrors.some((re) => re.test(String(e && e.message)))) errors.push(String(e && e.stack || e));
    });
    await use(page);
    expect(errors, 'no exception may reach the host page').toEqual([]);
  }
});
export { expect };

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function visitorId(page) {
  await page.waitForFunction(() => { try { return !!localStorage.getItem('ts_vid'); } catch (e) { return false; } });
  return page.evaluate(() => localStorage.getItem('ts_vid').split('.')[0]);
}

export async function getHits(request, filter) {
  const r = await request.get(API + '/__test/hits?' + new URLSearchParams(filter));
  return r.json();
}

export async function waitForHits(request, filter, predicate, timeout = 10000) {
  const deadline = Date.now() + timeout;
  let hits = [];
  while (Date.now() < deadline) {
    hits = await getHits(request, filter);
    if (predicate(hits)) return hits;
    await sleep(150);
  }
  throw new Error('timed out waiting for hits; got ' +
    JSON.stringify(hits.map((h) => [h.type, h.name, h.data && (h.data.why || h.data.nav)])));
}

export const ofType = (hits, t) => hits.filter((h) => h.type === t);
export const named = (hits, n) => hits.filter((h) => h.type === 'event' && h.name === n);

// Make the page believe it was hidden (as when switching tabs), which flushes with sendBeacon.
export async function hidePage(page) {
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    Object.defineProperty(document, 'hidden', { value: true, configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
}
