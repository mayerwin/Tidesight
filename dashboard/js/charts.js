// Chart components in plain SVG/HTML. Labels come from visitors' data, so text is always set with
// textContent (never innerHTML). Colours come from CSS variables, so both themes render correctly.
import * as fmt from './fmt.js';

const SVGNS = 'http://www.w3.org/2000/svg';

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
  return node;
}

export function s(tag, attrs = {}) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, v);
  return node;
}

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export const SERIES = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'];
export const SEQ = ['--seq-100', '--seq-200', '--seq-300', '--seq-400', '--seq-500', '--seq-600', '--seq-700'];

export const icons = {
  table: '<path d="M3 5h18v14H3zM3 10h18M3 15h18M9 5v14" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  chart: '<path d="M4 19V5M4 19h16M7 15l4-5 3 3 5-7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
  close: '<path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  download: '<path d="M12 4v11m0 0l-4-4m4 4l4-4M5 19h14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
};
export function icon(name) {
  const svgEl = s('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' });
  svgEl.innerHTML = icons[name]; // static markup from this file, never user data
  return svgEl;
}

// ---- tooltip -----------------------------------------------------------------------
let tipEl = null;
export const tip = {
  show(x, y, title, rows) {
    if (!tipEl) { tipEl = el('div', { class: 'tooltip', role: 'tooltip' }); document.body.appendChild(tipEl); }
    tipEl.replaceChildren();
    if (title) tipEl.appendChild(el('div', { class: 'tt-title', text: title }));
    for (const r of rows) {
      const row = el('div', { class: 'tt-row' });
      if (r.color) row.appendChild(el('i', { class: 'key', style: { background: r.color } }));
      row.appendChild(el('b', { text: r.value }));
      if (r.label) row.appendChild(el('span', { text: r.label }));
      tipEl.appendChild(row);
    }
    tipEl.hidden = false;
    const w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    const left = Math.min(x, window.innerWidth - w - 24), top = Math.min(y, window.innerHeight - h - 24);
    tipEl.style.left = left + 'px';
    tipEl.style.top = top + 'px';
  },
  hide() { if (tipEl) tipEl.hidden = true; },
};

// ---- scales -------------------------------------------------------------------------
export function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => st >= raw) || raw;
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

function observeWidth(container, draw) {
  let last = 0;
  const ro = new ResizeObserver(() => {
    const w = Math.round(container.getBoundingClientRect().width);
    if (w && Math.abs(w - last) > 2) { last = w; draw(w); }
  });
  ro.observe(container);
  return ro;
}

// ---- line chart (time series) --------------------------------------------------------
// series: [{name, values: [number|null], color: '--s1' | 'prev', area: bool}]
export function lineChart(container, { labels, series, format = fmt.num, height = 240, labelFormat = (x) => x, titleFormat }) {
  container.classList.add('chart');
  const draw = (width) => {
    container.replaceChildren();
    const m = { l: 46, r: 14, t: 10, b: 26 };
    const W = width, H = height, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const max = Math.max(1, ...series.flatMap((sr) => sr.values.filter((v) => v !== null && v !== undefined)));
    const ticks = niceTicks(max);
    const top = ticks[ticks.length - 1];
    const n = labels.length;
    const x = (i) => m.l + (n <= 1 ? iw / 2 : (i * iw) / (n - 1));
    const y = (v) => m.t + ih - (v / top) * ih;
    const svgEl = s('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img' });
    for (const t of ticks) {
      svgEl.appendChild(s('line', { class: 'grid-line', x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }));
      const lab = s('text', { class: 'tick', x: m.l - 8, y: y(t) + 4, 'text-anchor': 'end' });
      lab.textContent = format(t);
      svgEl.appendChild(lab);
    }
    svgEl.appendChild(s('line', { class: 'axis-line', x1: m.l, x2: W - m.r, y1: y(0), y2: y(0) }));
    const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 70))));
    labels.forEach((lb, i) => {
      if (i % every && i !== n - 1) return;
      const t = s('text', { class: 'tick', x: x(i), y: H - 6, 'text-anchor': i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle' });
      t.textContent = labelFormat(lb);
      svgEl.appendChild(t);
    });
    // Previous period first (behind), then current series.
    const order = [...series].sort((a, b) => (a.color === 'prev' ? -1 : 0) - (b.color === 'prev' ? -1 : 0));
    for (const sr of order) {
      const color = sr.color === 'prev' ? cssVar('--other') : cssVar(sr.color || '--s1');
      let d = '', started = false;
      sr.values.forEach((v, i) => {
        if (v === null || v === undefined) { started = false; return; }
        d += (started ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1);
        started = true;
      });
      if (sr.area && sr.values.length > 1) {
        const first = sr.values.findIndex((v) => v !== null && v !== undefined);
        let last = -1;
        sr.values.forEach((v, i) => { if (v !== null && v !== undefined) last = i; });
        if (first >= 0 && last > first) {
          svgEl.appendChild(s('path', { class: 'area', d: d + `L${x(last)},${y(0)}L${x(first)},${y(0)}Z`, fill: color, 'fill-opacity': 0.1 }));
        }
      }
      svgEl.appendChild(s('path', { class: 'series' + (sr.color === 'prev' ? ' prev' : ''), d, stroke: color }));
    }
    // Hover layer: a crosshair that snaps to the nearest x, one tooltip with every series.
    const cross = s('line', { class: 'crosshair', y1: m.t, y2: m.t + ih, visibility: 'hidden' });
    svgEl.appendChild(cross);
    const dots = series.map((sr) => {
      const dot = s('circle', { class: 'dot', r: 4.5, visibility: 'hidden', fill: sr.color === 'prev' ? cssVar('--other') : cssVar(sr.color || '--s1') });
      svgEl.appendChild(dot);
      return dot;
    });
    const hit = s('rect', { x: m.l, y: m.t, width: iw, height: ih, fill: 'transparent', tabindex: 0, 'aria-label': 'Chart data; use arrow keys' });
    svgEl.appendChild(hit);
    let idx = n - 1;
    const showAt = (i, cx, cy) => {
      idx = Math.max(0, Math.min(n - 1, i));
      cross.setAttribute('x1', x(idx)); cross.setAttribute('x2', x(idx)); cross.setAttribute('visibility', 'visible');
      const rows = [];
      series.forEach((sr, k) => {
        const v = sr.values[idx];
        if (v === null || v === undefined) { dots[k].setAttribute('visibility', 'hidden'); return; }
        dots[k].setAttribute('cx', x(idx)); dots[k].setAttribute('cy', y(v)); dots[k].setAttribute('visibility', 'visible');
        rows.push({ value: format(v), label: sr.name, color: sr.color === 'prev' ? cssVar('--other') : cssVar(sr.color || '--s1') });
      });
      const rect = svgEl.getBoundingClientRect();
      tip.show(cx ?? rect.left + (x(idx) / W) * rect.width, cy ?? rect.top + 20, (titleFormat || labelFormat)(labels[idx]), rows);
    };
    const hide = () => { cross.setAttribute('visibility', 'hidden'); dots.forEach((d) => d.setAttribute('visibility', 'hidden')); tip.hide(); };
    hit.addEventListener('pointermove', (e) => {
      const rect = svgEl.getBoundingClientRect();
      const px = ((e.clientX - rect.left) / rect.width) * W;
      showAt(Math.round(((px - m.l) / iw) * (n - 1)), e.clientX, e.clientY);
    });
    hit.addEventListener('pointerleave', hide);
    hit.addEventListener('focus', () => showAt(idx));
    hit.addEventListener('blur', hide);
    hit.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft') { showAt(idx - 1); e.preventDefault(); }
      if (e.key === 'ArrowRight') { showAt(idx + 1); e.preventDefault(); }
    });
    container.appendChild(svgEl);
    if (series.length > 1) {
      const legend = el('div', { class: 'legend' });
      for (const sr of series) {
        legend.appendChild(el('span', {}, el('i', { style: { background: sr.color === 'prev' ? cssVar('--other') : cssVar(sr.color || '--s1') } }), sr.name));
      }
      container.appendChild(legend);
    }
  };
  observeWidth(container, draw);
}

// ---- column chart (one series) -----------------------------------------------------------
export function columnChart(container, { labels, values, format = fmt.num, height = 160, labelFormat = (x) => x, name = '' }) {
  container.classList.add('chart');
  const draw = (width) => {
    container.replaceChildren();
    const m = { l: 40, r: 8, t: 8, b: 22 };
    const W = width, H = height, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const ticks = niceTicks(Math.max(1, ...values));
    const top = ticks[ticks.length - 1];
    const n = values.length, band = iw / Math.max(1, n), bw = Math.max(2, Math.min(24, band - 2));
    const y = (v) => m.t + ih - (v / top) * ih;
    const svgEl = s('svg', { viewBox: `0 0 ${W} ${H}`, height: H });
    for (const t of ticks) {
      svgEl.appendChild(s('line', { class: 'grid-line', x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }));
      const lab = s('text', { class: 'tick', x: m.l - 6, y: y(t) + 4, 'text-anchor': 'end' });
      lab.textContent = format(t);
      svgEl.appendChild(lab);
    }
    const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 60))));
    values.forEach((v, i) => {
      const cx = m.l + band * i + band / 2;
      if (v > 0) {
        const h = Math.max(1, (v / top) * ih), r = Math.min(4, bw / 2, h);
        const x0 = cx - bw / 2, y0 = m.t + ih - h;
        const d = `M${x0},${m.t + ih}V${y0 + r}Q${x0},${y0} ${x0 + r},${y0}H${x0 + bw - r}Q${x0 + bw},${y0} ${x0 + bw},${y0 + r}V${m.t + ih}Z`;
        const bar = s('path', { class: 'bar', d, tabindex: 0 });
        const show = (cx2, cy2) => tip.show(cx2, cy2, labelFormat(labels[i]), [{ value: format(v), label: name, color: cssVar('--s1') }]);
        bar.addEventListener('pointermove', (e) => show(e.clientX, e.clientY));
        bar.addEventListener('pointerleave', () => tip.hide());
        bar.addEventListener('focus', () => { const r2 = bar.getBoundingClientRect(); show(r2.right, r2.top); });
        bar.addEventListener('blur', () => tip.hide());
        svgEl.appendChild(bar);
      }
      if (i % every === 0 || i === n - 1) {
        const t = s('text', { class: 'tick', x: cx, y: H - 5, 'text-anchor': 'middle' });
        t.textContent = labelFormat(labels[i]);
        svgEl.appendChild(t);
      }
    });
    svgEl.appendChild(s('line', { class: 'axis-line', x1: m.l, x2: W - m.r, y1: y(0), y2: y(0) }));
    container.appendChild(svgEl);
  };
  observeWidth(container, draw);
}

// ---- sparkline ---------------------------------------------------------------------------------
export function sparkline(values, { width = 120, height = 28, color = '--s1' } = {}) {
  const svgEl = s('svg', { viewBox: `0 0 ${width} ${height}`, width, height, 'aria-hidden': 'true' });
  const vals = values.map((v) => v || 0);
  const max = Math.max(1, ...vals), n = vals.length;
  if (n < 2) return svgEl;
  const pts = vals.map((v, i) => [(i / (n - 1)) * (width - 4) + 2, height - 3 - (v / max) * (height - 6)]);
  svgEl.appendChild(s('path', { d: 'M' + pts.map((p) => p.map((c) => c.toFixed(1)).join(',')).join('L'), fill: 'none', stroke: cssVar(color), 'stroke-width': 1.5, 'stroke-linejoin': 'round' }));
  return svgEl;
}

// ---- bar list (breakdowns) -----------------------------------------------------------------------
// rows: [{label, sub?, value, extra?}] — one series, so every bar wears the same colour (wash).
export function barList(container, rows, { valueLabel = 'Visitors', format = fmt.compact, onClick, total, limit = 10, labelPrefix } = {}) {
  container.replaceChildren();
  if (!rows.length) { container.appendChild(el('div', { class: 'empty', text: 'No data for this period' })); return; }
  const max = Math.max(...rows.map((r) => r.value || 0), 1);
  const sum = total || rows.reduce((a, r) => a + (r.value || 0), 0);
  const list = el('div', { class: 'bars' });
  list.appendChild(el('div', { class: 'bars-head' }, el('span', { text: '' }), el('span', { text: valueLabel })));
  let shown = 0;
  const addRows = (n) => {
    for (const r of rows.slice(shown, shown + n)) {
      const row = el('div', { class: 'bar-row', tabindex: onClick ? 0 : -1, role: onClick ? 'button' : undefined, title: r.title || r.label });
      row.appendChild(el('span', { class: 'fill', style: { width: Math.max(0.5, ((r.value || 0) / max) * 100) + '%' } }));
      const label = el('span', { class: 'label' });
      if (labelPrefix) { const p = labelPrefix(r); if (p) label.appendChild(el('span', { class: 'flag', text: p })); }
      label.appendChild(document.createTextNode(r.label));
      if (r.sub) label.appendChild(el('span', { class: 'sub', text: r.sub }));
      row.appendChild(label);
      row.appendChild(el('span', { class: 'value', text: format(r.value) }));
      row.appendChild(el('span', { class: 'pct', text: sum ? fmt.pct((r.value || 0) / sum) : '' }));
      if (onClick) {
        row.addEventListener('click', () => onClick(r));
        row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(r); } });
      }
      list.appendChild(row);
    }
    shown += n;
  };
  addRows(limit);
  container.appendChild(list);
  if (rows.length > limit) {
    const more = el('div', { class: 'more' });
    const btn = el('button', { type: 'button', text: 'Show all ' + rows.length });
    btn.addEventListener('click', () => { addRows(rows.length); more.remove(); });
    more.appendChild(btn);
    container.appendChild(more);
  }
}

// ---- data table -----------------------------------------------------------------------------------
// columns: [{key, label, num?: bool, format?: fn, render?: fn(row) -> Node|string, width?}]
export function table(container, columns, rows, { onRowClick, sortKey, sortDir = -1, empty = 'No data for this period' } = {}) {
  container.replaceChildren();
  if (!rows.length) { container.appendChild(el('div', { class: 'empty', text: empty })); return; }
  let key = sortKey, dir = sortDir;
  const wrap = el('div', { class: 'table-wrap' });
  const tbl = el('table', { class: 'data' });
  const render = () => {
    const sorted = key ? [...rows].sort((a, b) => {
      const va = a[key], vb = b[key];
      if (va === vb) return 0;
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      return (va > vb ? 1 : -1) * dir;
    }) : rows;
    tbl.replaceChildren();
    const thead = el('thead');
    const tr = el('tr');
    for (const c of columns) {
      const th = el('th', { class: (c.num ? 'num ' : '') + (c.sortable === false ? '' : 'sortable'), scope: 'col', style: c.width ? { width: c.width } : undefined },
        c.label + (key === c.key ? (dir > 0 ? ' ↑' : ' ↓') : ''));
      if (c.sortable !== false) th.addEventListener('click', () => { if (key === c.key) dir = -dir; else { key = c.key; dir = c.num ? -1 : 1; } render(); });
      tr.appendChild(th);
    }
    thead.appendChild(tr);
    tbl.appendChild(thead);
    const tbody = el('tbody');
    for (const r of sorted) {
      const row = el('tr', { class: onRowClick ? 'clickable' : '' });
      for (const c of columns) {
        const v = c.render ? c.render(r) : c.format ? c.format(r[c.key], r) : r[c.key];
        const td = el('td', { class: (c.num ? 'num ' : '') + (c.trunc ? 'trunc' : '') , title: c.trunc && typeof v === 'string' ? v : undefined });
        if (v instanceof Node) td.appendChild(v); else td.textContent = v === null || v === undefined ? '—' : String(v);
        row.appendChild(td);
      }
      if (onRowClick) row.addEventListener('click', () => onRowClick(r));
      tbody.appendChild(row);
    }
    tbl.appendChild(tbody);
  };
  render();
  wrap.appendChild(tbl);
  container.appendChild(wrap);
}

// ---- heat table (cohorts, hour × weekday) ------------------------------------------------------
export function heatTable(container, { colLabels, rows, format = fmt.pct, max }) {
  container.replaceChildren();
  if (!rows.length) { container.appendChild(el('div', { class: 'empty', text: 'No data for this period' })); return; }
  const all = rows.flatMap((r) => r.cells.filter((v) => v !== null && v !== undefined));
  const top = max || Math.max(...all, 1e-9);
  const ramp = SEQ.map(cssVar);
  const tbl = el('table', { class: 'heat' });
  const head = el('tr', {}, el('th', { text: '' }), ...(rows[0].pre || []).map((p) => el('th', { text: p.label })), ...colLabels.map((c) => el('th', { text: c })));
  tbl.appendChild(head);
  for (const r of rows) {
    const tr = el('tr', {}, el('th', { text: r.label, style: { textAlign: 'left' } }));
    for (const p of r.pre || []) tr.appendChild(el('td', { class: 'num', text: p.text }));
    r.cells.forEach((v, i) => {
      if (v === null || v === undefined) { tr.appendChild(el('td', { text: '' })); return; }
      const k = Math.min(ramp.length - 1, Math.floor((v / top) * (ramp.length - 1) + 0.0001));
      const bg = ramp[k];
      const light = k <= 2;
      const td = el('td', { text: format(v), style: { background: bg, color: light ? cssVar('--ink') : '#fff' }, tabindex: 0 });
      td.addEventListener('pointermove', (e) => tip.show(e.clientX, e.clientY, r.label + ' · ' + colLabels[i], [{ value: format(v), color: bg }]));
      td.addEventListener('pointerleave', () => tip.hide());
      tr.appendChild(td);
    });
    tbl.appendChild(tr);
  }
  const wrap = el('div', { class: 'table-wrap' }, tbl);
  container.appendChild(wrap);
}

// ---- world map (sequential choropleth) ------------------------------------------------------------
let worldPromise = null;
function loadWorld() {
  if (!worldPromise) worldPromise = fetch(new URL('../vendor/world.json', import.meta.url)).then((r) => r.json());
  return worldPromise;
}
const EE = { A1: 1.340264, A2: -0.081106, A3: 0.000893, A4: 0.003796, M: Math.sqrt(3) / 2, X: 2.70663, Y: 1.31734 };
export function projectEqualEarth(lon, lat) {
  const l = (lon * Math.PI) / 180, p = (lat * Math.PI) / 180;
  const t = Math.asin(EE.M * Math.sin(p)), t2 = t * t, t6 = t2 * t2 * t2;
  const x = (2 * Math.sqrt(3) * l * Math.cos(t)) / (3 * (EE.A1 + 3 * EE.A2 * t2 + t6 * (7 * EE.A3 + 9 * EE.A4 * t2)));
  const y = t * (EE.A1 + EE.A2 * t2 + t6 * (EE.A3 + EE.A4 * t2));
  const sc = 1000 / (2 * EE.X);
  return [(x + EE.X) * sc, (EE.Y - y) * sc];
}
export async function worldMap(container, { values, format = fmt.num, label = 'Visitors', onClick, points = [] }) {
  container.classList.add('map', 'chart');
  const world = await loadWorld();
  container.replaceChildren();
  const svgEl = s('svg', { viewBox: world.viewBox, role: 'img', 'aria-label': 'World map of ' + label.toLowerCase() });
  const vals = Object.values(values).filter((v) => v > 0).sort((a, b) => a - b);
  const ramp = SEQ.slice(1).map(cssVar); // 6 classes, lightest step reserved for "no data"
  const q = (v) => {
    if (!vals.length || !(v > 0)) return null;
    let i = 0;
    while (i < vals.length && vals[i] < v) i++;
    return Math.min(ramp.length - 1, Math.floor((i / vals.length) * ramp.length));
  };
  const empty = cssVar('--surface-sunk');
  for (const [cc, c] of Object.entries(world.countries)) {
    const v = values[cc] || 0;
    const k = q(v);
    const path = s('path', { d: c.d, fill: k === null ? empty : ramp[k], tabindex: v ? 0 : -1 });
    const show = (x, y) => tip.show(x, y, fmt.flag(cc) + ' ' + fmt.country(cc), [{ value: format(v), label, color: k === null ? undefined : ramp[k] }]);
    path.addEventListener('pointermove', (e) => show(e.clientX, e.clientY));
    path.addEventListener('pointerleave', () => tip.hide());
    path.addEventListener('focus', () => { const r = path.getBoundingClientRect(); show(r.right, r.top); });
    path.addEventListener('blur', () => tip.hide());
    if (onClick && v) { path.style.cursor = 'pointer'; path.addEventListener('click', () => onClick(cc)); }
    svgEl.appendChild(path);
  }
  const pmax = Math.max(1, ...points.map((p) => p.value));
  for (const p of points.slice(0, 300)) {
    const [x, y] = projectEqualEarth(p.lon, p.lat);
    const r = 2 + Math.sqrt(p.value / pmax) * 7;
    const dot = s('circle', { cx: x.toFixed(1), cy: y.toFixed(1), r: r.toFixed(1), fill: cssVar('--s2'), 'fill-opacity': 0.75, stroke: cssVar('--surface'), 'stroke-width': 1.5 });
    dot.addEventListener('pointermove', (e) => tip.show(e.clientX, e.clientY, p.label, [{ value: format(p.value), label }]));
    dot.addEventListener('pointerleave', () => tip.hide());
    svgEl.appendChild(dot);
  }
  container.appendChild(svgEl);
  const scale = el('div', { class: 'scale' }, el('span', { text: 'Fewer' }), el('span', { class: 'ramp' }), el('span', { text: 'More ' + label.toLowerCase() }));
  if (points.length) scale.appendChild(el('span', { style: { marginLeft: '12px' } }, el('i', { class: 'box', style: { display: 'inline-block', width: '9px', height: '9px', borderRadius: '50%', background: cssVar('--s2'), marginRight: '6px' } }), 'Cities'));
  container.appendChild(scale);
}

// ---- card --------------------------------------------------------------------------------------
// A card with a title, optional tabs, and a chart/table toggle (every chart has a table twin).
export function card({ title, span = 12, tabs, onTab, tableToggle = false, actions = [] }) {
  const root = el('section', { class: 'card span-' + span });
  const head = el('div', { class: 'card-head' }, el('h2', { class: 'card-title', text: title }));
  head.appendChild(el('span', { class: 'spacer' }));
  let tabBar = null;
  if (tabs && tabs.length) {
    tabBar = el('div', { class: 'tabs', role: 'tablist' });
    tabs.forEach((t, i) => {
      const b = el('button', { type: 'button', role: 'tab', 'aria-selected': i === 0 ? 'true' : 'false', text: t.label });
      b.addEventListener('click', () => {
        tabBar.querySelectorAll('button').forEach((x) => x.setAttribute('aria-selected', 'false'));
        b.setAttribute('aria-selected', 'true');
        onTab && onTab(t.key);
      });
      tabBar.appendChild(b);
    });
    head.appendChild(tabBar);
  }
  for (const a of actions) head.appendChild(a);
  const body = el('div', { class: 'card-body' });
  const alt = el('div', { class: 'card-alt', hidden: true });
  let showingTable = false;
  if (tableToggle) {
    const btn = el('button', { class: 'icon-btn', type: 'button', title: 'Show as table', 'aria-label': 'Show as table' }, icon('table'));
    btn.addEventListener('click', () => {
      showingTable = !showingTable;
      body.hidden = showingTable;
      alt.hidden = !showingTable;
      btn.replaceChildren(icon(showingTable ? 'chart' : 'table'));
      btn.title = showingTable ? 'Show as chart' : 'Show as table';
    });
    head.appendChild(btn);
  }
  root.append(head, body, alt);
  return { root, head, body, alt };
}
