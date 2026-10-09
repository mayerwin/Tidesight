// Reusable dashboard pieces built on charts.js: breakdown cards, KPI tiles, the trend chart.
import { fileUrl } from './api.js';
import { barList, card, el, lineChart, table } from './charts.js';
import { DIM_LABEL, labelFor as dimLabel, metric, prefixFor as dimPrefix } from './dims.js';
import { hint, icon } from './dom.js';
import * as fmt from './fmt.js';
import { registerReport } from './export.js';
import { hrefFor } from './state.js';
import { PLOT_COLORS } from './table.js';
import { downloadUrl, menu } from './ui.js';

export const labelFor = (dim, value, row) => dimLabel(dim, value, row);
export const prefixFor = dimPrefix;

// A card listing the top values of one or more dimensions (tabs), with a table twin, deltas when comparing,
// click-to-filter, an export and a link to the full report.
export function breakdownCard(ctx, { title, span = 6, tabs, metric: defaultMetric = 'visitors', limit = 10, info }) {
  let active = tabs[0];
  const details = el('a', { class: 'card-link', href: hrefFor('report', { dim: active.dim }) }, el('span', { text: 'Details' }), icon('arrow'));
  const exp = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Export', 'aria-haspopup': 'menu' }, icon('download'));
  hint(exp, 'Export this list');
  const c = card({ title, span, info, tabs: tabs.length > 1 ? tabs.map((t) => ({ key: t.dim, label: t.label })) : null, tableToggle: !ctx.public,
    onTab: (key) => { active = tabs.find((t) => t.dim === key); details.href = hrefFor('report', { dim: active.dim }); load(); },
    actions: ctx.public ? [] : [exp] });
  if (!ctx.public) c.root.appendChild(el('div', { class: 'card-foot' }, details));
  exp.addEventListener('click', () => {
    const params = ctx.query({ dim: active.dim, goal: ctx.goal, limit: 100000 });
    const file = (format) => downloadUrl(fileUrl('breakdown', Object.assign({}, params, { format })));
    menu(exp, [{ heading: (DIM_LABEL[active.dim] || active.dim) + ': every row' },
      { label: 'Excel (.xlsx)', icon: 'download', onClick: () => file('xlsx') },
      { label: 'CSV', icon: 'download', onClick: () => file('csv') },
      { label: 'JSON', icon: 'download', onClick: () => file('json') }], { align: 'end' });
  });
  const load = async () => {
    c.root.classList.add('loading');
    try {
      await renderBreakdown(ctx, c.body, c.alt, active, { metric: defaultMetric, limit });
    } catch (err) {
      c.body.replaceChildren(el('div', { class: 'empty', text: err.message }));
    } finally {
      c.root.classList.remove('loading');
    }
  };
  load();
  return c.root;
}

// Draw the top values of one dimension into body (bar list) and alt (table twin).
export async function renderBreakdown(ctx, body, alt, tab, { metric: defaultMetric = 'visitors', limit = 10 } = {}) {
  const res = await ctx.api('breakdown', { dim: tab.dim, limit: 100 });
  if (!ctx.public) registerReport({ label: DIM_LABEL[tab.dim] || tab.label, endpoint: 'breakdown', params: ctx.query({ dim: tab.dim, goal: ctx.goal }) });
  let m = tab.metric || (res.scope === 'page' ? 'pageviews' : res.scope === 'event' ? 'count' : defaultMetric);
  if (res.rows.length && res.rows[0][m] === undefined) m = 'visitors'; // public pages carry visitors and pageviews only
  const md = metric(m);
  // Without a row in the compared period, a count was 0 then (so the change reads "new").
  const prevOf = (r) => (r.cmp && r.cmp[m] !== null && r.cmp[m] !== undefined ? r.cmp[m] : md.additive ? 0 : null);
  const rows = res.rows.map((r) => ({ ...r, label: dimLabel(tab.dim, r.value, r, ctx.goalNames), value: r[m], raw: r.value,
    prev: res.compare ? prevOf(r) : undefined }));
  barList(body, rows, {
    valueLabel: md.label, limit, total: res.totals ? res.totals[m] : undefined, upIsGood: md.up !== false,
    labelPrefix: (r) => dimPrefix(tab.dim, { value: r.raw, country: r.country }),
    onClick: ctx.public ? null : (r) => ctx.setFilter(tab.filter || tab.dim, r.raw),
    actions: ctx.public ? null : (r) => {
      const ex = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Exclude ' + r.label }, icon('minus'));
      hint(ex, 'Exclude ' + r.label);
      ex.addEventListener('click', (e) => { e.stopPropagation(); ctx.addFilter({ dim: tab.filter || tab.dim, op: 'is_not', values: [String(r.raw)] }); });
      return [ex];
    },
    empty: ctx.filters.length ? 'No data for these filters' : 'No data for this period',
  });
  if (!alt || ctx.public) return;
  const cols = [{ key: 'value', label: DIM_LABEL[tab.dim] || tab.label, render: (r) => dimLabel(tab.dim, r.value, r, ctx.goalNames), trunc: true }];
  for (const k of res.metrics || []) {
    if (k === 'conversion_rate' || k === 'conversions') continue;
    cols.push({ key: k, label: metric(k).short || metric(k).label, num: true, format: metric(k).format });
  }
  table(alt, cols, res.rows, { sortKey: m, exportName: ctx.site + '-' + tab.dim + '-' + ctx.from + '_' + ctx.to,
    onRowClick: ctx.public ? null : (r) => ctx.setFilter(tab.filter || tab.dim, r.value) });
}

// A KPI tile: label, value, change, sparkline (with the comparison dashed); pressed = shown in the chart.
export function statTile({ label, value, delta, spark, pressed, onClick, help, compareText }) {
  const tile = el('button', { class: 'tile', type: 'button', 'aria-pressed': pressed ? 'true' : 'false' });
  const lab = el('div', { class: 'tile-label', text: label });
  tile.appendChild(lab);
  const v = el('div', { class: 'tile-value' }, el('span', { text: value }));
  if (delta) v.appendChild(el('span', { class: 'delta ' + delta.cls, text: delta.text }));
  tile.appendChild(v);
  if (compareText) tile.appendChild(el('div', { class: 'tile-cmp', text: compareText }));
  if (spark) tile.appendChild(spark);
  if (help) hint(tile, help);
  if (onClick) tile.addEventListener('click', onClick);
  return tile;
}

export function emptyNote(text) {
  return el('div', { class: 'empty', text });
}

// Labels of time buckets for an interval.
export function bucketFormat(interval) {
  if (interval === 'hour') return { short: (h) => String(h).padStart(2, '0') + ':00', long: (h) => String(h).padStart(2, '0') + ':00' };
  if (interval === 'week') return { short: (d) => fmt.day(d), long: (d) => 'Week of ' + fmt.day(d, true) };
  if (interval === 'month') return { short: (d) => fmt.month(d, true), long: (d) => fmt.month(d) };
  return { short: (d) => fmt.day(d), long: (d) => fmt.day(d, true) };
}

// The intervals that make sense for a range (hours only for a single day: they are hours of the day).
export function intervalsFor(ctx) {
  const n = fmt.daysBetween(ctx.from, ctx.to) + 1;
  return [
    { id: 'hour', label: 'Hours', disabled: n > 1 }, { id: 'day', label: 'Days', disabled: n > 731 },
    { id: 'week', label: 'Weeks', disabled: n < 14 }, { id: 'month', label: 'Months', disabled: n < 45 },
  ];
}

// The trend of one metric (with the comparison period dashed), or of a few plotted rows of a report.
export async function trendChart(container, ctx, { metricKey = 'visitors', interval, plotted = [], dim, height = 260 }) {
  container.classList.add('loading');
  try {
    const md = metric(metricKey);
    if (plotted.length && dim) {
      const res = await ctx.api('breakdown_series', { dim, values: JSON.stringify(plotted.map((p) => p.value)), metric: metricKey, interval }, { compare: false });
      const f = bucketFormat(res.interval);
      lineChart(container, { labels: res.keys, height, format: md.compact || md.format, labelFormat: f.short, titleFormat: f.long, upIsGood: md.up !== false,
        series: res.series.map((sr, i) => ({ name: plotted[i] ? plotted[i].label : sr.value, values: sr.values, color: PLOT_COLORS[i] })) });
      return res;
    }
    const data = await ctx.api('overview', { interval, goal: ctx.goal });
    registerReport({ label: 'Trend', endpoint: 'overview', description: 'One row per ' + data.interval + ' with every metric',
      params: ctx.query({ interval: data.interval, goal: ctx.goal }) });
    drawTrend(container, ctx, data, metricKey, { height });
    return data;
  } finally {
    container.classList.remove('loading');
  }
}

export function seriesValue(p, key) {
  if (!p) return null;
  if (key === 'bounce_rate') return p.sessions ? p.bounces / p.sessions : null;
  if (key === 'views_per_session') return p.sessions ? p.pageviews / p.sessions : null;
  if (key === 'conversion_rate') return p.sessions ? (p.conversions || 0) / p.sessions : null;
  return p[key] === undefined ? null : p[key];
}

export function drawTrend(container, ctx, data, metricKey, { height = 260, onAnnotate } = {}) {
  const md = metric(metricKey);
  const f = bucketFormat(data.interval);
  const labels = data.series.map((p) => p.t);
  const series = [{ name: fmt.range(ctx.from, ctx.to), values: data.series.map((p) => seriesValue(p, metricKey)), color: '--s1', area: true,
    labels: data.series.map(() => md.label) }];
  if (data.compare && data.compare.series) {
    const cs = data.compare.series;
    series.push({ name: fmt.range(data.compare.from, data.compare.to), dashed: true, color: 'prev',
      values: labels.map((_, i) => seriesValue(cs[i], metricKey)), labels: labels.map((_, i) => (cs[i] ? f.long(cs[i].t) : '')) });
    series[0].labels = labels.map((t) => f.long(t));
  }
  const byBucket = {};
  for (const a of data.annotations || []) {
    let key = a.day;
    if (data.interval === 'week') { const d = fmt.parseDay(a.day); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); key = fmt.isoDay(d); }
    if (data.interval === 'month') key = a.day.slice(0, 8) + '01';
    if (data.interval === 'hour') continue;
    (byBucket[key] = byBucket[key] || []).push(a.text);
  }
  const annotations = Object.entries(byBucket).map(([key, items]) => ({ index: labels.indexOf(key), day: key, items })).filter((a) => a.index >= 0);
  lineChart(container, { labels, series, height, format: md.compact || md.format, labelFormat: f.short, annotations,
    titleFormat: series.length > 1 ? () => md.label : f.long, compareIndex: series.length > 1 ? 1 : null, upIsGood: md.up !== false, onAnnotate });
}

