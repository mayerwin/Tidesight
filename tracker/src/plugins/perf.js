// Performance: Core Web Vitals (vitals), Navigation Timing (navigation_timing) and long tasks (long_tasks).
// Vitals follow the web-vitals library's definitions and thresholds and describe the initial page load only (not SPA
// soft navigations): they carry that page's pid and url. TTFB and FCP are sent as soon as known, LCP at the first
// input (or when hidden), CLS and INP when the page is first hidden or left; a later hide re-sends a value that
// changed, with upd: 1 (the last value per page wins).
import { register, selector, listen } from '../lib/dom.js';

const LIMITS = { LCP: [2500, 4000], CLS: [0.1, 0.25], INP: [200, 500], FCP: [1800, 3000], TTFB: [800, 1800] };
// Navigation Timing fields: name → [end, start] (a duration) or [time] (ms since navigation start).
const NT = {
  dns: ['domainLookupEnd', 'domainLookupStart'], tcp: ['connectEnd', 'connectStart'], ttfb: ['responseStart'],
  req: ['requestStart'], dl: ['responseEnd', 'responseStart'], dom: ['domInteractive'],
  dcl: ['domContentLoadedEventEnd'], load: ['loadEventEnd'], rt: ['redirectEnd', 'redirectStart']
};

register('perf', (api) => {
  const w = window, d = document, P = w.performance, PO = w.PerformanceObserver;
  if (!P || !P.getEntriesByType) return;
  const on = api.enabled, R = Math.round, pid = api.pid(), url = api.url();
  const nav = P.getEntriesByType('navigation')[0];
  const act = (nav && nav.activationStart) || 0, nt = nav ? (act > 0 ? 'prerender' : nav.type) : undefined;
  const types = (PO && PO.supportedEntryTypes) || [];
  const observe = (type, cb, o) => {
    if (types.indexOf(type) < 0) return;
    try {
      const po = new PO(api.safe((l) => cb(l.getEntries())));
      po.observe(Object.assign({ type, buffered: true }, o));
      po.f = () => cb(po.takeRecords()); // pending entries, before a final report
      return po;
    } catch (e) { /* unsupported option */ }
  };

  if (on('vitals')) {
    const sent = {};
    const vital = (n, val, el) => {
      if (!(val >= 0)) return;
      val = n === 'CLS' ? R(val * 1e4) / 1e4 : R(val);
      if (sent[n] === val) return;
      const t = LIMITS[n];
      api.send('vital', { n, val, r: val <= t[0] ? 'good' : val <= t[1] ? 'ni' : 'poor', el, nt, pid, url, upd: n in sent ? 1 : undefined });
      sent[n] = val;
    };
    const sel = (node) => { const el = node && (node.nodeType === 1 ? node : node.parentElement); return el ? selector(el) : undefined; };
    // Paints after the page was first hidden (e.g. a background tab) do not count.
    let hidden = d.visibilityState === 'hidden' ? 0 : Infinity;
    try { P.getEntriesByType('visibility-state').forEach((e) => { if (e.name === 'hidden') hidden = Math.min(hidden, e.startTime); }); } catch (e) { /* unsupported */ }

    if (nav && nav.responseStart > 0) vital('TTFB', Math.max(nav.responseStart - act, 0));

    observe('paint', (es) => es.forEach((e) => {
      if (e.name === 'first-contentful-paint' && e.startTime < hidden) vital('FCP', Math.max(e.startTime - act, 0));
    }));

    let lcp = null, lcpDone = 0;
    const lcpPo = observe('largest-contentful-paint', (es) => es.forEach((e) => {
      if (!lcpDone && e.startTime < hidden) lcp = [Math.max(e.startTime - act, 0), sel(e.element)];
    }));
    const lcpEnd = () => {
      if (lcpDone || !lcpPo) return;
      lcpPo.f();
      lcpDone = 1;
      lcpPo.disconnect();
      if (lcp) vital('LCP', lcp[0], lcp[1]);
    };
    ['keydown', 'click'].forEach((t) => listen(api, d, t, lcpEnd, { capture: true, once: true, passive: true }));

    // CLS: the largest session window (shifts < 1 s apart, window ≤ 5 s), ignoring shifts after recent input.
    let cls = 0, win = 0, first = 0, prev = 0, winEs = [], maxEs = [];
    const clsPo = observe('layout-shift', (es) => es.forEach((e) => {
      if (e.hadRecentInput) return;
      if (winEs.length && e.startTime - prev < 1000 && e.startTime - first < 5000) { win += e.value; winEs.push(e); }
      else { win = e.value; first = e.startTime; winEs = [e]; }
      prev = e.startTime;
      if (win > cls) { cls = win; maxEs = winEs; }
    }));
    const clsEl = () => {
      let big = null;
      maxEs.forEach((e) => { if (!big || e.value > big.value) big = e; });
      const src = big && (big.sources || []).filter((s) => s.node)[0];
      return src ? sel(src.node) : undefined;
    };

    // INP: worst interaction (by interactionId); with many interactions, one per 50 is ignored (≈ p98).
    let top = [], ids = {}, count = 0;
    const onEvent = (es) => es.forEach((e) => {
      const id = e.interactionId;
      if (!id) return;
      if (!ids[id]) { ids[id] = 1; count++; }
      let it = top.filter((x) => x.id === id)[0];
      if (!it) top.push(it = { id, d: 0 });
      if (e.duration > it.d) { it.d = e.duration; it.t = e.target; }
      top = top.sort((a, b) => b.d - a.d).slice(0, 10);
    });
    const evPo = observe('event', onEvent, { durationThreshold: 40 });
    const fiPo = observe('first-input', onEvent);

    const final = () => {
      lcpEnd();
      if (clsPo) { clsPo.f(); vital('CLS', cls, clsEl()); }
      [evPo, fiPo].forEach((po) => po && po.f());
      if (top.length) {
        const it = top[Math.min(top.length - 1, Math.floor((P.interactionCount || count) / 50))];
        vital('INP', it.d, sel(it.t));
      }
    };
    api.on('hide', () => { hidden = Math.min(hidden, P.now()); final(); });
    api.on('leave', final);
  }

  if (on('navigation_timing') && nav) {
    const send = () => {
      const e = P.getEntriesByType('navigation')[0] || nav, o = { nt, pid, url };
      Object.keys(NT).forEach((k) => {
        const f = NT[k], v = f[1] ? e[f[0]] - e[f[1]] : e[f[0]];
        if (v >= 0) o[k] = R(v);
      });
      if (e.secureConnectionStart > 0) o.tls = R(e.connectEnd - e.secureConnectionStart);
      if (act) o.act = R(act);
      o.rc = e.redirectCount;
      o.tsz = e.transferSize; // not `ts`: that is every event's client time
      o.ebs = e.encodedBodySize;
      o.dbs = e.decodedBodySize;
      o.proto = e.nextHopProtocol || undefined;
      const st = (e.serverTiming || []).slice(0, 10).map((s) => [s.name, s.duration, s.description || undefined]);
      if (st.length) o.srt = st;
      api.send('perf', o);
    };
    const later = () => setTimeout(api.safe(send), 0); // loadEventEnd is set once the load handlers have run
    if (d.readyState === 'complete') later(); else listen(api, w, 'load', later, { once: true, passive: true });
  }

  if (on('long_tasks')) {
    // Per page: count, total and longest duration, plus total blocking time (time over 50 ms); sent as the
    // tasks since the previous report when the page is hidden, left or changed.
    let n = 0, tot = 0, max = 0, tbt = 0;
    const po = observe('longtask', (es) => es.forEach((e) => {
      n++; tot += e.duration; tbt += Math.max(0, e.duration - 50); max = Math.max(max, e.duration);
    }));
    const report = () => {
      if (po) po.f();
      if (n) api.send('longtask', { n, tot: R(tot), max: R(max), tbt: R(tbt) });
      n = tot = max = tbt = 0;
    };
    api.on('hide', report);
    api.on('leave', report);
    api.on('route', report);
  }
});
