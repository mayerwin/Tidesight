/*! Tidesight tracker core | MIT License */
// Pageviews (including SPA routes), custom events, identity, sessions,
// engagement time, client context, batching and reliable delivery.
// Behaviour features live in plugins (src/plugins/*.js) loaded on demand.
// Rule: nothing in here may ever throw into the host page.
((w, d) => {
  'use strict';
  const VERSION = '0.3.0';
  // Markup on the host page can shadow globals and document properties (DOM clobbering: <a id="tidesight">,
  // <img name="currentScript">…). Nothing read from them is trusted unless it has the expected type.
  const prev = w.tidesight;
  if (!d || (typeof prev === 'function' && prev.v)) return; // no DOM, or already loaded

  const nav = w.navigator || {};
  const loc = w.location;
  const now = () => Date.now();
  const noop = () => {};
  const enc = encodeURIComponent;
  const plain = (o) => Object.prototype.toString.call(o) === '[object Object]';
  const text = (v) => (typeof v === 'string' || typeof v === 'number' ? String(v) : '');
  const conf = plain(w.tidesightConfig) ? w.tidesightConfig : {};
  // Document properties through their own accessors: <img name="cookie"> cannot shadow those.
  const dprop = (n) => {
    try { for (let o = Object.getPrototypeOf(d); o; o = Object.getPrototypeOf(o)) { const p = Object.getOwnPropertyDescriptor(o, n); if (p) return p; } } catch (e) { /* old browser */ }
    return null;
  };
  const dget = (n) => { const p = dprop(n); try { return p && p.get ? p.get.call(d) : d[n]; } catch (e) { return undefined; } };
  const script = (() => { const s = dget('currentScript'); return w.HTMLScriptElement && s instanceof w.HTMLScriptElement ? s : null; })();
  const attr = (n) => (script ? script.getAttribute('data-' + n) : null);
  const debug = !!conf.debug || attr('debug') != null;
  const log = (...a) => { if (debug && w.console) console.log('[tidesight]', ...a); };
  const safe = (fn) => function () { try { return fn.apply(this, arguments); } catch (e) { log(e); } };
  const listen = (t, ev, fn, o) => t.addEventListener(ev, safe(fn), o);
  const siteId = text(conf.site) || attr('site') || '';
  // Where the collector is (and the plugins, which load from it as scripts): an http(s) URL, else nothing.
  const base = (() => {
    const src = script && script.src ? script.src.split(/[?#]/)[0] : '';
    const u = text(conf.api) || attr('api') || src.slice(0, src.lastIndexOf('/') + 1);
    try {
      const x = u ? new URL(u, loc.href) : null;
      return x && /^https?:$/.test(x.protocol) ? (x.origin + x.pathname).replace(/\/+$/, '') : '';
    } catch (e) { return ''; }
  })();
  const auto = String(conf.auto != null ? conf.auto : attr('auto')) !== 'false';
  const addScript = (src, onload) => {
    const s = d.createElement('script');
    s.async = true;
    s.referrerPolicy = 'origin';
    s.src = src;
    if (onload) s.onload = safe(onload);
    if (script && script.nonce) s.nonce = script.nonce;
    (d.head || d.documentElement).appendChild(s);
  };

  // Defaults until the site's configuration arrives. Plugins only load once it has.
  const DEF = {
    features: { pageviews: 1, spa: 1, engagement: 1, context: 1, not_found: 1 },
    opts: {}, session_timeout_min: 30, split_session_at_midnight: 1, timezone: 'UTC', cookie_domain: 'auto',
    cookie_days: 400, hash_mode: 'route', spa_replace: 'path', heartbeat_sec: 30, idle_sec: 300, flush_sec: 5, exclude_paths: [],
    redact_params: ['*token*', '*password*', '*passwd*', 'pwd', 'pass', '*secret*', 'key', '*_key', '*-key', '*api_key*', '*api-key*', '*apikey*',
      '*access_key*', '*private_key*', 'auth', '*authorization*', 'jwt', 'otp', '*mfa*', '*session*', '*sessid*', 'sid', 'ticket',
      '*signature*', 'sig', '*credential*'],
    redact_paths: []
  };
  let cfg = DEF;
  let cfgLoaded = 0;
  const merge = (c) => Object.assign({}, DEF, c, { features: Object.assign({}, DEF.features, c.features), opts: c.opts || {} });

  // ---- storage: localStorage and first-party cookies, each a backup of the other
  const ls = (() => { try { const s = w.localStorage; s.setItem('ts_t', 1); s.removeItem('ts_t'); return s; } catch (e) { return null; } })();
  const lsGet = (k) => { try { return ls && ls.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { if (ls) { if (v == null) ls.removeItem(k); else ls.setItem(k, v); } } catch (e) { /* full */ } };
  const ckGet = (k) => {
    try { const m = String(dget('cookie')).match('(?:^|;)\\s*' + k + '=([^;]*)'); return m ? decodeURIComponent(m[1]) : null; } catch (e) { return null; }
  };
  const ckWrite = (k, v, age, dom) => {
    const c = k + '=' + v + ';path=/;max-age=' + age + ';samesite=lax' + (dom ? ';domain=' + dom : '') +
      (loc.protocol === 'https:' ? ';secure' : ''), p = dprop('cookie');
    if (p && p.set) p.set.call(d, c); else d.cookie = c;
  };
  let ckDomain = null;
  const findDomain = () => {
    const s = cfg.cookie_domain, h = loc.hostname, p = h.split('.');
    if (s !== 'auto') return s || '';
    if (p.length < 2 || /^[\d.]+$|:/.test(h)) return '';
    for (let i = p.length - 2; i >= 0; i--) { // the shortest suffix a cookie sticks to is the registrable domain
      const dom = p.slice(i).join('.');
      ckWrite('ts_t', 1, 9, dom);
      if (ckGet('ts_t')) { ckWrite('ts_t', '', -1, dom); return dom; }
    }
    return '';
  };
  const put = (k, v) => { // cookie + localStorage
    try { if (ckDomain === null) ckDomain = findDomain(); ckWrite(k, enc(v), cfg.cookie_days * 86400, ckDomain); } catch (e) { /* blocked */ }
    lsSet(k, v);
  };

  const rid = (n) => {
    const a = new Uint8Array(n || 16), c = w.crypto;
    if (c && c.getRandomValues) c.getRandomValues(a); else for (let i = 0; i < a.length; i++) a[i] = Math.random() * 256;
    return Array.from(a, (x) => (x % 36).toString(36)).join('');
  };
  const hash = (s) => { // FNV-1a 32-bit
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  };
  // The first n UTF-16 units of x, never ending in half a character (a lone surrogate cannot be stored).
  const cut = (x, n) => {
    const s = String(x);
    if (s.length <= n) return s;
    const c = s.charCodeAt(n - 1);
    return s.slice(0, c >= 0xd800 && c <= 0xdbff ? n - 1 : n);
  };
  // Size in UTF-8 bytes, an upper bound (what counts against the collector's limits).
  const bytes = (s) => {
    let n = s.length;
    for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) > 127) n += 2;
    return n;
  };

  // ---- identity: the oldest id found in cookie, localStorage or the server cookie wins
  let vis = null, idSrc, idConflict, isNew = 0, firstPv = 1;
  let uid = lsGet('ts_uid') || undefined;
  const parseId = (s) => { const m = /^([\w-]{8,64})(?:\.(\d+))?$/.exec(s || ''); return m ? { id: m[1], t: +m[2] || 0 } : null; };
  const loadVisitor = () => {
    const c = parseId(ckGet('ts_vid')), l = parseId(lsGet('ts_vid'));
    const cands = [[c, 'cookie'], [l, 'ls'], [parseId(ckGet('ts_svid')), 'server']].filter((x) => x[0])
      .sort((a, b) => (a[0].t || 9e15) - (b[0].t || 9e15));
    if (cands.length) [vis, idSrc] = cands[0]; else { vis = { id: rid(20), t: now() }; idSrc = 'new'; isNew = 1; }
    if (c && l && c.id !== l.id) idConflict = { cookie: c.id, ls: l.id };
    put('ts_vid', vis.id + (vis.t ? '.' + vis.t : ''));
  };

  // ---- sessions: ts_ses = id.start.last.number.pageviews (base 36), shared by tabs
  let ses = null, dayFmt = null;
  const parseSes = (s) => {
    const p = (s || '').split('.');
    return p.length === 5 && /^[\w-]{8,64}$/.test(p[0])
      ? { id: p[0], start: parseInt(p[1], 36), last: parseInt(p[2], 36), sn: parseInt(p[3], 36) || 1, pn: parseInt(p[4], 36) || 0 }
      : null;
  };
  const readSes = () => {
    const a = parseSes(ckGet('ts_ses')), b = parseSes(lsGet('ts_ses'));
    return a && b ? (a.last >= b.last ? a : b) : a || b;
  };
  const day = (t) => {
    try { return (dayFmt = dayFmt || new Intl.DateTimeFormat('en-CA', { timeZone: cfg.timezone })).format(t); } catch (e) { return new Date(t).toDateString(); }
  };
  const touch = (isPv) => { // returns true when this activity starts a new session
    const t = now(), s = readSes() || ses;
    const fresh = !s || t - s.last > cfg.session_timeout_min * 60000 || (cfg.split_session_at_midnight && day(s.last) !== day(t));
    ses = fresh ? { id: rid(16), start: t, sn: s ? s.sn + 1 : 1, pn: 0 } : s;
    ses.last = t;
    if (isPv) ses.pn++;
    put('ts_ses', [ses.id, ses.start, ses.last, ses.sn, ses.pn].map((x, i) => (i ? x.toString(36) : x)).join('.'));
    return fresh;
  };

  // ---- hooks for plugins
  const hooks = {};
  const on = (n, f) => { (hooks[n] = hooks[n] || []).push(f); };
  const emit = (n, a) => (hooks[n] || []).forEach((f) => { try { f(a); } catch (e) { log(e); } });

  // ---- event queue and delivery
  let queue = [], timer = 0, due = 0, started = 0, silent = 0, dropped = 0, leaving = 0;
  let pid = rid(12), pageUrl = loc.href, lastUrl = pageUrl, pageSent = 0;
  const endpoint = () => base + '/collect?site=' + enc(siteId); // the site in the URL too: refusals before parsing are counted for it

  // ---- URL redaction: secrets in URLs (login links, reset tokens, signed URLs…) never leave the browser.
  // The rules of server/python/tidesight/redact.py, which the collector applies to the Referer it stores, and the
  // same test vectors. Globs (only * is special, ASCII case-insensitive) are matched in linear time: a hostile URL
  // must not freeze the page.
  const REDACTED = 'redacted', MAX_URL = 8000;
  const fold = (s) => s.replace(/[A-Z]+/g, (m) => m.toLowerCase());
  // Where each * of glob p matched in t, as [start, end] pairs, if p matches all of t; else null.
  const spans = (p, t) => {
    const parts = p.split('*'), head = parts[0], tail = parts[parts.length - 1], end = t.length - tail.length, out = [];
    if (parts.length < 2) return p === t ? out : null;
    if (end < head.length || t.slice(0, head.length) !== head || t.slice(end) !== tail) return null;
    let pos = head.length;
    for (let i = 1; i < parts.length - 1; i++) {
      const at = t.indexOf(parts[i], pos);
      if (at < 0 || at + parts[i].length > end) return null;
      out.push([pos, at]);
      pos = at + parts[i].length;
    }
    out.push([pos, end]);
    return out;
  };
  const strs = (l) => (Array.isArray(l) ? l.filter((p) => typeof p === 'string') : []);
  const decode = (s) => { try { return decodeURIComponent(s.replace(/\+/g, ' ')); } catch (e) { return s; } }; // malformed: as it is
  // names and globs: patterns already folded (rx below). depth: how deep in nested parameter values.
  const pairs = (s, sep, names, globs, depth) => s.split(sep).map((kv) => {
    const i = kv.indexOf('=');
    if (i < 1) return kv;
    const k = kv.slice(0, i), fk = k.length > 256 ? '' : fold(decode(k));
    if (names.length && (k.length > 256 || names.some((p) => spans(p, fk)))) return kv.slice(0, i + 1) + REDACTED;
    // A value that is itself a URL or a query with a secret (a login page's ?next=%2Freset%3Ftoken%3D…).
    return depth < 2 && secretInside(kv.slice(i + 1), names, globs, depth + 1) ? kv.slice(0, i + 1) + REDACTED : kv;
  }).join(sep);
  const secretInside = (v, names, globs, depth) => {
    const s = decode(v);
    return redact(s, names, globs, depth) !== s ||
      (s.indexOf('?') < 0 && s.indexOf('#') < 0 && s.indexOf('=') >= 0 && pairs(s, '&', names, globs, depth) !== s);
  };
  const redactPath = (path, names, globs, depth) => {
    if (path.indexOf(';') >= 0) { // path parameters: /shop;jsessionid=…
      path = path.split('/').map((s) => { const i = s.indexOf(';'); return i < 0 ? s : s.slice(0, i + 1) + pairs(s.slice(i + 1), ';', names, globs, depth); }).join('/');
    }
    const fp = globs.length ? fold(path) : '';
    for (let j = 0; j < globs.length; j++) {
      const sp = spans(globs[j], fp);
      if (sp) {
        for (let i = sp.length - 1; i >= 0; i--) if (sp[i][1] > sp[i][0]) path = path.slice(0, sp[i][0]) + REDACTED + path.slice(sp[i][1]);
        break;
      }
    }
    return path;
  };
  const redact = (u, names, globs, depth) => {
    const h = u.indexOf('#'), main = h < 0 ? u : u.slice(0, h), q = main.indexOf('?'), base = q < 0 ? main : main.slice(0, q);
    const c = base.indexOf(':');
    let start = -1, origin = '', path = base;
    if (c > 0 && /^[a-z][a-z\d+.-]*$/i.test(base.slice(0, c))) {
      if (!/^(?:https?|wss?)$/i.test(base.slice(0, c))) return u; // data:, mailto:, javascript:…
      if (base.slice(c, c + 3) === '://') start = c + 3;
    } else if (base.slice(0, 2) === '//') start = 2;
    if (start >= 0) {
      const j = base.indexOf('/', start);
      origin = j < 0 ? base : base.slice(0, j);
      path = j < 0 ? '' : base.slice(j);
      const at = origin.lastIndexOf('@');
      if (at > start) origin = origin.slice(0, start) + REDACTED + origin.slice(at); // user:password@
    }
    let out = origin + redactPath(path, names, globs, depth);
    if (q >= 0) out += '?' + pairs(main.slice(q + 1), '&', names, globs, depth);
    if (h >= 0) { // #a=b&c=d (OAuth implicit flow), a hash route #/path or #!/path, with or without ?query
      const f = u.slice(h + 1), fq = f.indexOf('?');
      let head = fq < 0 ? f : f.slice(0, fq);
      if (head[0] === '/' || head.slice(0, 2) === '!/') {
        const k = head[0] === '!' ? 1 : 0;
        head = head.slice(0, k) + redactPath(head.slice(k), names, globs, depth);
      } else if (fq < 0) {
        return out + '#' + (f.indexOf('=') > 0 ? pairs(f, '&', names, globs, depth) : f);
      }
      out += '#' + head + (fq < 0 ? '' : '?' + pairs(f.slice(fq + 1), '&', names, globs, depth));
    }
    return out;
  };
  const redactUrl = (u, names, globs) => (typeof u !== 'string' || !u || (!names.length && !globs.length) ? u
    : redact(u, names.map(fold), globs.map(fold), 0));
  // A URL cut to MAX_URL characters, without half a %-escape at the end (it would hide what follows from decoding).
  const cutUrl = (u) => (typeof u === 'string' && u.length > MAX_URL ? cut(u, MAX_URL).replace(/%[\da-f]?$/i, '') : u);
  let scrubIn, scrubOut, rx = {};
  const scrub = (u) => { // with the site's patterns; the last answer is kept (the page URL goes in every event)
    if (rx.cfg !== cfg) rx = { cfg, names: strs(cfg.redact_params).map(fold), globs: strs(cfg.redact_paths).map(fold) };
    u = cutUrl(u);
    if (u !== scrubIn || !rx.fresh) {
      scrubIn = u;
      rx.fresh = 1;
      scrubOut = typeof u !== 'string' || !u || (!rx.names.length && !rx.globs.length) ? u
        : cut(redact(u, rx.names, rx.globs, 0), MAX_URL); // "redacted" can be longer than what it replaced
    }
    return scrubOut;
  };
  // exclude_paths: globs (case-sensitive) on the path, and on a hash route's path (#/admin/…).
  let exKey, exVal, exCfg;
  const excluded = () => {
    const hr = /^#!?(\/[^?]*)/.exec(loc.hash), key = loc.pathname + (hr ? '#' + hr[1] : '');
    if (exKey !== key || exCfg !== cfg) {
      exKey = key;
      exCfg = cfg;
      exVal = strs(cfg.exclude_paths).some((p) => spans(p, loc.pathname) || (hr && spans(p, hr[1])));
    }
    return exVal;
  };
  // A page calling track() in a loop must not flood the collector: at most MAX_EVENTS_PER_MINUTE events a
  // minute leave this page; the rest are counted and reported in the next batch (`drop`). URLs are cut at
  // MAX_URL characters by scrub() (a crafted link with a huge #fragment would otherwise make every event too large
  // to send, and slow to redact).
  const MAX_EVENTS_PER_MINUTE = 300;
  let rateMinute = 0, rateCount = 0;
  const overRate = () => {
    const m = Math.floor(now() / 6e4);
    if (m !== rateMinute) { rateMinute = m; rateCount = 0; }
    return ++rateCount > MAX_EVENTS_PER_MINUTE;
  };
  const enqueue = (ev, fast, touched) => {
    if (silent || !started || excluded()) return;
    if (overRate()) { dropped++; return; }
    ev.id = rid(12);
    ev.ts = ev.ts || now();
    ev.pid = ev.pid || pid;
    ev.url = scrub(ev.url || loc.href);
    if (!touched && touch()) ev.ns = 1;
    ev._s = ses.id;
    emit('event', ev);
    queue.push(ev);
    if (queue.length >= 20 && !leaving) flush(); else schedule(fast ? 150 : cfg.flush_sec * 1000);
  };
  const schedule = (ms) => {
    const t = now() + ms;
    if (timer && due <= t) return;
    clearTimeout(timer);
    due = t;
    timer = setTimeout(safe(() => { timer = 0; flush(); }), ms);
  };
  const envelope = (evs) => {
    const sid = ses.id, env = {
      v: 1, site: siteId, vid: vis.id, vt: vis.t || undefined, sid, uid, sent: now(), lib: 'ts.js/' + VERSION,
      try: 0, drop: dropped || undefined,
      events: evs.map((e) => {
        const c = Object.assign({}, e);
        delete c._s;
        if (e._s !== sid) c.sid = e._s;
        return c;
      })
    };
    dropped = 0;
    return env;
  };
  // Batches of at most 50 events and maxBody() bytes: under the collector's limit (`max_kb` in its config)
  // and the browsers' ~64 KB allowance for beacons and keepalive requests. An event too large to ever be
  // accepted is dropped (and counted) instead of taking its whole batch down with it.
  const maxBody = () => Math.min(60, +cfg.max_kb > 0 ? +cfg.max_kb : 64) * 1000 - 1000;
  const flush = (beacon) => {
    if (!started || silent) return;
    clearTimeout(timer);
    timer = 0;
    const max = maxBody();
    let batch = [], size = 0;
    const out = () => { if (batch.length) send(envelope(batch), beacon); batch = []; size = 0; };
    queue.splice(0).forEach((ev) => {
      let n;
      try { n = bytes(JSON.stringify(ev)); } catch (e) { n = Infinity; }
      if (n > max) { dropped++; return; }
      if (batch.length >= 50 || size + n > max) out();
      batch.push(ev);
      size += n + 1;
    });
    out();
  };
  const gz = (s) => {
    try {
      if (!w.CompressionStream || s.length < 1024) return Promise.resolve(s);
      return new Response(new Blob([s]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer()
        .then((b) => new Blob([b], { type: 'text/plain' }), () => s);
    } catch (e) { return Promise.resolve(s); }
  };
  // Requests that must survive the page (hidden, left): a keepalive fetch, which sends only the origin as
  // Referer, else a beacon (which sends what the page's referrer policy allows). Browsers allow ~64 KB of them in
  // flight per page; past that (counted here) the answer is false and the caller keeps the data for later.
  let kaBytes = 0;
  const KEEPALIVE = !!(w.Request && 'keepalive' in w.Request.prototype);
  const unloadSend = (url, body, creds, failed) => {
    const n = body.size || bytes(body);
    if (KEEPALIVE && kaBytes + n <= 6e4) {
      try {
        kaBytes += n;
        const done = () => { kaBytes -= n; };
        fetch(url, { method: 'POST', body, keepalive: true, mode: 'no-cors', credentials: creds ? 'include' : 'omit', referrerPolicy: 'origin' })
          .then(done, () => { done(); if (failed) failed(); });
        return true;
      } catch (e) { kaBytes -= n; }
    }
    try { return !!nav.sendBeacon(url, body); } catch (e) { return false; }
  };
  const send = (env, beacon) => {
    const body = JSON.stringify(env);
    if (nav.onLine === false) return store(env);
    if (beacon || !w.fetch) {
      if (!unloadSend(endpoint(), body, cfg.server_cookie, () => store(env))) store(env);
      return;
    }
    gz(body)
      .then((b) => fetch(endpoint(), {
        method: 'POST', body: b, keepalive: (b.size || b.length * 3) < 6e4, credentials: cfg.server_cookie ? 'include' : 'omit',
        referrerPolicy: 'origin' // the page URL travels in the payload (redacted); never in a Referer header
      }))
      .then((r) => { if (r.status === 429 || r.status >= 500) store(env); }, () => store(env));
  };

  // Undelivered batches wait in localStorage (bounded) and are retried with backoff.
  let memQ = [], rTimer = 0;
  const readQ = () => { try { return ls ? JSON.parse(lsGet('ts_q')) || [] : memQ; } catch (e) { return []; } };
  const store = (env) => {
    if (++env.try > 10) { dropped++; return; }
    const q = readQ();
    q.push(env);
    while (q.length > 1 && (q.length > 50 || JSON.stringify(q).length > 4e5)) { q.shift(); dropped++; }
    if (ls) lsSet('ts_q', JSON.stringify(q)); else memQ = q;
    clearTimeout(rTimer);
    rTimer = setTimeout(safe(retry), Math.min(3e5, 1000 * 2 ** env.try));
  };
  // A few batches at a time, so a backlog (after being offline, say) does not hit the collector all at once.
  const retry = () => {
    if (nav.onLine === false || !started || silent) return;
    const q = readQ();
    if (!q.length) return;
    const now5 = q.splice(0, 5);
    if (ls) lsSet('ts_q', q.length ? JSON.stringify(q) : null); else memQ = q;
    now5.forEach((env) => { env.sent = now(); send(env); });
    if (q.length) { clearTimeout(rTimer); rTimer = setTimeout(safe(retry), 2000); }
  };

  // ---- client context
  let uad, uadReady = Promise.resolve();
  const loadUad = () => {
    const u = nav.userAgentData;
    if (!u) return;
    try { uad = JSON.parse(w.sessionStorage.getItem('ts_uad')); } catch (e) { /* blocked */ }
    if (uad) return;
    uad = { brands: u.brands, mobile: u.mobile, platform: u.platform };
    if (u.getHighEntropyValues) {
      uadReady = u.getHighEntropyValues(['platformVersion', 'model', 'architecture', 'bitness', 'fullVersionList',
        'formFactors', 'wow64', 'uaFullVersion']).then((v) => {
        uad = v;
        try { w.sessionStorage.setItem('ts_uad', JSON.stringify(v)); } catch (e) { /* blocked */ }
      }, noop);
    }
  };
  const mm = (q) => { try { return w.matchMedia(q).matches; } catch (e) { return false; } };
  const pick = (list, q) => list.find((v) => mm('(' + q + ': ' + v + ')'));
  const context = () => {
    const s = w.screen || {}, c = nav.connection || {};
    let tz;
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { /* old browser */ }
    return {
      sw: s.width, sh: s.height, saw: s.availWidth, sah: s.availHeight, vw: w.innerWidth, vh: w.innerHeight,
      dpr: w.devicePixelRatio, cd: s.colorDepth, or: s.orientation && s.orientation.type,
      lang: nav.language, langs: nav.languages && [].slice.call(nav.languages, 0, 10), tz, tzo: new Date().getTimezoneOffset(),
      cs: pick(['dark', 'light'], 'prefers-color-scheme'), rm: mm('(prefers-reduced-motion: reduce)') ? 1 : 0,
      ct: pick(['more', 'less', 'custom'], 'prefers-contrast') || 'no-preference', fc: mm('(forced-colors: active)') ? 1 : 0,
      tp: nav.maxTouchPoints, touch: 'ontouchstart' in w ? 1 : 0, hc: nav.hardwareConcurrency, dm: nav.deviceMemory,
      net: c.effectiveType ? { type: c.type, eff: c.effectiveType, dl: c.downlink, rtt: c.rtt, sd: c.saveData ? 1 : 0 } : undefined,
      ck: nav.cookieEnabled ? 1 : 0, ls: ls ? 1 : 0, pwa: pick(['standalone', 'fullscreen', 'minimal-ui'], 'display-mode') || 'browser',
      ifr: w.top !== w.self ? 1 : 0, ua: nav.userAgent,
      dnt: nav.doNotTrack || w.doNotTrack || undefined, gpc: nav.globalPrivacyControl, uad
    };
  };

  // ---- pageviews
  const navType = () => {
    try { const e = performance.getEntriesByType('navigation')[0]; return e.activationStart > 0 ? 'prerender' : e.type; } catch (e) { return 'navigate'; }
  };
  const pageview = (o) => {
    o = o || {};
    if (!cfg.features.pageviews || excluded()) return;
    if (pageSent) { engagement('route'); emit('route'); }
    pid = rid(12);
    pageSent = 1;
    pageUrl = lastUrl = loc.href;
    resetEngagement();
    const fresh = touch(1), canon = d.querySelector('link[rel=canonical]'), st = d.querySelector('meta[name="ts:status"]');
    const ev = { t: 'pageview', url: pageUrl, title: text(dget('title')), ref: scrub(o.ref != null ? o.ref : text(dget('referrer'))), nav: o.nav || navType(), sn: ses.sn, pn: ses.pn };
    if (fresh) ev.ns = 1;
    if (firstPv) {
      firstPv = 0;
      if (isNew) ev.new = 1;
      ev.idsrc = idSrc;
      ev.idc = idConflict;
    }
    if (canon && typeof canon.href === 'string' && canon.href !== pageUrl) ev.canon = scrub(canon.href);
    ev.lang = d.documentElement.lang || undefined;
    if (st && cfg.features.not_found) ev.st = +st.content || st.content;
    ev.props = o.props;
    if (cfg.features.context) ev.ctx = context();
    emit('pageview', ev);
    enqueue(ev, 1, 1);
    loadPlugins(); // sampling depends on the session, which now exists
  };

  // SPA route changes: History API and #hash routes, debounced so redirects count once.
  let rTimeout = 0, onlyReplace = 1;
  const route = (kind) => {
    if (kind !== 'replaceState') onlyReplace = 0;
    clearTimeout(rTimeout);
    rTimeout = setTimeout(safe(onRoute), 100);
  };
  const isRoute = (h) => /^#[!/]/.test(h);
  const onRoute = () => {
    const replaceOnly = onlyReplace, url = loc.href, prevUrl = lastUrl;
    onlyReplace = 1;
    if (url === prevUrl) return;
    const a = new URL(prevUrl), b = new URL(url);
    let kind = 'spa', skip;
    if (a.pathname + a.search === b.pathname + b.search) {
      skip = cfg.hash_mode === 'off' || (cfg.hash_mode === 'route' && !isRoute(a.hash) && !isRoute(b.hash));
      kind = 'hash';
    } else {
      skip = !cfg.features.spa || (replaceOnly && (cfg.spa_replace === 'off' || (cfg.spa_replace === 'path' && a.pathname === b.pathname)));
    }
    if (skip) lastUrl = url; else pageview({ nav: kind, ref: prevUrl });
  };
  const hookHistory = () => {
    const h = w.history;
    if (h && h.pushState) {
      ['pushState', 'replaceState'].forEach((m) => {
        const orig = h[m];
        h[m] = function () { const r = orig.apply(this, arguments); safe(route)(m); return r; };
      });
    }
    listen(w, 'popstate', () => route('pop'));
    listen(w, 'hashchange', () => route('hash'));
  };

  // ---- engagement: time while the page is visible, focused and not idle
  let engMs = 0, engFrom = 0, engSent = 0, lastAct = now();
  const idleMs = () => cfg.idle_sec * 1000;
  const hidden = () => dget('visibilityState') === 'hidden';
  const engaged = () => !hidden() && (!d.hasFocus || d.hasFocus()) && now() - lastAct < idleMs();
  const engTick = () => {
    const t = now();
    if (engFrom) engMs += Math.max(0, Math.min(t, lastAct + idleMs()) - engFrom);
    engFrom = engaged() ? t : 0;
  };
  const activity = () => { // input after an idle period resumes counting
    const idle = now() - lastAct >= idleMs();
    lastAct = now();
    if (idle) engTick();
  };
  const resetEngagement = () => { engMs = engSent = 0; engFrom = engaged() ? now() : 0; };
  const engagement = (why) => {
    if (!cfg.features.engagement || !pageSent) return;
    engTick();
    const ms = engMs - engSent;
    if (ms < (why === 'hb' ? 1000 : 1)) return;
    engSent = engMs;
    const ev = { t: 'engagement', ms, tot: engMs, why, url: pageUrl };
    emit('engagement', ev);
    enqueue(ev);
  };

  // Errors raised before the errors plugin loads are kept for it.
  const early = [];
  const grab = (e) => { if (early.length < 20) early.push(e); };
  w.addEventListener('error', grab, true);
  w.addEventListener('unhandledrejection', grab);

  // ---- plugins: each covers a set of features and loads when one of them is on
  const PLUGINS = {
    fp: ['fingerprint', 'bot_signals'],
    clicks: ['clicks', 'outbound', 'downloads', 'heatmap_clicks', 'rage_clicks', 'dead_clicks', 'error_clicks'],
    scroll: ['scroll'], forms: ['forms'], perf: ['vitals', 'navigation_timing', 'long_tasks'],
    errors: ['errors', 'resource_errors'], ux: ['resize', 'visibility', 'copy_paste', 'text_selection'],
    mouse: ['mouse_moves'], replay: ['replay', 'dom_snapshots'], adblock: ['adblock']
  };
  const plugins = {}, inited = {}, loading = {};
  const sampled = (f) => { // decided per session, so a session is entirely in or out
    const o = cfg.opts[f], r = o && o.sample_rate != null ? o.sample_rate : 1, s = ses || readSes();
    return r >= 1 || (!!s && hash(s.id + ':' + f) / 4294967296 < r);
  };
  const enabled = (f) => !!(cfg.features[f] && sampled(f));
  const wanted = (n) => !PLUGINS[n] || PLUGINS[n].some(enabled);
  const initPlugin = (n) => { // after the first pageview (its page id), so never on an excluded page
    if (inited[n] || !started || silent || (!pageSent && cfg.features.pageviews) || !wanted(n)) return;
    inited[n] = 1;
    try { plugins[n](api); } catch (e) { log(e); }
  };
  const loadPlugins = () => {
    if (cfgLoaded && !silent) {
      Object.keys(PLUGINS).forEach((n) => {
        if (plugins[n]) initPlugin(n);
        else if (wanted(n) && !loading[n] && base) { loading[n] = 1; addScript(base + '/ts/' + n + '.js?v=' + VERSION); }
      });
    }
  };
  // Heavy uploads (replay, mouse, snapshots): at most MAX_CHUNKS_PER_MINUTE a minute from this page, well
  // under the collector's per-client allowance, whatever a plugin does.
  const MAX_CHUNKS_PER_MINUTE = 60;
  let chunkMinute = 0, chunkCount = 0;
  const chunk = (kind, seq, t0, body, beacon, extra) => { // extra: more query parameters, e.g. '&rec=…'
    if (!started || silent || !ses) return;
    const m = Math.floor(now() / 6e4);
    if (m !== chunkMinute) { chunkMinute = m; chunkCount = 0; }
    if (++chunkCount > MAX_CHUNKS_PER_MINUTE) return;
    const url = base + '/chunk?site=' + enc(siteId) + '&k=' + kind + '&vid=' + enc(vis.id) + '&sid=' + enc(ses.id) +
      '&pid=' + enc(pid) + '&seq=' + seq + '&ts=' + t0 + '&url=' + enc(cut(scrub(pageUrl), 1000)) + (extra || '');
    if (beacon && unloadSend(url, body)) return;
    if (w.fetch) fetch(url, { method: 'POST', body, credentials: 'omit', referrerPolicy: 'origin' }).catch(noop);
  };
  const api = {
    v: VERSION, site: siteId, base, on, safe, log, rid, now, hash, gz, chunk, enabled, cut, scrub, excluded,
    redact: (u, names, globs) => redactUrl(u, strs(names), strs(globs)),
    load: addScript, // a script from the collector (base + '/ts/…'), with the tracker's nonce
    cfg: () => cfg,
    opt: (f, k, def) => { const o = cfg.opts[f]; return o && o[k] != null ? o[k] : def; },
    send: (t, data, fast) => enqueue(Object.assign({ t }, data), fast),
    pid: () => pid,
    sid: () => ses && ses.id,
    vid: () => vis && vis.id,
    url: () => pageUrl,
    early: () => {
      w.removeEventListener('error', grab, true);
      w.removeEventListener('unhandledrejection', grab);
      return early.splice(0);
    },
    flush: () => flush()
  };

  // ---- public commands
  // Properties: at most 50 keys (100 characters each), string values cut at 500 characters, nested values
  // (ecommerce items) kept while the whole stays under MAX_PROPS characters of JSON, else dropped (_trunc: 1).
  const MAX_PROPS = 24000;
  const props = (p) => {
    if (!p || typeof p !== 'object') return;
    const o = {};
    Object.keys(p).slice(0, 50).forEach((k) => { const v = p[k]; o[cut(k, 100)] = typeof v === 'string' ? cut(v, 500) : v; });
    let j;
    try { j = JSON.stringify(o); } catch (e) { return; } // not serialisable (a cycle, a BigInt…)
    if (j.length > MAX_PROPS) {
      Object.keys(o).forEach((k) => { if (o[k] && typeof o[k] === 'object') delete o[k]; });
      o._trunc = 1;
      j = JSON.stringify(o);
    }
    return JSON.parse(j);
  };
  const str = cut;
  const cmds = {
    _config: (c) => {
      if (!c || typeof c !== 'object') return;
      cfg = merge(c);
      cfgLoaded = 1;
      dayFmt = null;
      lsSet('ts_cfg_' + siteId, JSON.stringify({ at: now(), c }));
      if (started) loadPlugins(); else start();
    },
    _plugin: (n, fn) => {
      if (typeof fn !== 'function') return;
      plugins[n] = fn;
      if (cfgLoaded) initPlugin(n);
    },
    track: (n, p, val) => { if (n != null && n !== '') enqueue({ t: 'event', n: str(n, 200), p: props(p), val: +val === val ? val : undefined }); },
    page: (p) => pageview({ props: props(p) }),
    identify: (id, traits) => {
      if (id == null || id === '') return;
      flush();
      lsSet('ts_uid', uid = str(id, 200));
      enqueue({ t: 'identify', uid, traits: props(traits) });
    },
    reset: () => { flush(); uid = undefined; lsSet('ts_uid'); },
    search: (q, n, cat) => enqueue({ t: 'search', q: str(q, 200), n, cat }),
    status: (st) => enqueue({ t: 'status', st }),
    ignore: (off) => { silent = off !== false; lsSet('ts_ignore', silent ? 1 : null); },
    flush: () => flush()
  };
  const pending = [];
  const run = (args) => {
    const name = args[0], fn = cmds[name];
    if (!fn) return log('?', name);
    if (!started && name[0] !== '_') return pending.push(args);
    fn(...[].slice.call(args, 1));
  };

  // ---- startup
  let cfgTimer = 0;
  const start = () => {
    if (started) return;
    clearTimeout(cfgTimer);
    started = 1;
    if (silent || (cfg.dnt && (nav.doNotTrack === '1' || w.doNotTrack === '1')) || (cfg.gpc && nav.globalPrivacyControl === true)) {
      silent = 1;
      return;
    }
    loadVisitor();
    listen(d, 'visibilitychange', () => {
      engTick();
      if (hidden()) { leaving = 1; engagement('hidden'); emit('hide'); leaving = 0; flush(1); } else retry();
    });
    listen(w, 'pagehide', () => { leaving = 1; engagement('leave'); emit('leave'); leaving = 0; flush(1); });
    listen(w, 'pageshow', (e) => { if (e.persisted && pageSent) pageview({ nav: 'restore' }); });
    listen(w, 'focus', engTick);
    listen(w, 'blur', engTick);
    listen(w, 'online', retry);
    ['mousemove', 'keydown', 'scroll', 'pointerdown', 'touchstart', 'wheel'].forEach((e) => listen(d, e, activity, { passive: true, capture: true }));
    if (cfg.heartbeat_sec > 0) setInterval(safe(() => { if (engaged()) engagement('hb'); }), cfg.heartbeat_sec * 1000);
    const go = safe(() => {
      if (auto) {
        if (dget('prerendering') === true) listen(d, 'prerenderingchange', () => pageview({ nav: 'prerender' }), { once: true });
        else pageview();
      }
      pending.splice(0).forEach(run);
    });
    if (auto) hookHistory();
    Promise.race([uadReady, new Promise((r) => setTimeout(r, 100))]).then(go, go);
    loadPlugins();
    setTimeout(safe(retry), 1000);
  };

  const ts = function () { try { run(arguments); } catch (e) { log(e); } };
  ts.v = VERSION;
  Object.keys(cmds).forEach((k) => { if (k[0] !== '_') ts[k] = (...a) => ts(k, ...a); });
  const q = typeof prev === 'function' && Array.isArray(prev.q) ? prev.q : [];
  w.tidesight = ts;
  if (lsGet('ts_ignore') === '1') silent = 1;
  safe(loadUad)();
  q.forEach((a) => { try { ts.apply(null, a); } catch (e) { log(e); } }); // may include a _config from an early config.js
  if (!cfgLoaded) {
    safe(() => {
      let cached;
      try { cached = JSON.parse(lsGet('ts_cfg_' + siteId)); } catch (e) { /* none */ }
      if (cached && cached.c && now() - cached.at < 864e5) {
        cmds._config(cached.c);
        if (now() - cached.at > 3e5 && base) addScript(base + '/config.js?site=' + enc(siteId));
      } else {
        if (base) addScript(base + '/config.js?site=' + enc(siteId));
        cfgTimer = setTimeout(safe(start), 3000);
      }
    })();
  }
})(window, document);
