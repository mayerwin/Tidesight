// JS errors and unhandled rejections (feature errors) and failed resource loads (resource_errors), including those
// raised before the plugin loaded (kept by the core). Identical errors on a page (same k + msg + src + line) are sent
// once; when the page is hidden, left or changed (SPA), an update with the total count `n` and `upd: 1` follows for
// any error seen more than once. At most opts.errors.max_per_page distinct errors per page.
// URLs in messages and stacks get the redaction of every URL the tracker sends; a rejection with something other
// than an Error (a response, a data object…) is described by its type and message only, never sent whole.
import { register, listen } from '../lib/dom.js';
import { scrubText as scrubIn, errorReason as reason } from '../lib/scrub.js';

register('errors', (api) => {
  const js = api.enabled('errors'), resources = api.enabled('resource_errors');
  if (!js && !resources) return;
  const w = window, P = w.performance, max = +api.opt('errors', 'max_per_page', 25) || 25;
  let seen = {}, count = 0;

  const cut = api.cut || ((v, n) => String(v).slice(0, n)); // api.cut: core 0.2+ (an older core may be cached)
  const str = (v, n) => (v == null ? undefined : cut(v, n));
  const scrub = api.scrub || ((u) => u); // signed URLs and tokens in resource and script URLs
  const scrubText = (s) => (typeof s === 'string' ? scrubIn(api, s) : undefined);
  // Event time as epoch ms (events kept from before the plugin loaded happened earlier).
  const at = (e) => {
    const t = e.timeStamp, o = P && (P.timeOrigin || (P.timing && P.timing.navigationStart));
    return t > 1e12 ? Math.round(t) : o && t ? Math.round(o + t) : undefined;
  };
  const report = (ev, e) => {
    const key = [ev.k, ev.msg, ev.src || ev.res, ev.line].join('|'), s = seen[key];
    if (s) { s.n++; return; }
    if (count >= max) return;
    count++;
    seen[key] = { ev: Object.assign({}, ev), n: 1, sent: 1 };
    ev.ts = at(e);
    api.send('error', ev);
  };
  const onError = (e) => {
    const t = e.target;
    if (t && t !== w && t.nodeType === 1) { // a resource failed (img, script, link, media…)
      const u = t.currentSrc || t.src || t.href;
      if (resources) report({ k: 'resource', tag: t.tagName.toLowerCase(), res: str(scrub(typeof u === 'string' ? u : u && u.baseVal), 1000) }, e);
      return;
    }
    if (!js) return;
    if (e.type === 'unhandledrejection') {
      const r = e.reason;
      report({ k: 'rejection', msg: str(scrubText(reason(r)), 1000), stack: str(scrubText(r && r.stack), 4096) }, e);
    } else if (e.type === 'error') {
      const err = e.error;
      report({
        k: 'error', msg: str(scrubText(String(e.message || reason(err))), 1000), src: str(scrub(e.filename), 1000), line: e.lineno,
        col: e.colno, stack: str(scrubText(err && err.stack), 4096)
      }, e);
    }
  };
  listen(api, w, 'error', onError, true); // capture: resource errors do not bubble
  listen(api, w, 'unhandledrejection', onError, { passive: true });
  api.early().forEach(api.safe(onError));

  const counts = () => Object.keys(seen).forEach((k) => {
    const s = seen[k], ev = s.ev;
    if (s.n > s.sent) {
      s.sent = s.n;
      api.send('error', { k: ev.k, msg: ev.msg, src: ev.src, line: ev.line, col: ev.col, tag: ev.tag, res: ev.res, n: s.n, upd: 1 });
    }
  });
  api.on('hide', counts);
  api.on('leave', counts);
  api.on('route', counts);
  api.on('pageview', () => { seen = {}; count = 0; });
});
