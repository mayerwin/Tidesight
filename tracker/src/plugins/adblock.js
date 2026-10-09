// Ad-blocker detection (feature adblock): once per session, a bait element carrying class names that ad blockers
// hide is placed out of view; if it is hidden or removed ~100 ms later, an ad blocker is active.
// Sends `adblock` {ab: 1|0}. The session already reported is remembered in sessionStorage (ts_ab).
import { register } from '../lib/dom.js';

register('adblock', (api) => {
  if (!api.enabled('adblock')) return;
  const w = window, d = document, sid = api.sid();
  let ss = null;
  try { ss = w.sessionStorage; if (ss.getItem('ts_ab') === sid) return; } catch (e) { /* storage blocked */ }
  const run = api.safe(() => {
    const b = d.createElement('div');
    b.className = 'ad ads adsbox ad-banner ad-placement pub_300x250 textads banner-ads';
    b.setAttribute('aria-hidden', 'true');
    b.style.cssText = 'position:absolute;left:-9999px;top:-9999px;width:3px;height:3px';
    b.textContent = ' ';
    (d.body || d.documentElement).appendChild(b);
    setTimeout(api.safe(() => {
      let ab;
      try {
        const cs = w.getComputedStyle(b);
        ab = !d.documentElement.contains(b) || !b.offsetHeight || cs.display === 'none' || cs.visibility === 'hidden' ? 1 : 0;
      } finally {
        if (b.parentNode) b.parentNode.removeChild(b);
      }
      api.send('adblock', { ab });
      try { if (ss) ss.setItem('ts_ab', sid); } catch (e) { /* storage blocked */ }
    }), 100);
  });
  if (d.body) run(); else w.addEventListener('DOMContentLoaded', run);
});
