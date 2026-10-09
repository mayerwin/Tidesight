// Helpers for the plugin specs: plugin readiness, stored chunks, and the bodies of the chunks a page sends.
import { gunzipSync } from 'node:zlib';
import { API, sleep } from './helpers.mjs';

// Wait until the named plugin files have loaded; the core runs a plugin once its file has run and the first
// pageview exists, so callers also wait for the pageview hit.
export async function pluginsReady(page, ...names) {
  await page.waitForFunction((ns) => ns.every((n) => performance.getEntriesByType('resource')
    .some((e) => e.name.indexOf('/ts/' + n + '.js') > 0)), names);
  await sleep(150);
}

// Chunks stored by the collector for a visitor, with meta parsed.
export async function getChunks(request, vid) {
  const rows = await (await request.get(API + '/__test/chunks?since=0')).json();
  return rows.filter((c) => c.visitor_id === vid).map((c) => Object.assign(c, { meta: JSON.parse(c.meta) }));
}

export async function waitForChunks(request, vid, predicate, timeout = 10000) {
  const deadline = Date.now() + timeout;
  let rows = [];
  while (Date.now() < deadline) {
    rows = await getChunks(request, vid);
    if (predicate(rows)) return rows;
    await sleep(150);
  }
  throw new Error('timed out waiting for chunks; got ' + JSON.stringify(rows.map((c) => [c.kind, c.seq, c.bytes])));
}

// Record, in the page, the body of every /chunk request (fetch or sendBeacon). Playwright cannot read Blob bodies
// (the gzip-compressed chunks), so the page keeps them as base64.
export async function captureChunks(page) {
  await page.addInitScript(() => {
    const out = (window.__chunks = []);
    const keep = (url, body, how) => {
      if (String(url).indexOf('/chunk?') < 0) return null;
      const rec = { url: String(url), how };
      out.push(rec);
      if (typeof body === 'string') rec.text = body;
      else if (body && body.arrayBuffer) {
        rec.pending = 1;
        body.arrayBuffer().then((b) => {
          const a = new Uint8Array(b);
          let s = '';
          for (let i = 0; i < a.length; i += 8192) s += String.fromCharCode.apply(null, a.subarray(i, i + 8192));
          rec.b64 = btoa(s);
          rec.pending = 0;
        });
      }
      return rec;
    };
    const safeKeep = (url, body, how) => { try { return keep(url, body, how); } catch (e) { return null; } };
    const f = window.fetch;
    window.fetch = function (url, o) {
      const rec = safeKeep(url, o && o.body, 'fetch');
      if (rec) { rec.keepalive = !!(o && o.keepalive); rec.referrerPolicy = o && o.referrerPolicy; }
      return f.apply(this, arguments);
    };
    const b = navigator.sendBeacon;
    navigator.sendBeacon = function (url, data) {
      const rec = safeKeep(url, data, 'beacon');
      const ok = b.apply(this, arguments);
      if (rec) rec.ok = ok;
      return ok;
    };
  });
}

// The captured chunk requests: {k, seq, rec, pid, how, keepalive, referrerPolicy, ok, gzip, text}; failed beacons
// (retried by fetch) left out.
export async function chunkBodies(page) {
  await page.waitForFunction(() => window.__chunks.every((c) => !c.pending));
  const raw = await page.evaluate(() => window.__chunks);
  return raw.filter((c) => !(c.how === 'beacon' && c.ok === false)).map((c) => {
    const q = new URL(c.url).searchParams;
    let buf = c.b64 != null ? Buffer.from(c.b64, 'base64') : Buffer.from(c.text || '', 'utf8');
    const gzip = buf[0] === 0x1f && buf[1] === 0x8b;
    if (gzip) buf = gunzipSync(buf);
    return { k: q.get('k'), seq: +q.get('seq'), rec: q.get('rec'), pid: q.get('pid'), ts: +q.get('ts'), how: c.how,
      keepalive: c.keepalive, referrerPolicy: c.referrerPolicy, gzip, text: buf.toString('utf8') };
  });
}
