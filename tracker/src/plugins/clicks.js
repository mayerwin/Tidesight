// Clicks (clicks, heatmap_clicks), link tracking (outbound, downloads, mailto/tel as outbound) and frustration
// signals (rage_clicks, dead_clicks, error_clicks). One capture listener on the document; nothing is ever read from
// a field the visitor types into. Elements inside .ts-no-track are ignored.
import { register, selector, text, closest, listen, pageSize, noTrack } from '../lib/dom.js';
import { scrubText, errorReason } from '../lib/scrub.js';

// What the visitor meant to click: the nearest interactive element, else the target itself.
const ACT = 'a[href],area[href],button,[role=button],[role=link],[role=checkbox],[role=menuitem],[role=tab],' +
  'input,select,textarea,summary,label,[onclick]';
// Elements that look clickable, for dead clicks (form fields react without DOM changes, so they are left out).
const LOOKS = 'a,button,[role=button],[role=link],input[type=submit],input[type=button],summary,label,[onclick]';

register('clicks', (api) => {
  const w = window, d = document, loc = location, now = Date.now, R = Math.round;
  // Switches are read once: the page keeps the session's sampling decisions.
  const [CLICKS, HEAT, RAGE, DEAD, ERRC, OUT, DL] = ['clicks', 'heatmap_clicks', 'rage_clicks', 'dead_clicks', 'error_clicks',
    'outbound', 'downloads'].map(api.enabled);
  const opt = (f, k, v) => +api.opt(f, k, v) || v;
  const textMax = opt('clicks', 'text_max', 100);
  const rageN = opt('rage_clicks', 'count', 3), rageMs = opt('rage_clicks', 'window_ms', 1000);
  const ragePx = opt('rage_clicks', 'radius_px', 30), deadMs = opt('dead_clicks', 'wait_ms', 1000);

  // Link kind: mail/tel/out (feature outbound) or dl (feature downloads).
  const link = (a) => {
    let u;
    try { u = new URL(typeof a.href === 'string' ? a.href : a.getAttribute('href'), loc.href); } catch (e) { return {}; }
    const p = u.protocol, ext = (/\.([a-z0-9]{1,8})$/i.exec(u.pathname) || [])[1];
    let k;
    if (p === 'mailto:') k = 'mail';
    else if (p === 'tel:') k = 'tel';
    else if (/^https?:$/.test(p)) {
      if (a.hasAttribute('download') || (ext && (api.cfg().download_extensions || []).indexOf(ext.toLowerCase()) >= 0)) k = 'dl';
      else if (u.host !== loc.host) k = 'out';
    }
    const href = api.scrub ? api.scrub(u.href) : u.href; // api.scrub, api.cut: core 0.2+
    return { href: api.cut ? api.cut(href, 1000) : href.slice(0, 1000), lk: k && (k === 'dl' ? DL : OUT) ? k : undefined,
      nav: /^https?:$/.test(p) };
  };

  // ---- rage clicks: rageN clicks within rageMs and ragePx of each other; one event per burst
  let recent = [], burst = null, bTimer = 0;
  const near = (c, x, y) => Math.hypot(c.x - x, c.y - y) <= ragePx;
  const endBurst = () => {
    clearTimeout(bTimer);
    const b = burst;
    burst = null;
    if (b) api.send('rage', { sel: b.sel, n: b.n, ms: b.t1 - b.t0, x: b.x, y: b.y });
  };
  const rage = (x, y, sel) => {
    const t = now();
    if (burst && t - burst.t1 <= rageMs && near(burst, x, y)) { burst.n++; burst.t1 = t; }
    else {
      endBurst();
      recent = recent.filter((c) => t - c.t <= rageMs && near(c, x, y));
      recent.push({ x, y, t, sel });
      if (recent.length < rageN) return;
      const f = recent[0];
      burst = { x: f.x, y: f.y, t0: f.t, t1: t, n: recent.length, sel: f.sel };
      recent = [];
    }
    clearTimeout(bTimer);
    bTimer = setTimeout(api.safe(endBurst), rageMs);
  };

  // ---- dead clicks: no DOM change, URL change, focus change, scroll, input or navigation within deadMs
  let pend = null, reactions = 0;
  const react = () => { reactions++; };
  const cancel = () => {
    if (pend) { clearTimeout(pend.timer); if (pend.mo) pend.mo.disconnect(); pend = null; }
  };
  const dead = (e, t, x, y) => {
    if (pend || (/^(INPUT|SELECT|TEXTAREA|OPTION)$/.test(t.tagName) && !/^(submit|button|reset|image)$/.test(t.type))) return;
    let c = closest(t, LOOKS);
    if (!c && w.getComputedStyle(t).cursor === 'pointer') c = t;
    if (!c) return;
    const r0 = reactions, href = loc.href, foc = d.activeElement, sx = w.pageXOffset, sy = w.pageYOffset, a = closest(t, 'a[href]');
    const p = pend = { mut: 0 };
    try {
      p.mo = new MutationObserver(() => { p.mut = 1; p.mo.disconnect(); }); // one change settles it
      p.mo.observe(d, { childList: true, subtree: true, attributes: true, characterData: true });
    } catch (x2) { /* no MutationObserver */ }
    p.timer = setTimeout(api.safe(() => {
      cancel();
      if (p.mut || reactions > r0 || loc.href !== href || d.activeElement !== foc || w.pageXOffset !== sx || w.pageYOffset !== sy) return;
      // A link the browser follows (new tab, download, slow page) has its effect elsewhere: not dead.
      const h = a && a.getAttribute('href');
      if (a && !e.defaultPrevented && h && !/^(#|javascript:)/i.test(h)) return;
      api.send('dead', { sel: selector(c), tag: c.tagName.toLowerCase(), x, y });
    }), deadMs);
  };

  // ---- error clicks: a JS error within 1 s after a click
  let last = null;
  const errc = (msg) => {
    const c = last;
    if (c && now() - c.t <= 1000) {
      last = null;
      api.send('errclick', { sel: c.sel, msg: api.cut(scrubText(api, String(msg)), 500) }); // the error plugin's rules
    }
  };

  // ---- the click itself
  let skip = null; // the click a <label> forwards to its control is the same click
  const click = (e, aux) => {
    let t = e.target;
    if (t && t.nodeType !== 1) t = t.parentElement;
    if (!t || !t.tagName || t === skip || noTrack(t)) return;
    const lab = closest(t, 'label');
    if (lab && lab.control && lab.control !== t) { skip = lab.control; setTimeout(() => { skip = null; }, 0); }
    const el = closest(t, ACT) || t, a = closest(t, 'a[href],area[href]'), l = a ? link(a) : {};
    const sel = selector(el), ptr = e.detail !== 0 || e.clientX || e.clientY; // keyboard and scripted clicks have no position
    const x = R(e.pageX), y = R(e.pageY);
    if (CLICKS || l.lk) {
      const ev = {
        sel, tag: el.tagName.toLowerCase(), txt: scrubText(api, text(el, textMax, el === t && !closest(t, ACT))), href: l.href, lk: l.lk,
        btn: e.button, mod: (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0) || undefined,
        vw: w.innerWidth, vh: w.innerHeight
      };
      if (ptr) {
        ev.x = x; ev.y = y; ev.vx = R(e.clientX); ev.vy = R(e.clientY);
        if (HEAT) {
          const b = el.getBoundingClientRect(), ps = pageSize();
          const rel = (v, s) => (s ? R(Math.min(1, Math.max(0, v / s)) * 1000) / 1000 : 0);
          ev.ex = rel(e.clientX - b.left, b.width); ev.ey = rel(e.clientY - b.top, b.height);
          ev.ew = R(b.width); ev.eh = R(b.height); ev.pw = ps[0]; ev.ph = ps[1];
        }
      } else ev.kb = 1;
      api.send('click', ev, !!l.lk);
    }
    if (aux) return;
    if (ptr && RAGE) rage(x, y, sel);
    if (DEAD && !(l.nav && (e.ctrlKey || e.metaKey || e.shiftKey || /^_blank$/i.test(a.target)))) {
      dead(e, t, ptr ? x : undefined, ptr ? y : undefined);
    }
    last = { sel, t: now() };
  };

  listen(api, d, 'click', (e) => click(e));
  listen(api, d, 'auxclick', (e) => { if (e.button === 1 && closest(e.target, 'a[href]')) click(e, 1); });
  if (DEAD) {
    ['scroll', 'input', 'change', 'submit', 'focusin'].forEach((t) => listen(api, d, t, react));
    ['blur', 'hashchange', 'popstate'].forEach((t) => listen(api, w, t, react, { passive: true }));
  }
  if (ERRC) {
    // Bubbling listeners on window: JS errors only (resource errors do not bubble).
    listen(api, w, 'error', (e) => errc(e.message || errorReason(e.error)), { passive: true });
    listen(api, w, 'unhandledrejection', (e) => errc(errorReason(e.reason)), { passive: true });
  }
  const done = () => { endBurst(); cancel(); };
  api.on('hide', done);
  api.on('leave', done);
});
