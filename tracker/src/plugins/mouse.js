// Mouse movement (feature mouse_moves, sampled per session): the pointer position (page coordinates) at most every
// opts.interval_ms while it moves (mouse and pen, not touch), sent to /chunk as kind "mouse":
// {pw, ph, vw, vh, pts: [[x, y, ms since the chunk's ts], …]}. A chunk is sent every 10 s or 500 points (gzip),
// and when the page is hidden or left (sendBeacon, uncompressed). Per page: seq restarts at 0 for each pid,
// at most opts.max_points points.
import { register, listen, pageSize } from '../lib/dom.js';
import { chunker, excluded } from '../lib/chunk.js';

register('mouse', (api) => {
  if (!api.enabled('mouse_moves')) return;
  const w = window, d = document, now = Date.now;
  const every = Math.max(16, +api.opt('mouse_moves', 'interval_ms', 100) || 100);
  const cap = +api.opt('mouse_moves', 'max_points', 3000) || 3000;
  const out = chunker(api, 'mouse');
  let pts = [], t0 = 0, seq = 0, total = 0, timer = 0, trail = 0, last = 0, x = -1, y = -1, lx = -1, ly = -1;

  // mode 0: compress and send; 1: send now, uncompressed (SPA route: the page id changes next); 2: beacon (unload)
  const flush = (mode) => {
    clearTimeout(timer);
    timer = 0;
    if (mode) out.drain(mode > 1);
    if (!pts.length) return;
    const ps = pageSize(), body = JSON.stringify({ pw: ps[0], ph: ps[1], vw: w.innerWidth, vh: w.innerHeight, pts });
    pts = [];
    if (mode) api.chunk('mouse', seq++, t0, body, mode > 1); else out.send(seq++, t0, body);
  };
  const sample = () => {
    trail = 0;
    last = now();
    if ((x === lx && y === ly) || total >= cap || excluded(api)) return;
    lx = x; ly = y;
    if (!pts.length) { t0 = last; timer = setTimeout(api.safe(() => flush(0)), 10000); }
    pts.push([x, y, last - t0]);
    if (++total >= cap || pts.length >= 500) flush(0);
  };
  listen(api, d, w.PointerEvent ? 'pointermove' : 'mousemove', (e) => {
    if (e.pointerType === 'touch') return;
    x = Math.round(e.pageX);
    y = Math.round(e.pageY);
    const wait = last + every - now();
    if (wait <= 0) sample(); else if (!trail) trail = setTimeout(api.safe(sample), wait);
  });
  api.on('hide', () => flush(2));
  api.on('leave', () => flush(2));
  api.on('route', () => flush(1));
  api.on('pageview', () => { seq = total = 0; lx = ly = -1; });
});
