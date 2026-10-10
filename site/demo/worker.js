// The live demo's worker: Pyodide (Python compiled to WebAssembly) runs Tidesight's web app on the demo data and
// answers the dashboard's API calls (see boot.js). Everything it loads comes from this site.
import { loadPyodide } from './pyodide/pyodide.mjs';

const say = (message) => self.postMessage(message);

// A file of this site, with progress reports for the larger ones.
async function download(name, label) {
  const res = await fetch(new URL(name, import.meta.url));
  if (!res.ok) throw new Error(name + ': HTTP ' + res.status);
  const total = Number(res.headers.get('Content-Length')) || 0;
  if (!res.body || !res.body.getReader) return new Uint8Array(await res.arrayBuffer());
  const reader = res.body.getReader();
  const parts = [];
  let done = 0;
  for (;;) {
    const { value, done: end } = await reader.read();
    if (end) break;
    parts.push(value);
    done += value.length;
    say({ progress: label, done, total });
  }
  const out = new Uint8Array(done);
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}

const started = (async () => {
  say({ progress: 'python' });
  const [py, code, data, bridge] = await Promise.all([
    loadPyodide({ indexURL: new URL('./pyodide/', import.meta.url).href }).then(async (p) => {
      await p.loadPackage('tzdata', { messageCallback: () => {} });
      return p;
    }),
    download('tidesight.zip', 'code'),
    download('demo-data.tar.gz', 'data'),
    fetch(new URL('bridge.py', import.meta.url)).then((r) => r.text()),
  ]);
  say({ progress: 'start' });
  py.unpackArchive(code, 'zip', { extractDir: '/app' });
  // (a server that sends the archive with Content-Encoding: gzip hands over the tar itself)
  py.unpackArchive(data, data[0] === 0x1f && data[1] === 0x8b ? 'gztar' : 'tar', { extractDir: '/demo' });
  py.runPython(bridge);
  const handle = py.globals.get('handle');
  const tick = py.globals.get('tick');
  setInterval(() => { try { tick(); } catch (e) { /* the next one */ } }, 20000);
  say({ ready: true });
  return handle;
})();
started.catch((e) => say({ failed: String((e && e.message) || e) }));

self.onmessage = async (e) => {
  const { id, url, method, headers, body } = e.data;
  let handle;
  try {
    handle = await started;
  } catch (err) {
    say({ id, status: 503, reason: 'Service Unavailable', headers: [['Content-Type', 'application/json']],
      body: new TextEncoder().encode(JSON.stringify({ error: 'The demo could not start.' })).buffer });
    return;
  }
  const u = new URL(url, self.location.href);
  const endpoint = u.pathname.slice(u.pathname.lastIndexOf('/api/') + 5);
  let status = 500, reason = 'Internal Server Error', hdrs = [['Content-Type', 'application/json']];
  let bytes = new TextEncoder().encode('{"error": "internal error"}');
  try {
    const out = handle(method, '/dashboard/api/' + endpoint, u.search.slice(1), JSON.stringify(headers || {}), body);
    [status, reason, hdrs, bytes] = out.toJs();
    out.destroy();
  } catch (err) {
    console.error(err);
  }
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  self.postMessage({ id, status, reason, headers: hdrs, body: buffer }, [buffer]);
};
