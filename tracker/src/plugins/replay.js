// Session replay (feature replay, sampled per session) and DOM snapshots for heatmap backgrounds (dom_snapshots),
// both recorded with rrweb (MIT), lazy-loaded from ts/rrweb-record.js next to this file.
//
// Replay: one recording per page load, id `rec`; SPA route changes continue it (same rec, seq keeps counting) and
// add an rrweb custom event "pageview" {pid, url}. Chunks of rrweb events (kind "replay", JSON array, gzip) go to
// /chunk every 10 s, at 200 events or 500 KB, and ~1 s after a full snapshot; when the page is hidden or left the
// rest goes out at once, uncompressed, in pieces of at most 48 KB: the first by sendBeacon, the others by fetch
// (browsers allow ~64 KB of beacons in flight per page, and the core's own events must fit too).
// Recording stops after opts.replay.max_minutes, and on an excluded path (restarting, as a new recording, on the
// next tracked page). Inputs are masked (all by default, passwords always), and so is the text of rich-text editors
// (contenteditable, role=textbox) unless mask_all_inputs is off; .ts-mask text is masked, .ts-block elements are
// blocked. URLs in the recorded page (links, images, form actions…) get the same redaction as every URL the
// tracker sends.
//
// DOM snapshots: once per page (path, plus the hash for #/ routes) per session, ~1.5 s after load or route, a
// chunk of kind "snapshot" holding [Meta, FullSnapshot] rrweb events. Taken with a short rrweb recording that stops
// at once, or, while a replay is recording, from a full snapshot taken in that recording (rrweb cannot run two
// recordings). Pages already snapshotted are remembered in sessionStorage (ts_snap).
import { register, idle, listen } from '../lib/dom.js';
import { chunker, excluded } from '../lib/chunk.js';
import { scrubText } from '../lib/scrub.js';

// Attributes holding URLs, redacted in full snapshots and mutations.
const URL_ATTRS = { href: 1, src: 1, action: 1, formaction: 1, poster: 1, data: 1, cite: 1, background: 1, ping: 1, longdesc: 1, 'xlink:href': 1 };
// Inside a masked subtree (an editor, .ts-mask), what a visitor put there can also be in attributes: links they
// pasted, image data, mentions. These are blanked; the elements themselves stay, for the layout.
const PRIVATE_ATTR = /^(?:href|src|srcset|alt|title|value|placeholder|xlink:href|aria-.*|data-.*)$/;

register('replay', (api) => {
  const w = window, d = document, rp = api.enabled('replay'), sn = api.enabled('dom_snapshots');
  // rrweb loads from the collector, through the core (api.load: core 0.3+), never from a URL read off the page.
  if ((!rp && !sn) || !api.load || !api.base) return;
  const o = (k, v) => api.opt('replay', k, v);
  const maskAll = o('mask_all_inputs', true) !== false, maskClass = String(o('mask_text_class', 'ts-mask'));
  const EDITORS = '[contenteditable]:not([contenteditable="false"]),[role="textbox"]';
  const config = () => ({
    emit: api.safe(emit), maskAllInputs: maskAll, maskInputOptions: { password: true },
    maskTextClass: maskClass, blockClass: o('block_class', 'ts-block'),
    // What visitors type into rich-text editors is text, not an input value: masked like inputs. mask_all_text
    // masks every text node (code editors that draw what is typed outside any input).
    maskTextSelector: o('mask_all_text', false) ? '*' : maskAll ? EDITORS : undefined,
    blockSelector: 'input[type=hidden]', // rrweb's input masking skips hidden fields (tokens): leave them out
    recordCanvas: !!o('record_canvas', false), sampling: { mousemove: 50, scroll: 150, input: 'last' },
    slimDOMOptions: 'all', checkoutEveryNms: 3e5, errorHandler: (e) => { api.log(e); return true; }
  });

  // rrweb: a UMD file setting the global rrwebRecord (the build makes it ignore AMD loaders on the page).
  const rr = () => { const r = w.rrwebRecord; return r && typeof r.record === 'function' ? r : null; };
  let R = rr(), waiting = null;
  const load = (cb) => {
    if (R) return cb(R);
    if (waiting) return waiting.push(cb);
    waiting = [cb];
    api.load(api.base + '/ts/rrweb-record.js?v=' + api.v, () => {
      R = rr();
      if (R) waiting.splice(0).forEach((f) => api.safe(f)(R));
    });
  };

  // URLs in the recorded DOM: the redaction of every URL the tracker sends (a reset link on the page, a form
  // posting to ?session_id=…, a signed image in a style). srcset holds several URLs, each with a descriptor.
  const css = (v) => v.replace(/url\(\s*(['"]?)([^'")]*)\1\s*\)/gi, (m, q, u) => 'url(' + q + api.scrub(u) + q + ')');
  const scrubValue = (n, v) => {
    if (n === 'srcset' || n === 'imagesrcset') return v.split(',').map((p) => p.replace(/^(\s*)(\S+)/, (m, s, u) => s + api.scrub(u))).join(',');
    if (URL_ATTRS[n] || n.slice(0, 5) === 'data-') return api.scrub(v);
    if (n === 'style' || n === '_csstext') return css(v);
    if (n === 'srcdoc') return css(scrubText(api, v));
    return v;
  };
  // Elements whose content the visitor typed (editors, when inputs are masked) or the site masked (.ts-mask):
  // inside them, private attributes go too.
  const masking = (a) => !!a && ((maskAll && (('contenteditable' in a && a.contenteditable !== 'false') || a.role === 'textbox')) ||
    (typeof a.class === 'string' && (' ' + a.class + ' ').indexOf(' ' + maskClass + ' ') >= 0));
  const scrubAttrs = (a, masked) => {
    if (!a || typeof a !== 'object') return;
    Object.keys(a).forEach((k) => {
      const v = a[k], n = k.toLowerCase();
      if (masked && PRIVATE_ATTR.test(n)) a[k] = '';
      else if (typeof v === 'string') a[k] = scrubValue(n, v);
      else if (n === 'style' && v && typeof v === 'object') { // a style mutation: {property: value | [value, priority]}
        Object.keys(v).forEach((p) => {
          if (typeof v[p] === 'string') v[p] = css(v[p]);
          else if (Array.isArray(v[p]) && typeof v[p][0] === 'string') v[p][0] = css(v[p][0]);
        });
      }
    });
  };
  // node id -> 1 for an element whose content is masked, 2 for a node inside one (from snapshots and mutations)
  let masks = new Map();
  const scrubTree = (root, inside) => {
    const stack = [[root, inside]];
    while (stack.length) {
      const [n, inMask] = stack.pop();
      if (!n || typeof n !== 'object') continue;
      const m = inMask || masking(n.attributes);
      if (m && n.id != null) masks.set(n.id, inMask ? 2 : 1);
      scrubAttrs(n.attributes, inMask);
      if (Array.isArray(n.childNodes)) for (let i = 0; i < n.childNodes.length; i++) stack.push([n.childNodes[i], m]);
    }
  };
  const scrubEvent = (e) => {
    const x = e.data;
    if (!x || !api.scrub) return;
    if (e.type === 4) x.href = api.scrub(x.href); // Meta: the page URL
    else if (e.type === 2) { masks = new Map(); scrubTree(x.node, false); } // FullSnapshot
    else if (e.type === 3 && x.source === 0) { // IncrementalSnapshot, Mutation
      (x.adds || []).forEach((m) => m && scrubTree(m.node, masks.has(m.parentId)));
      (x.attributes || []).forEach((m) => {
        if (!m) return;
        if (m.id != null && !masks.has(m.id) && masking(m.attributes)) masks.set(m.id, 1);
        scrubAttrs(m.attributes, masks.get(m.id) === 2);
      });
    }
  };

  const out = chunker(api, 'replay'), snaps = chunker(api, 'snapshot');
  let rec = '', stop = null, seq = 0, buf = [], size = 0, timer = 0, due = 0, maxT = 0, ended = 0, grab = null, te;
  const tag = () => '&rec=' + rec;

  const flush = () => {
    clearTimeout(timer);
    timer = 0;
    if (buf.length) out.send(seq++, buf[0][0], '[' + buf.map((b) => b[1]).join() + ']', tag());
    buf = [];
    size = 0;
  };
  const schedule = (ms) => {
    const t = Date.now() + ms;
    if (timer && due <= t) return;
    clearTimeout(timer);
    due = t;
    timer = setTimeout(api.safe(flush), ms);
  };
  const halt = () => { const s = stop; stop = null; clearTimeout(maxT); if (s) s(); };
  const end = () => { flush(); rec = ''; setTimeout(api.safe(halt), 0); }; // never stop rrweb from inside its emit

  function emit(e) {
    scrubEvent(e);
    if (grab && (e.type === 4 || e.type === 2)) grab.push(e); // Meta, FullSnapshot
    if (!rec) return;
    if (excluded(api)) return end(); // an SPA route into an excluded path: nothing from it is kept
    const s = JSON.stringify(e);
    buf.push([e.timestamp, s]);
    size += s.length;
    if (buf.length >= 200 || size >= 5e5) flush();
    else schedule(e.type === 2 ? 1000 : 10000); // the large full snapshot goes out soon, compressed
  }

  const start = () => {
    if (!rp || rec || ended || excluded(api)) return;
    load(() => {
      if (rec || stop || excluded(api)) return;
      rec = api.rid(12);
      seq = 0;
      try { stop = R.record(config()) || null; } catch (e) { api.log(e); }
      if (!stop) { rec = ''; buf = []; size = 0; return; }
      maxT = setTimeout(api.safe(() => { ended = 1; end(); }), (+o('max_minutes', 30) || 30) * 6e4);
    });
  };

  // Unload: everything at once, uncompressed; beacons within a byte budget, fetch for the rest.
  const unload = () => {
    clearTimeout(timer);
    timer = 0;
    let used = 0, piece = [], first = 0, n = 0;
    const bytes = (s) => { try { return (te = te || new TextEncoder()).encode(s).length; } catch (e) { return s.length * 3; } };
    const budget = (s) => { const k = bytes(s); return used + k <= 48e3 ? !!(used += k) : false; };
    out.drain(budget);
    snaps.drain(budget);
    const ship = () => {
      if (!piece.length) return;
      const body = '[' + piece.join() + ']';
      api.chunk('replay', seq++, first, body, budget(body), tag());
      piece = [];
      n = 0;
    };
    buf.forEach((b) => {
      const k = bytes(b[1]) + 1;
      if (n + k > 48e3) ship();
      if (!piece.length) first = b[0];
      piece.push(b[1]);
      n += k;
    });
    ship();
    buf = [];
    size = 0;
  };

  // DOM snapshot of the current page, once per page per session.
  let snapT = 0;
  const snap = () => {
    if (excluded(api)) return;
    const sid = api.sid(), k = api.hash(location.pathname + (/^#[!/]/.test(location.hash) ? location.hash : ''));
    let st = null;
    try { st = JSON.parse(w.sessionStorage.getItem('ts_snap')); } catch (e) { /* none */ }
    if (!st || st.s !== sid || !st.p) st = { s: sid, p: [] };
    if (st.p.indexOf(k) >= 0) return;
    load(() => {
      const evs = grab = [];
      try {
        if (stop) R.record.takeFullSnapshot(true);
        else if (!rec) { const s = R.record(config()); if (s) s(); }
      } catch (e) { api.log(e); }
      grab = null;
      if (evs.length < 2) return;
      st.p.push(k);
      try { w.sessionStorage.setItem('ts_snap', JSON.stringify(st)); } catch (e) { /* storage blocked */ }
      snaps.send(0, evs[0].timestamp, JSON.stringify(evs));
    });
  };
  const wantSnap = () => {
    if (!sn) return;
    clearTimeout(snapT);
    const later = () => { snapT = setTimeout(api.safe(() => idle(api.safe(snap), 3000)), 1500); };
    if (d.readyState === 'complete') later(); else listen(api, w, 'load', later, { once: true, passive: true });
  };

  api.on('pageview', () => {
    try {
      if (stop) R.record.addCustomEvent('pageview', { pid: api.pid(), url: api.scrub ? api.scrub(api.url()) : api.url() });
    } catch (e) { /* not recording */ }
    start();
    wantSnap();
  });
  api.on('route', () => snaps.drain(0));
  api.on('hide', unload);
  api.on('leave', unload);
  start();
  wantSnap();
});
