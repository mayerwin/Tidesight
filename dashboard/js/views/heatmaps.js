// Heatmaps: clicks (and rage/dead clicks), mouse movement and scroll reach over a page.
// With a DOM snapshot (dom_snapshots feature) clicks are placed on the element they hit, using the stored selector and
// the position inside the element, so the map survives layout changes. Otherwise the live page is framed
// (sandboxed, scripts off: viewing never records a visit) and clicks are placed by page coordinates.
import { card, cssVar, el, table } from '../charts.js';
import * as fmt from '../fmt.js';
import { loadPlayer, sanitizeEvents } from '../player.js';

const DEVICE_WIDTH = { desktop: 1280, tablet: 820, mobile: 390 };
const median = (xs) => { const v = xs.filter((x) => x > 0).sort((a, b) => a - b); return v.length ? v[Math.floor(v.length / 2)] : 0; };

function heatPalette() {
  const c = document.createElement('canvas');
  c.width = 256; c.height = 1;
  const g = c.getContext('2d');
  const grad = g.createLinearGradient(0, 0, 256, 0);
  grad.addColorStop(0, 'rgba(57,135,229,0)');
  grad.addColorStop(0.18, cssVar('--s1'));
  grad.addColorStop(0.45, cssVar('--s3'));
  grad.addColorStop(0.72, cssVar('--s4'));
  grad.addColorStop(1, cssVar('--s8'));
  g.fillStyle = grad;
  g.fillRect(0, 0, 256, 1);
  return g.getImageData(0, 0, 256, 1).data;
}

// points: [[x, y, weight]] in canvas pixels.
function drawHeat(canvas, points, radius = 22) {
  const w = canvas.width, h = canvas.height;
  const shadow = document.createElement('canvas');
  shadow.width = w; shadow.height = h;
  const sg = shadow.getContext('2d');
  const maxW = Math.max(1, ...points.map((p) => p[2] || 1));
  for (const [x, y, wt] of points) {
    const a = Math.min(1, 0.12 + 0.88 * ((wt || 1) / maxW)) * 0.35;
    const grad = sg.createRadialGradient(x, y, 0, x, y, radius);
    grad.addColorStop(0, 'rgba(0,0,0,' + a + ')');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    sg.fillStyle = grad;
    sg.fillRect(x - radius, y - radius, radius * 2, radius * 2);
  }
  const img = sg.getImageData(0, 0, w, h);
  const pal = heatPalette();
  const d = img.data;
  for (let i = 0; i < d.length; i += 4) {
    const a = d[i + 3];
    if (!a) continue;
    const k = Math.min(255, a) * 4;
    d[i] = pal[k]; d[i + 1] = pal[k + 1]; d[i + 2] = pal[k + 2]; d[i + 3] = Math.min(230, a * 2.2);
  }
  canvas.getContext('2d').putImageData(img, 0, 0);
}

function drawScroll(canvas, scroll, total) {
  const g = canvas.getContext('2d');
  const h = canvas.height, w = canvas.width;
  if (!total) return;
  for (let i = 0; i < scroll.length - 1; i++) {
    const share = scroll[i].reached / total;
    const y0 = (scroll[i].depth / 100) * h, y1 = (scroll[i + 1].depth / 100) * h;
    g.fillStyle = 'rgba(15,34,54,' + (0.62 * (1 - share)).toFixed(3) + ')';
    g.fillRect(0, y0, w, y1 - y0 + 1);
  }
  g.font = '600 13px "Plex Sans", sans-serif';
  for (const target of [0.75, 0.5, 0.25]) {
    const row = scroll.find((r) => r.reached / total < target);
    if (!row) continue;
    const y = (row.depth / 100) * h;
    g.fillStyle = cssVar('--s4');
    g.fillRect(0, y, w, 2);
    const label = Math.round(target * 100) + '% of visitors reach this point';
    g.fillStyle = 'rgba(11,11,11,0.78)';
    const tw = g.measureText(label).width + 16;
    g.fillRect(12, y - 26, tw, 22);
    g.fillStyle = '#fff';
    g.fillText(label, 20, y - 10);
  }
}

async function renderSnapshot(host, snap, width, height) {
  const Player = await loadPlayer();
  const events = Array.isArray(snap) ? snap : [snap];
  const full = events.find((e) => e && e.type === 2);
  if (!full) throw new Error('snapshot has no full DOM snapshot');
  const ts = full.timestamp || 1;
  const meta = { type: 4, timestamp: ts - 1, data: { href: (events.find((e) => e.type === 4) || {}).data?.href || '', width, height } };
  const target = el('div');
  host.appendChild(target);
  const player = new Player({ target, props: { events: sanitizeEvents([meta, { ...full, timestamp: ts }]), width, height, autoPlay: false, showController: false, skipInactive: false, mouseTail: false, UNSAFE_replayCanvas: false } });
  const replayer = player.getReplayer();
  replayer.pause(1);
  await new Promise((r) => setTimeout(r, 150));
  const frame = target.querySelector('iframe');
  return { doc: frame && frame.contentDocument, root: target, frame };
}

// The live page to frame: a path on the site's own origin, nothing else. Paths are visitors' data, and a
// "path" such as ".evil.example/x" or "@evil.example" joined to the origin would frame another site.
export function livePageUrl(origin, page) {
  if (!origin || typeof page !== 'string' || !page.startsWith('/') || page.startsWith('//') || page.includes('\\')) return '';
  try {
    const base = new URL(origin);
    const u = new URL(page, base);
    return u.origin === base.origin && /^https?:$/.test(u.protocol) ? u.href : '';
  } catch (e) { return ''; }
}

export const attachFirst = true;

export async function render(root, ctx) {
  const params = ctx.params;
  const pagesRes = await ctx.api('breakdown', { dim: 'page', limit: 50 });
  const page = params.get('page') || (pagesRes.rows[0] && pagesRes.rows[0].value) || '/';
  const device = params.get('device') || 'desktop';
  const mode = params.get('mode') || 'clicks';
  const modes = [{ key: 'clicks', label: 'Clicks' }, { key: 'frustration', label: 'Rage & dead' }, { key: 'mouse', label: 'Mouse' }, { key: 'scroll', label: 'Scroll' }];
  const c = card({ title: 'Heatmap', span: 9, tabs: modes, onTab: (k) => ctx.go('heatmaps', { mode: k }) });
  c.head.querySelectorAll('.tabs button').forEach((b, i) => b.setAttribute('aria-selected', modes[i].key === mode ? 'true' : 'false'));
  const side = card({ title: 'Most clicked elements', span: 3 });
  root.append(c.root, side.root);

  const pageSel = el('select', { class: 'input', 'aria-label': 'Page' },
    ...pagesRes.rows.map((r) => el('option', { value: r.value, text: r.value + ' · ' + fmt.compact(r.pageviews), selected: r.value === page })));
  if (!pagesRes.rows.some((r) => r.value === page)) pageSel.prepend(el('option', { value: page, text: page, selected: true }));
  pageSel.addEventListener('change', () => ctx.go('heatmaps', { page: pageSel.value }));
  const devSel = el('select', { class: 'input', 'aria-label': 'Device' },
    ...['desktop', 'tablet', 'mobile', 'all'].map((d) => el('option', { value: d, text: d[0].toUpperCase() + d.slice(1), selected: d === device })));
  devSel.addEventListener('change', () => ctx.go('heatmaps', { device: devSel.value }));
  const scriptsOn = params.get('scripts') === '1';
  const scripts = el('label', { class: 'note', style: { display: 'inline-flex', gap: '6px', alignItems: 'center' } },
    el('input', { type: 'checkbox', checked: scriptsOn }), 'Run the page’s scripts');
  scripts.querySelector('input').addEventListener('change', (e) => ctx.go('heatmaps', { scripts: e.target.checked ? '1' : null }));
  const controls = el('div', { style: { display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'center', marginBottom: '12px' } }, pageSel, devSel);
  c.body.appendChild(controls);

  const data = await ctx.api('heatmap', { page, device: device === 'all' ? '' : device });
  let snap = null;
  try { snap = (await ctx.api('snapshot', { page })).snapshot; } catch (e) { snap = null; }
  const clicks = data.clicks.filter((k) => (mode === 'frustration' ? k.kind !== 'click' : k.kind === 'click'));
  // Sizes come from visitors' data: keep them within what a page can be.
  const width = Math.max(200, Math.min(4096, median(data.clicks.map((k) => k.vw)) || DEVICE_WIDTH[device] || 1280));
  const height = Math.max(200, Math.min(12000, median(data.clicks.map((k) => k.ph)) || 2400));
  const summary = el('p', { class: 'note' });
  controls.appendChild(summary);

  const viewer = el('div', { class: 'viewer' });
  const stage = el('div', { style: { position: 'relative', width: width + 'px', height: height + 'px', transformOrigin: '0 0' } });
  viewer.appendChild(stage);
  c.body.appendChild(viewer);
  const legend = el('div', { class: 'scale' }, el('span', { text: 'Fewer' }), el('span', { class: 'ramp', style: { background: 'linear-gradient(90deg, ' +
    ['--s1', '--s3', '--s4', '--s8'].map(cssVar).join(', ') + ')' } }), el('span', { text: mode === 'scroll' ? '' : 'More ' + (mode === 'mouse' ? 'movement' : 'clicks') }));
  if (mode !== 'scroll') c.body.appendChild(legend);

  let doc = null;
  if (snap) {
    try {
      const r = await renderSnapshot(stage, snap, width, height);
      doc = r.doc;
      summary.textContent = 'Rendered from a DOM snapshot · clicks placed on the elements they hit';
    } catch (e) {
      console.warn('Tidesight: could not render the DOM snapshot, framing the live page instead', e);
      snap = null;
    }
  }
  if (!snap) {
    const origin = (ctx.siteInfo && ctx.siteInfo.origins && ctx.siteInfo.origins[0]) || '';
    const src = livePageUrl(origin, page);
    // Scripts run only in a frame of another origin than the dashboard's (it could act as the dashboard).
    const scriptsAllowed = scriptsOn && src && new URL(src).origin !== location.origin;
    if (src) {
      const frame = el('iframe', { src, width, height, title: 'Page preview', loading: 'lazy',
        sandbox: scriptsAllowed ? 'allow-same-origin allow-scripts' : 'allow-same-origin', referrerpolicy: 'no-referrer' });
      stage.appendChild(frame);
      summary.textContent = 'Live page framed at ' + width + 'px · positions by page coordinates' +
        (scriptsAllowed ? ' · page scripts on (the visit may be recorded)' : '');
      controls.appendChild(scripts);
    } else {
      summary.textContent = origin ? 'This page is not on the site’s origin: showing positions on a blank page'
        : 'No origin configured for this site: showing positions on a blank page';
    }
  }
  const canvas = el('canvas', { width, height });
  stage.appendChild(canvas);
  const fit = () => {
    const avail = viewer.clientWidth - 2;
    const scale = Math.min(1, avail / width);
    stage.style.transform = 'scale(' + scale + ')';
    stage.style.marginBottom = -(height * (1 - scale)) + 'px';
    stage.style.marginRight = -(width * (1 - scale)) + 'px';
  };
  new ResizeObserver(fit).observe(viewer);
  fit();

  if (mode === 'scroll') {
    drawScroll(canvas, data.scroll, data.pageviews);
    summary.textContent += ' · ' + fmt.num(data.pageviews) + ' pageviews';
  } else if (mode === 'mouse') {
    const m = data.mouse;
    const pts = [];
    if (m.grid && m.grid.length) {
      m.grid.forEach((row, y) => row.forEach((v, x) => { if (v) pts.push([(x + 0.5) / m.cols * width, (y + 0.5) / m.rows * height, v]); }));
    }
    drawHeat(canvas, pts, 34);
    summary.textContent += ' · ' + fmt.num(m.points) + ' mouse positions' + (m.points ? '' : ' (enable mouse_moves to collect them)');
  } else {
    const pts = [];
    let placed = 0;
    for (const k of clicks) {
      let x = null, y = null;
      if (doc && k.sel && k.ex !== null && k.ey !== null) {
        try {
          const node = doc.querySelector(k.sel);
          if (node) {
            const r = node.getBoundingClientRect();
            const sx = doc.defaultView ? doc.defaultView.scrollX : 0, sy = doc.defaultView ? doc.defaultView.scrollY : 0;
            x = r.left + sx + k.ex * r.width; y = r.top + sy + k.ey * r.height;
            placed++;
          }
        } catch (e) { /* invalid selector for this document */ }
      }
      if (x === null && k.x !== null && k.y !== null) {
        const f = k.vw ? width / k.vw : 1;
        x = k.x * f; y = k.y * f;
      }
      if (x !== null && y !== null) pts.push([x, y, 1]);
    }
    drawHeat(canvas, pts);
    summary.textContent += ' · ' + fmt.num(clicks.length) + ' ' + (mode === 'frustration' ? 'rage, dead and error clicks' : 'clicks') +
      (doc ? ' (' + fmt.num(placed) + ' on their element)' : '');
  }

  table(side.body, [
    { key: 'sel', label: 'Element', trunc: true, render: (r) => el('code', { text: r.sel }) },
    { key: 'clicks', label: 'Clicks', num: true, format: fmt.num },
    { key: 'rage', label: 'Rage', num: true, format: (v) => (v ? fmt.num(v) : '') },
    { key: 'dead', label: 'Dead', num: true, format: (v) => (v ? fmt.num(v) : '') },
  ], data.selectors.slice(0, 40), { sortKey: 'clicks', empty: 'No clicks on this page yet.' });
}
