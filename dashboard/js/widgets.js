// Reusable dashboard pieces built on charts.js.
import { barList, card, el, table } from './charts.js';
import * as fmt from './fmt.js';

export const DIM_LABEL = {
  page: 'Page', entry: 'Entry page', exit: 'Exit page', title: 'Page title', source: 'Source', medium: 'Medium',
  campaign: 'Campaign', channel: 'Channel', referrer: 'Referrer', country: 'Country', region: 'Region', city: 'City',
  device: 'Device', browser: 'Browser', os: 'Operating system', screen: 'Screen', language: 'Language', event: 'Event',
  browser_version: 'Browser version', os_version: 'OS version', timezone: 'Time zone', asn: 'Network', brand: 'Brand',
  model: 'Model', category: 'Source category', hour: 'Hour',
};

const DEVICE_LABEL = { desktop: 'Desktop', mobile: 'Mobile', tablet: 'Tablet', tv: 'TV', console: 'Console', wearable: 'Wearable', car: 'Car', bot: 'Bot', unknown: 'Unknown' };

export function labelFor(dim, value, row) {
  if (value === null || value === undefined || value === '(none)') {
    return dim === 'referrer' || dim === 'source' ? 'Direct / none' : '(not set)';
  }
  if (dim === 'country') return fmt.country(value);
  if (dim === 'device') return DEVICE_LABEL[value] || value;
  if (dim === 'city' && row && row.country) return value;
  if (dim === 'hour') return String(value).padStart(2, '0') + ':00';
  return String(value);
}

export function prefixFor(dim, row) {
  if (dim === 'country') return fmt.flag(row.value);
  if (dim === 'city' && row.country) return fmt.flag(row.country);
  return '';
}

// Draw the top values of one dimension into body (bar list) and alt (table twin); click-to-filter.
export async function renderBreakdown(ctx, body, alt, tab, { metric = 'visitors', limit = 10 } = {}) {
  const res = await ctx.api('breakdown', { dim: tab.dim, limit: 100 });
  const m = tab.metric || metric;
  const rows = res.rows.map((r) => ({ ...r, label: labelFor(tab.dim, r.value, r), value: r[m], raw: r.value }));
  barList(body, rows, {
    valueLabel: m === 'pageviews' ? 'Views' : m === 'count' ? 'Count' : m === 'sessions' ? 'Visits' : 'Visitors', limit,
    labelPrefix: (r) => prefixFor(tab.dim, { value: r.raw, country: r.country }),
    onClick: ctx.public ? null : (r) => ctx.setFilter(tab.filter || tab.dim, r.raw),
  });
  if (!alt) return;
  const cols = [{ key: 'value', label: tab.label, render: (r) => labelFor(tab.dim, r.value, r), trunc: true }];
  for (const [k, label, f] of [['visitors', 'Visitors', fmt.num], ['sessions', 'Visits', fmt.num], ['pageviews', 'Views', fmt.num],
    ['count', 'Count', fmt.num], ['bounce_rate', 'Bounce', (v) => fmt.pct(v)], ['avg_duration', 'Duration', fmt.duration],
    ['avg_engaged', 'Engaged', fmt.duration], ['avg_scroll', 'Scroll', (v) => (v === null || v === undefined ? '—' : v + '%')]]) {
    if (res.rows.length && res.rows[0][k] !== undefined) cols.push({ key: k, label, num: true, format: f });
  }
  table(alt, cols, res.rows, { sortKey: m, onRowClick: ctx.public ? null : (r) => ctx.setFilter(tab.filter || tab.dim, r.value) });
}

// A card listing the top values of one or more dimensions (tabs), with a table twin.
export function breakdownCard(ctx, { title, span = 6, tabs, metric = 'visitors', limit = 10 }) {
  let active = tabs[0];
  const c = card({ title, span, tabs: tabs.length > 1 ? tabs.map((t) => ({ key: t.dim, label: t.label })) : null, tableToggle: true,
    onTab: (key) => { active = tabs.find((t) => t.dim === key); load(); } });
  const load = async () => {
    c.root.classList.add('loading');
    try {
      await renderBreakdown(ctx, c.body, c.alt, active, { metric, limit });
    } catch (err) {
      c.body.replaceChildren(el('div', { class: 'empty', text: err.message }));
    } finally {
      c.root.classList.remove('loading');
    }
  };
  load();
  return c.root;
}

export function statTile({ label, value, delta, spark, pressed, onClick }) {
  const tile = el('button', { class: 'tile', type: 'button', 'aria-pressed': pressed ? 'true' : 'false' });
  tile.appendChild(el('div', { class: 'tile-label', text: label }));
  const v = el('div', { class: 'tile-value', text: value });
  if (delta) v.appendChild(el('span', { class: 'delta ' + delta.cls, title: 'vs previous period', text: ' ' + delta.text }));
  tile.appendChild(v);
  if (spark) tile.appendChild(spark);
  if (onClick) tile.addEventListener('click', onClick);
  return tile;
}

export function emptyNote(text) {
  return el('div', { class: 'empty', text });
}
