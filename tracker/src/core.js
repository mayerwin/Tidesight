/*! Tidesight tracker core | MIT License */
// Pageviews (including SPA routes), custom events, identity, sessions,
// engagement time, client context, batching and reliable delivery.
// Behaviour features live in plugins (src/plugins/*.js) loaded on demand.
// Rule: nothing in here may ever throw into the host page.
((w, d) => {
  'use strict';
  const VERSION = '0.1.0';
  const prev = w.tidesight;
  if (!d || (prev && prev.v)) return; // no DOM, or already loaded

  const nav = w.navigator || {};
  const loc = w.location;
  const now = () => Date.now();
  const noop = () => {};
  const enc = encodeURIComponent;
  const conf = w.tidesightConfig || {};
  const script = d.currentScript;
  const attr = (n) => (script ? script.getAttribute('data-' + n) : null);
  const debug = !!conf.debug || attr('debug') != null;
  const log = (...a) => { if (debug && w.console) console.log('[tidesight]', ...a); };
  const safe = (fn) => function () { try { return fn.apply(this, arguments); } catch (e) { log(e); } };
  const listen = (t, ev, fn, o) => t.addEventListener(ev, safe(fn), o);
  const siteId = String(conf.site || attr('site') || '');
  const base = String(conf.api || attr('api') || (script && script.src ? script.src.split(/[?#]/)[0].replace(/[^/]*$/, '') : ''))
    .replace(/\/+$/, '');
  const auto = String(conf.auto != null ? conf.auto : attr('auto')) !== 'false';
  const addScript = (src) => {
    const s = d.createElement('script');
    s.async = true;
    s.src = src;
    if (script && script.nonce) s.nonce = script.nonce;
    (d.head || d.documentElement).appendChild(s);
  };

  // Defaults until the site's configuration arrives. Plugins only load once it has.
  const DEF = {
    features: { pageviews: 1, spa: 1, engagement: 1, context: 1, not_found: 1 },
    opts: {}, session_timeout_min: 30, split_session_at_midnight: 1, timezone: 'UTC', cookie_domain: 'auto',
    cookie_days: 400, hash_mode: 'route', spa_replace: 'path', heartbeat_sec: 30, idle_sec: 300, flush_sec: 5, exclude_paths: []
  };
  let cfg = DEF;
  let cfgLoaded = 0;
  const merge = (c) => Object.assign({}, DEF, c, { features: Object.assign({}, DEF.features, c.features), opts: c.opts || {} });

  // ---- storage: localStorage and first-party cookies, each a backup of the other
  const ls = (() => { try { const s = w.localStorage; s.setItem('ts_t', 1); s.removeItem('ts_t'); return s; } catch (e) { return null; } })();
  const lsGet = (k) => { try { return ls && ls.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { if (ls) { if (v == null) ls.removeItem(k); else ls.setItem(k, v); } } catch (e) { /* full */ } };
  const ckGet = (k) => {
    try { const m = d.cookie.match('(?:^|;)\\s*' + k + '=([^;]*)'); return m ? decodeURIComponent(m[1]) : null; } catch (e) { return null; }
  };
  const ckWrite = (k, v, age, dom) => {
    d.cookie = k + '=' + v + ';path=/;max-age=' + age + ';samesite=lax' + (dom ? ';domain=' + dom : '') +
      (loc.protocol === 'https:' ? ';secure' : '');
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
  const endpoint = () => base + '/collect';

  const globRe = (p) => new RegExp('^' + p.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*') + '$');
  let exPath, exVal;
  const excluded = () => {
    if (exPath !== loc.pathname) exVal = (cfg.exclude_paths || []).some((p) => globRe(p).test(exPath = loc.pathname));
    return exVal;
  };
  const enqueue = (ev, fast, touched) => {
    if (silent || !started || excluded()) return;
    ev.id = rid(12);
    ev.ts = ev.ts || now();
    ev.pid = ev.pid || pid;
    ev.url = ev.url || loc.href;
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
  const flush = (beacon) => {
    if (!started || silent) return;
    clearTimeout(timer);
    timer = 0;
    while (queue.length) send(envelope(queue.splice(0, 50)), beacon);
  };
  const gz = (s) => {
    try {
      if (!w.CompressionStream || s.length < 1024) return Promise.resolve(s);
      return new Response(new Blob([s]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer()
        .then((b) => new Blob([b], { type: 'text/plain' }), () => s);
    } catch (e) { return Promise.resolve(s); }
  };
  const send = (env, beacon) => {
    const body = JSON.stringify(env);
    if (nav.onLine === false) return store(env);
    if (beacon || !w.fetch) {
      let ok = 0;
      try { ok = nav.sendBeacon(endpoint(), body); } catch (e) { /* no beacon */ }
      if (!ok) store(env);
      return;
    }
    gz(body)
      .then((b) => fetch(endpoint(), {
        method: 'POST', body: b, keepalive: (b.size || b.length * 3) < 6e4, credentials: cfg.server_cookie ? 'include' : 'omit'
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
  const retry = () => {
    if (nav.onLine === false || !started || silent) return;
    const q = readQ();
    if (!q.length) return;
    if (ls) lsSet('ts_q'); else memQ = [];
    q.forEach((env) => { env.sent = now(); send(env); });
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
    const ev = { t: 'pageview', url: pageUrl, title: d.title, ref: o.ref != null ? o.ref : d.referrer, nav: o.nav || navType(), sn: ses.sn, pn: ses.pn };
    if (fresh) ev.ns = 1;
    if (firstPv) {
      firstPv = 0;
      if (isNew) ev.new = 1;
      ev.idsrc = idSrc;
      ev.idc = idConflict;
    }
    if (canon && canon.href !== pageUrl) ev.canon = canon.href;
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
  const engaged = () => d.visibilityState !== 'hidden' && (!d.hasFocus || d.hasFocus()) && now() - lastAct < idleMs();
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
  const chunk = (kind, seq, t0, body, beacon, extra) => { // extra: more query parameters, e.g. '&rec=…'
    if (!started || silent || !ses) return;
    const url = base + '/chunk?site=' + enc(siteId) + '&k=' + kind + '&vid=' + enc(vis.id) + '&sid=' + enc(ses.id) +
      '&pid=' + enc(pid) + '&seq=' + seq + '&ts=' + t0 + '&url=' + enc(pageUrl.slice(0, 1000)) + (extra || '');
    try { if (beacon && nav.sendBeacon(url, body)) return; } catch (e) { /* use fetch */ }
    if (w.fetch) fetch(url, { method: 'POST', body, keepalive: !!beacon && (body.size || body.length * 3) < 6e4, credentials: 'omit' }).catch(noop);
  };
  const api = {
    v: VERSION, site: siteId, on, safe, log, rid, now, hash, gz, chunk, enabled,
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
  const props = (p) => {
    if (!p || typeof p !== 'object') return;
    const o = {};
    Object.keys(p).slice(0, 50).forEach((k) => { const v = p[k]; o[k.slice(0, 100)] = typeof v === 'string' ? v.slice(0, 500) : v; });
    try { return JSON.parse(JSON.stringify(o)); } catch (e) { /* not serialisable */ }
  };
  const str = (x, n) => String(x).slice(0, n);
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
      if (d.visibilityState === 'hidden') { leaving = 1; engagement('hidden'); emit('hide'); leaving = 0; flush(1); } else retry();
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
        if (d.prerendering) listen(d, 'prerenderingchange', () => pageview({ nav: 'prerender' }), { once: true });
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
  const q = (prev && prev.q) || [];
  w.tidesight = ts;
  if (lsGet('ts_ignore') === '1') silent = 1;
  safe(loadUad)();
  q.forEach((a) => ts(...a)); // may include a _config from an early config.js
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
