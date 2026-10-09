// Performance: Core Web Vitals at the 75th percentile (as Google judges them), per page, and navigation timing.
import { card, cssVar, el, lineChart, table } from '../charts.js';
import * as fmt from '../fmt.js';

const METRICS = [
  { key: 'LCP', label: 'Largest Contentful Paint', format: fmt.ms, good: 2500, poor: 4000 },
  { key: 'INP', label: 'Interaction to Next Paint', format: fmt.ms, good: 200, poor: 500 },
  { key: 'CLS', label: 'Cumulative Layout Shift', format: (v) => (v === null || v === undefined ? '—' : v.toFixed(3)), good: 0.1, poor: 0.25 },
  { key: 'FCP', label: 'First Contentful Paint', format: fmt.ms, good: 1800, poor: 3000 },
  { key: 'TTFB', label: 'Time to First Byte', format: fmt.ms, good: 800, poor: 1800 },
];
const RATING = { good: { label: 'Good', icon: '●' }, ni: { label: 'Needs improvement', icon: '▲' }, poor: { label: 'Poor', icon: '■' } };

function statusBadge(rating) {
  if (!rating) return el('span', { class: 'note', text: 'No data' });
  const r = RATING[rating];
  return el('span', { class: 'status ' + rating }, el('i'), el('span', { text: r.label }));
}

export async function render(root, ctx) {
  const res = await ctx.api('performance');
  const tiles = card({ title: 'Core Web Vitals · 75th percentile', span: 12 });
  root.appendChild(tiles.root);
  const grid = el('div', { class: 'tiles', style: { gridTemplateColumns: 'repeat(5, minmax(0, 1fr))', borderBottom: '0' } });
  for (const m of METRICS) {
    const v = res.vitals[m.key];
    const tile = el('div', { class: 'tile', style: { cursor: 'default' } });
    tile.appendChild(el('div', { class: 'tile-label', text: m.key + ' · ' + m.label }));
    tile.appendChild(el('div', { class: 'tile-value', text: v ? m.format(v.p75) : '—' }));
    tile.appendChild(statusBadge(v && v.rating));
    if (v) {
      // Share of page loads per rating: status colours, each segment also named in the tooltip and the table.
      const total = (v.ratings.good || 0) + (v.ratings.ni || 0) + (v.ratings.poor || 0);
      const bar = el('div', { style: { display: 'flex', gap: '2px', height: '6px', marginTop: '10px' }, title:
        Object.keys(RATING).map((k) => RATING[k].label + ' ' + fmt.pct((v.ratings[k] || 0) / (total || 1))).join(' · ') });
      for (const k of ['good', 'ni', 'poor']) {
        const share = total ? (v.ratings[k] || 0) / total : 0;
        if (share > 0) bar.appendChild(el('span', { style: { flex: String(share), background: cssVar(k === 'good' ? '--good' : k === 'ni' ? '--warning' : '--critical'), borderRadius: '2px' } }));
      }
      tile.appendChild(bar);
      tile.appendChild(el('div', { class: 'note', text: fmt.num(v.count) + ' measurements' }));
    }
    grid.appendChild(tile);
  }
  tiles.body.appendChild(grid);

  const metric = ctx.params.get('vital') || 'LCP';
  const trend = card({ title: 'Daily p75', span: 12, tableToggle: true, tabs: METRICS.map((m) => ({ key: m.key, label: m.key })),
    onTab: (k) => ctx.go('performance', { vital: k }) });
  trend.head.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', b.textContent === metric ? 'true' : 'false'));
  root.appendChild(trend.root);
  const md = METRICS.find((m) => m.key === metric) || METRICS[0];
  const series = (res.vitals[metric] && res.vitals[metric].series) || [];
  if (series.length) {
    lineChart(trend.body, { labels: series.map((p) => p.t), series: [{ name: 'p75 ' + metric, values: series.map((p) => p.p75), color: '--s1' }],
      format: md.format, labelFormat: (d) => fmt.day(d), titleFormat: (d) => fmt.day(d, true), height: 200 });
    trend.body.appendChild(el('p', { class: 'note', text: 'Good ≤ ' + md.format(md.good) + ' · poor > ' + md.format(md.poor) }));
  } else {
    trend.body.appendChild(el('div', { class: 'empty', text: 'No ' + metric + ' measurements in this period.' }));
  }
  table(trend.alt, [{ key: 't', label: 'Day', render: (r) => fmt.day(r.t, true) }, { key: 'p75', label: 'p75', num: true, format: md.format }], series, { sortKey: null });

  const pages = card({ title: 'Pages', span: 8 });
  root.appendChild(pages.root);
  table(pages.body, [{ key: 'path', label: 'Page', trunc: true }, { key: 'count', label: 'Loads', num: true, format: fmt.num },
    ...METRICS.slice(0, 3).map((m) => ({ key: m.key, label: m.key, num: true, format: m.format }))], res.pages, { sortKey: 'count' });

  const timing = card({ title: 'Navigation timing · median', span: 4 });
  root.appendChild(timing.root);
  const names = { dns: 'DNS lookup', tcp: 'Connection', tls: 'TLS', ttfb: 'Server response', dl: 'Download', dom: 'DOM interactive',
    dcl: 'DOMContentLoaded', load: 'Load event', rt: 'Redirects' };
  const rows = Object.entries(names).filter(([k]) => res.timing[k]).map(([k, label]) => ({ label, median: res.timing[k].median, p75: res.timing[k].p75 }));
  table(timing.body, [{ key: 'label', label: 'Phase' }, { key: 'median', label: 'Median', num: true, format: fmt.ms }, { key: 'p75', label: 'p75', num: true, format: fmt.ms }],
    rows, { sortKey: null, empty: 'No navigation timing yet.' });
}
