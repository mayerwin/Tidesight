// Paths: how visits move through the site. A flow of their first pages (from their entry, from a page, or back from
// a page), and what comes before and after one page. A page seen again at once (a reload) is one step.
import { barList, card, cssVar, el, icon, observeWidth, s, tip } from '../charts.js';
import * as fmt from '../fmt.js';
import { hrefFor } from '../state.js';
import { segmented } from '../ui.js';

const OTHER = '(other pages)';

export async function render(root, ctx) {
  const p = ctx.params;
  const mode = p.get('mode') === 'transitions' ? 'transitions' : 'flow';
  const steps = Math.max(2, Math.min(8, parseInt(p.get('steps') || '4', 10) || 4));
  const width = Math.max(2, Math.min(15, parseInt(p.get('width') || '6', 10) || 6));
  const anchor = p.get('anchor') === 'end' ? 'end' : p.get('anchor') === 'start' ? 'start' : 'entry';
  const page = p.get('page') || '';

  // Controls.
  const controls = card({ title: '', span: 12, className: 'paths-controls' });
  controls.head.remove();
  const bar = el('div', { class: 'paths-bar' });
  bar.appendChild(segmented([{ id: 'flow', label: 'Flow' }, { id: 'transitions', label: 'Around a page' }], mode,
    (v) => ctx.go('paths', { mode: v === 'flow' ? null : v }), { label: 'View' }));
  const pageInput = el('input', { class: 'input page-input', value: page, list: 'paths-pages', placeholder: '/pricing, or /blog/*', spellcheck: 'false', 'aria-label': 'Page' });
  const pages = el('datalist', { id: 'paths-pages' });
  const apply = (changes) => ctx.go('paths', changes);
  if (mode === 'flow') {
    const from = el('select', { class: 'input', 'aria-label': 'Paths' },
      el('option', { value: 'entry', text: 'From the entry page', selected: anchor === 'entry' || null }),
      el('option', { value: 'start', text: 'From a page…', selected: anchor === 'start' || null }),
      el('option', { value: 'end', text: 'Leading to a page…', selected: anchor === 'end' || null }));
    from.addEventListener('change', () => {
      pageInput.hidden = from.value === 'entry';
      if (from.value === 'entry') apply({ anchor: null, page: null });
      else if (pageInput.value.trim()) apply({ anchor: from.value, page: pageInput.value.trim() });
      else pageInput.focus();
    });
    pageInput.hidden = anchor === 'entry';
    pageInput.addEventListener('change', () => { if (pageInput.value.trim()) apply({ anchor: from.value, page: pageInput.value.trim() }); });
    const stepSel = el('select', { class: 'input', 'aria-label': 'Steps' }, ...[2, 3, 4, 5, 6, 7, 8].map((n) => el('option', { value: n, text: n + ' steps', selected: n === steps || null })));
    stepSel.addEventListener('change', () => apply({ steps: stepSel.value === '4' ? null : stepSel.value }));
    const widthSel = el('select', { class: 'input', 'aria-label': 'Pages per step' }, ...[3, 4, 6, 8, 10, 12, 15].map((n) => el('option', { value: n, text: n + ' pages per step', selected: n === width || null })));
    widthSel.addEventListener('change', () => apply({ width: widthSel.value === '6' ? null : widthSel.value }));
    bar.append(from, pageInput, pages, stepSel, widthSel);
  } else {
    pageInput.addEventListener('change', () => { if (pageInput.value.trim()) apply({ page: pageInput.value.trim() }); });
    bar.append(pageInput, pages);
  }
  controls.body.appendChild(bar);
  root.appendChild(controls.root);
  ctx.api('breakdown', { dim: 'page', limit: 200, sort: 'pageviews' }, { compare: false })
    .then((r) => pages.replaceChildren(...r.rows.map((x) => el('option', { value: x.value })))).catch(() => {});

  if (mode === 'transitions') {
    const target = page || (await topPage(ctx));
    if (!target) { root.appendChild(el('div', { class: 'card span-12 empty', text: 'No pageviews in this period.' })); return; }
    pageInput.value = target;
    await transitions(root, ctx, target);
    return;
  }
  const params = { steps, width };
  if (anchor !== 'entry' && page) params[anchor] = page;
  const data = await ctx.api('paths', params, { compare: false });
  const c = card({ title: data.direction === 'backward' ? 'Paths leading to ' + page : anchor === 'start' && page ? 'Paths from ' + page : 'Paths from the entry page', span: 12,
    info: 'Each column is a step of the visit; bands show how many visits went from one page to the next. The pale part of a page: visits that ' +
      (data.direction === 'backward' ? 'started there.' : 'ended there.') + ' Click a page to see what comes before and after it.' });
  root.appendChild(c.root);
  if (!data.sessions) {
    c.body.appendChild(el('div', { class: 'empty', text: page && anchor !== 'entry' ? 'No visit in this period saw ' + page + '.' : 'No pageviews in this period.' }));
    return;
  }
  const holder = el('div', { class: 'sankey-scroll' });
  c.body.appendChild(holder);
  sankey(holder, data, (node) => ctx.go('paths', { mode: 'transitions', page: node.value }));
  c.body.appendChild(el('p', { class: 'note', text: fmt.plural(data.sessions, 'visit') + (data.truncated ? ' (the first ones only: narrow the period for every visit)' : '') + '. ' +
    (data.direction === 'backward' ? 'Read from right to left: the page, then the pages before it.' : '') }));
}

async function topPage(ctx) {
  try {
    const r = await ctx.api('breakdown', { dim: 'page', limit: 1, sort: 'pageviews' }, { compare: false });
    return r.rows.length ? r.rows[0].value : '';
  } catch (e) { return ''; }
}

// ---- flow (a Sankey diagram) ---------------------------------------------------------------------------------
function sankey(container, data, onNode) {
  observeWidth(container, (width) => drawSankey(container, data, onNode, width));
}

function drawSankey(container, data, onNode, width) {
  const cols = data.steps;
  const backward = data.direction === 'backward';
  const nodeW = 10, gap = 14, top = 26, labelRoom = 230;
  // The columns share the card's width (scrolling sideways below 200 px a column).
  const colW = cols.length > 1 ? Math.max(200, (width - nodeW - labelRoom) / (cols.length - 1)) : 0;
  const maxNodes = Math.max(...cols.map((c) => c.nodes.length));
  const H = Math.max(300, maxNodes * 62);
  const W = (cols.length - 1) * colW + nodeW + labelRoom;
  const totals = cols.map((c) => c.nodes.reduce((a, n) => a + n.visits, 0));
  const k = Math.min(...cols.map((c, i) => (H - gap * (c.nodes.length - 1)) / Math.max(1, totals[i])));
  const pos = {};
  // Backward paths read from right to left: the page the visits lead to is on the right.
  const xOf = (i) => (backward ? (cols.length - 1 - i) : i) * colW;
  cols.forEach((c, i) => {
    let y = top;
    for (const n of c.nodes) {
      const h = Math.max(2, n.visits * k);
      pos[n.id] = { x: xOf(i), y, h, node: n, outY: y, inY: y };
      y += h + gap;
    }
  });
  const svg = s('svg', { viewBox: `0 0 ${W} ${H + top + 8}`, width: W, height: H + top + 8, class: 'sankey', role: 'img', 'aria-label': 'Paths through the site' });
  const ink = cssVar('--s1'), muted = cssVar('--other');
  cols.forEach((c, i) => {
    const t = s('text', { class: 'sk-step', x: xOf(i), y: 14 });
    t.textContent = i === 0 ? (backward ? 'The page' : 'Step 1') : (backward ? i + ' before' : 'Step ' + (i + 1));
    svg.appendChild(t);
  });
  // Bands, widest first so the thin ones stay on top.
  const links = [...data.links].sort((a, b) => (pos[a.source] ? pos[a.source].y : 0) - (pos[b.source] ? pos[b.source].y : 0)
    || (pos[a.target] ? pos[a.target].y : 0) - (pos[b.target] ? pos[b.target].y : 0));
  const bands = [];
  for (const l of links) {
    const a = pos[l.source], b = pos[l.target];
    if (!a || !b) continue;
    const h = l.visits * k;
    // Forward: out of a's right edge into b's left edge. Backward: a is right of b.
    const [L, R] = backward ? [b, a] : [a, b];
    const y0 = backward ? L.outY : a.outY, y1 = backward ? R.inY : b.inY;
    if (backward) { L.outY += h; R.inY += h; } else { a.outY += h; b.inY += h; }
    const x0 = L.x + nodeW, x1 = R.x, xm = (x0 + x1) / 2;
    const d = `M${x0},${y0}C${xm},${y0} ${xm},${y1} ${x1},${y1}L${x1},${y1 + h}C${xm},${y1 + h} ${xm},${y0 + h} ${x0},${y0 + h}Z`;
    const band = s('path', { class: 'sk-band', d, fill: ink, tabindex: 0 });
    const show = (e) => {
      band.classList.add('on');
      const from = backward ? b.node : a.node, to = backward ? a.node : b.node;
      const r = band.getBoundingClientRect();
      tip.show(e && e.clientX ? e.clientX : r.left + r.width / 2, e && e.clientY ? e.clientY : r.top, from.value + ' → ' + to.value,
        [{ value: fmt.num(l.visits), label: 'visits' }, { value: fmt.pct(l.visits / Math.max(1, a.node.visits), 1), label: 'of the visits to ' + a.node.value }]);
    };
    band.addEventListener('pointermove', show);
    band.addEventListener('focus', show);
    const hide = () => { band.classList.remove('on'); tip.hide(); };
    band.addEventListener('pointerleave', hide);
    band.addEventListener('blur', hide);
    bands.push(band);
  }
  bands.forEach((b) => svg.appendChild(b));
  const lastX = Math.max(...Object.values(pos).map((q) => q.x));
  for (const id of Object.keys(pos)) {
    const { x, y, h, node } = pos[id];
    const g = s('g', { class: 'sk-node' + (node.other ? ' other' : ''), tabindex: 0 });
    const color = node.other ? muted : ink;
    const room = Math.max(16, Math.floor((x === lastX ? labelRoom : colW) / 8.5) - 6);
    // What a pointer can hit: the bar and its label, gaps between the words included.
    const mid = y + Math.min(h, 30) / 2, hitTop = Math.min(y, mid - 10);
    g.appendChild(s('rect', { class: 'sk-hit', x, y: hitTop, width: nodeW + 6 + room * 7.5, height: Math.max(y + h, mid + 10) - hitTop }));
    g.appendChild(s('rect', { class: 'sk-bar', x, y, width: nodeW, height: h, rx: 2, fill: color }));
    if (node.ends) {
      const eh = Math.min(h, node.ends * k);
      g.appendChild(s('rect', { x, y: y + h - eh, width: nodeW, height: eh, fill: cssVar('--surface'), opacity: 0.55 }));
    }
    const label = s('text', { class: 'sk-label', x: x + nodeW + 6, y: mid + 4 });
    const short = node.value.length > room ? node.value.slice(0, room - 1) + '…' : node.value;
    label.appendChild(s('tspan', {})).textContent = short;
    const count = s('tspan', { class: 'sk-count', dx: 6 });
    count.textContent = fmt.compact(node.visits);
    label.appendChild(count);
    g.appendChild(label);
    const show = (e) => {
      const r = g.getBoundingClientRect();
      const rows = [{ value: fmt.num(node.visits), label: 'visits' }];
      if (node.ends) rows.push({ value: fmt.num(node.ends), label: backward ? 'started here' : 'ended here' });
      if (node.other) rows.push({ value: fmt.num(node.pages || 0), label: 'pages grouped' });
      tip.show(e && e.clientX ? e.clientX : r.right, e && e.clientY ? e.clientY : r.top, node.value, rows, node.other ? null : 'Click: before and after this page');
    };
    g.addEventListener('pointermove', show);
    g.addEventListener('focus', show);
    g.addEventListener('pointerleave', () => tip.hide());
    g.addEventListener('blur', () => tip.hide());
    if (!node.other) {
      g.addEventListener('click', () => { tip.hide(); onNode(node); });
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter') { tip.hide(); onNode(node); } });
    }
    svg.appendChild(g);
  }
  container.replaceChildren(svg);
}

// ---- around a page -----------------------------------------------------------------------------------------------
async function transitions(root, ctx, page) {
  const data = await ctx.api('paths', { mode: 'transitions', page }, { compare: false });
  const go = (r) => { if (!r.other) ctx.go('paths', { mode: 'transitions', page: r.label }); };
  const asRows = (list) => list.map((r) => ({ label: r.value, value: r.visits, other: r.other, title: r.other ? fmt.plural(r.pages || 0, 'page') : r.value }));
  const before = card({ title: 'Before', span: 4, info: 'The page seen just before, or where the visit came from when it started here.' });
  const mid = card({ title: 'The page', span: 4, className: 'transition-mid' });
  const after = card({ title: 'After', span: 4, info: 'The page seen next, or the end of the visit.' });
  root.append(before.root, mid.root, after.root);
  const views = data.views || 0;
  mid.body.append(el('div', { class: 'transition-page' }, el('code', { class: 'wrap', text: page }),
    el('div', { class: 'transition-views' }, el('b', { text: fmt.num(views) }), el('span', { class: 'note', text: ' views' + (data.truncated ? ' (first visits only)' : '') }))));
  if (!views) {
    mid.body.appendChild(el('p', { class: 'note', text: 'No visit saw this page in this period.' }));
    return;
  }
  const share = (n) => fmt.pct(n / views, 1);
  mid.body.appendChild(el('ul', { class: 'transition-facts' },
    el('li', {}, el('b', { text: share(data.entrance_total) }), ' started their visit here'),
    el('li', {}, el('b', { text: share(views - data.entrance_total) }), ' came from another page'),
    el('li', {}, el('b', { text: share(data.exits) }), ' ended their visit here')));
  const open = el('a', { class: 'btn small ghost', href: hrefFor('heatmaps', { page }) }, icon('heat'), el('span', { text: 'Heatmap of this page' }));
  mid.body.appendChild(open);
  before.body.appendChild(el('h3', { class: 'sub-head', text: 'Started here, from' }));
  const ent = el('div');
  before.body.appendChild(ent);
  barList(ent, asRows(data.entrances), { valueLabel: 'Visits', format: fmt.num, total: views, empty: 'None', limit: 5 });
  before.body.appendChild(el('h3', { class: 'sub-head', text: 'Previous page' }));
  const prev = el('div');
  before.body.appendChild(prev);
  barList(prev, asRows(data.previous), { valueLabel: 'Views', format: fmt.num, total: views, onClick: go, empty: 'None', limit: 10 });
  const next = el('div');
  after.body.appendChild(el('h3', { class: 'sub-head', text: 'Next page' }));
  after.body.appendChild(next);
  barList(next, asRows(data.next), { valueLabel: 'Views', format: fmt.num, total: views, onClick: go, empty: 'None', limit: 10 });
  after.body.appendChild(el('h3', { class: 'sub-head', text: 'Left the site' }));
  const exitBox = el('div');
  after.body.appendChild(exitBox);
  barList(exitBox, [{ label: 'End of the visit', value: data.exits }], { valueLabel: 'Visits', format: fmt.num, total: views, empty: 'None' });
}
