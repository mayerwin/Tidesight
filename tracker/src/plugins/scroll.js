// Scroll depth (feature scroll): depth % = (scrollY + viewport height) / document height. One `scroll` event per
// newly crossed milestone (opts.scroll.marks); when several are crossed at once (a short page at load, a jump to the
// end) only the highest is sent, the lower ones are implied. The maximum reached is added to engagement events as
// `sd`. Per page: reset on every pageview, including SPA route changes.
import { register, listen, pageSize } from '../lib/dom.js';

register('scroll', (api) => {
  if (!api.enabled('scroll')) return;
  const w = window;
  const marks = [].concat(api.opt('scroll', 'marks', [25, 50, 75, 90, 100])).map(Number).sort((a, b) => a - b);
  let max = 0, next = 0, raf = 0;
  const measure = () => {
    raf = 0;
    const ph = pageSize()[1], vh = w.innerHeight;
    if (!ph || !vh) return;
    const pct = Math.min(100, Math.round(((w.pageYOffset || 0) + vh) / ph * 100));
    if (pct > max) max = pct;
    let d;
    while (next < marks.length && marks[next] <= max) d = marks[next++];
    if (d != null) api.send('scroll', { d, max, ph, vh });
  };
  const later = () => {
    if (!raf) raf = w.requestAnimationFrame ? w.requestAnimationFrame(api.safe(measure)) : setTimeout(api.safe(measure), 50);
  };
  listen(api, w, 'scroll', later, { passive: true });
  listen(api, w, 'resize', later, { passive: true });
  listen(api, w, 'load', later, { passive: true });
  api.on('engagement', (ev) => { if (max) ev.sd = max; });
  api.on('pageview', () => { max = next = 0; later(); });
  later();
});
