// Search engines: what people searched for on Google and Bing before they came (Search Console and Bing Webmaster
// Tools, imported by the scheduled jobs), with the visits Tidesight recorded from those searches.
import { card, el, icon, lineChart, sparkline, table } from '../charts.js';
import { hint } from '../dom.js';
import * as fmt from '../fmt.js';
import { hrefFor } from '../state.js';
import { segmented } from '../ui.js';
import { statTile } from '../widgets.js';

const NAMES = { google: 'Google', bing: 'Bing' };
const position = (v) => (v === null || v === undefined ? '—' : v.toFixed(1));
const ctr = (v) => (v === null || v === undefined ? '—' : fmt.pct(v, 1));
const METRICS = [
  { key: 'clicks', label: 'Clicks', format: fmt.num, compact: fmt.compact, help: 'Clicks from the search results to the site.' },
  { key: 'impressions', label: 'Impressions', format: fmt.num, compact: fmt.compact, help: 'Times the site appeared in search results.' },
  { key: 'ctr', label: 'CTR', format: ctr, help: 'Clicks per impression.' },
  { key: 'position', label: 'Avg. position', format: position, up: false, help: 'The average rank of the site’s best result (1 is the top). Lower is better.' },
  { key: 'visits', label: 'Visits', format: fmt.num, compact: fmt.compact, help: 'Visits Tidesight recorded from this engine’s organic results (filters apply).' },
];
const TABS = { queries: 'Queries', pages: 'Pages', countries: 'Countries', devices: 'Devices' };

export async function render(root, ctx) {
  const p = ctx.params;
  const tab = TABS[p.get('tab')] ? p.get('tab') : 'queries';
  const data = await ctx.api('seo', { engine: p.get('engine'), tab, query: p.get('query'), page: p.get('page'), goal: ctx.goal });
  if (!data.engine) { root.appendChild(setup(ctx, data)); return; }
  const engine = data.engine;
  const name = NAMES[engine];

  const main = card({ title: '', span: 12, className: 'overview-main' });
  main.head.remove();
  const top = el('div', { class: 'seo-top' });
  const usable = data.engines.filter((e) => e.last_day);
  if (usable.length > 1) {
    top.appendChild(segmented(usable.map((e) => ({ id: e.id, label: e.label })), engine, (v) => ctx.go('seo', { engine: v, query: null, page: null }), { label: 'Search engine' }));
  } else {
    top.appendChild(el('span', { class: 'badge accent', text: name }));
  }
  for (const [k, label] of [['query', 'Query'], ['page', 'Page']]) {
    if (!data[k]) continue;
    const x = el('button', { type: 'button', class: 'chip-x', 'aria-label': 'Show every ' + label.toLowerCase() }, icon('close'));
    x.addEventListener('click', () => ctx.go('seo', { [k]: null }));
    top.appendChild(el('span', { class: 'chip' }, el('span', { class: 'chip-main' }, el('span', { class: 'chip-dim', text: label + ': ' }), el('b', { text: data[k] })), x));
  }
  top.appendChild(el('span', { class: 'spacer' }));
  const notes = [];
  const engineInfo = data.engines.find((e) => e.id === engine);
  if (engine === 'google') notes.push('Google’s figures arrive two to three days late and count Pacific Time days');
  else notes.push('Bing gives its query and page figures by week (dated on Saturdays)');
  if (engineInfo && engineInfo.last_day && data.days_late > 0) notes.push('the latest day imported is ' + fmt.day(engineInfo.last_day, true));
  if (ctx.filters.length) notes.push('filters apply to Tidesight’s visits only');
  const noteEl = el('span', { class: 'note', text: notes.join(' · ') + '.' });
  top.appendChild(noteEl);
  main.body.appendChild(top);

  const selected = METRICS.some((m) => m.key === p.get('metric')) ? p.get('metric') : 'clicks';
  const tiles = el('div', { class: 'tiles' });
  const prev = data.compare ? data.compare.totals : data.previous || {};
  for (const m of METRICS) {
    const value = data.totals[m.key];
    if (m.key === 'visits' && value === undefined) continue;
    const spark = m.key !== 'visits' && data.series.length > 2
      ? sparkline(data.series.filter((x) => x.clicks !== null).map((x) => x[m.key] || 0), { width: 110, height: 24, color: m.key === selected ? '--s1' : '--other' }) : null;
    tiles.appendChild(statTile({
      label: m.label, value: (m.compact || m.format)(value), pressed: m.key === selected,
      delta: fmt.delta(value, prev[m.key], m.up !== false),
      compareText: prev[m.key] === undefined || prev[m.key] === null ? null : 'was ' + (m.compact || m.format)(prev[m.key]),
      spark, help: m.label + ': ' + m.help,
      onClick: m.key === 'visits' ? () => ctx.go('sources', { dim: 'source' }) : () => ctx.go('seo', { metric: m.key }),
    }));
  }
  main.body.appendChild(tiles);
  const chartBox = el('div', { class: 'chart-box' });
  main.body.appendChild(chartBox);
  const md = METRICS.find((m) => m.key === selected);
  const series = [{ name: fmt.range(ctx.from, ctx.to), values: data.series.map((x) => x[selected]), color: '--s1', area: selected !== 'position',
    labels: data.series.map(() => md.label) }];
  if (data.compare) {
    const cs = data.compare.series;
    series.push({ name: fmt.range(data.compare.from, data.compare.to), dashed: true, color: 'prev',
      values: data.series.map((_, i) => (cs[i] ? cs[i][selected] : null)), labels: data.series.map((_, i) => (cs[i] ? fmt.day(cs[i].t, true) : '')) });
    series[0].labels = data.series.map((x) => fmt.day(x.t, true));
  }
  lineChart(chartBox, { labels: data.series.map((x) => x.t), series, height: 250, format: md.compact || md.format, labelFormat: (d) => fmt.day(d),
    titleFormat: series.length > 1 ? () => md.label : (d) => fmt.day(d, true), compareIndex: series.length > 1 ? 1 : null, upIsGood: md.up !== false });
  if (data.coverage !== null && data.coverage !== undefined && data.coverage < 0.995) {
    const cov = el('p', { class: 'note chart-note', text: fmt.pct(data.coverage) + ' of the clicks come with their query: ' + name + ' keeps rare queries to itself, for its users’ privacy.' });
    main.body.appendChild(cov);
  }
  root.appendChild(main.root);

  // The breakdown: queries, pages, countries, devices.
  const tabs = Object.entries(TABS).filter(([k]) => engine === 'google' || k === 'queries' || k === 'pages');
  const box = card({ title: 'Breakdown', span: 12, tabs: tabs.map(([key, label]) => ({ key, label })), active: tab, onTab: (k) => ctx.go('seo', { tab: k }) });
  root.appendChild(box.root);
  const comparing = !!data.compare;
  const withDelta = (key, fmtFn, up = true) => (r) => {
    const cell = el('span', { class: 'cell-delta' }, el('span', { text: fmtFn(r[key]) }));
    if (comparing && r.cmp) {
      const d = fmt.delta(r[key], r.cmp[key], up);
      if (d) cell.appendChild(hint(el('span', { class: 'delta small ' + d.cls, text: d.text }), 'Compared period: ' + fmtFn(r.cmp[key])));
    }
    return cell;
  };
  const label = { queries: 'Query', pages: 'Page', countries: 'Country', devices: 'Device' }[tab];
  const cols = [{
    key: 'value', label, trunc: true,
    render: (r) => (tab === 'countries' ? (fmt.flag(r.value) + ' ' + fmt.country(r.value)).trim() : tab === 'devices' ? r.value[0].toUpperCase() + r.value.slice(1)
      : tab === 'pages' ? pageCell(r) : r.value),
    exportValue: (r) => r.value,
  },
  { key: 'clicks', label: 'Clicks', num: true, render: withDelta('clicks', fmt.num), exportValue: (r) => r.clicks },
  { key: 'impressions', label: 'Impressions', num: true, render: withDelta('impressions', fmt.num), exportValue: (r) => r.impressions },
  { key: 'ctr', label: 'CTR', num: true, format: ctr },
  { key: 'position', label: 'Position', num: true, render: withDelta('position', position, false), exportValue: (r) => r.position }];
  if (tab === 'pages') {
    cols.push({ key: 'visits', label: 'Visits', num: true, format: fmt.num },
      { key: 'bounce_rate', label: 'Bounce rate', num: true, format: (v) => (v === null || v === undefined ? '—' : fmt.pct(v)) },
      { key: 'avg_engaged', label: 'Engaged time', num: true, format: (v) => (v === null || v === undefined ? '—' : fmt.duration(v)) });
    if (ctx.goal) cols.push({ key: 'conversions', label: (ctx.goalNames[ctx.goal] || 'Goal') + ' conversions', num: true, format: fmt.num });
  }
  const empty = (data.query || data.page) && (tab === 'countries' || tab === 'devices') ? name + ' gives countries and devices for the whole site only.'
    : engine === 'bing' && ((tab === 'pages' && data.query) || (tab === 'queries' && data.page)) ? 'Bing does not say which pages a query led to.'
      : 'No ' + label.toLowerCase() + ' in this period.';
  const onRow = tab === 'queries' ? (r) => ctx.go('seo', { query: r.value, tab: engine === 'google' ? 'pages' : 'queries', page: null })
    : tab === 'pages' ? (r) => ctx.go('seo', { page: r.value, tab: engine === 'google' ? 'queries' : 'pages', query: null }) : null;
  table(box.body, cols, data.rows, { sortKey: 'clicks', onRowClick: onRow, empty, pageSize: 50,
    exportName: ctx.site + '-' + engine + '-' + tab + '-' + ctx.from + '_' + ctx.to });
  const foot = [];
  if (data.total_rows > data.rows.length) foot.push('The first ' + fmt.num(data.rows.length) + ' of ' + fmt.num(data.total_rows) + ', by clicks.');
  if (onRow) foot.push(tab === 'queries' ? 'Click a query for the pages it led to.' : 'Click a page for its queries.');
  if (tab === 'pages') foot.push('Visits, bounce rate and engaged time: the visits Tidesight recorded from ' + name + '’s organic results that started on the page.');
  if (foot.length) box.body.appendChild(el('p', { class: 'note', text: foot.join(' ') }));
}

function pageCell(r) {
  const open = el('a', { class: 'icon-btn small', href: r.value, target: '_blank', rel: 'noopener noreferrer', 'aria-label': 'Open the page' }, icon('external'));
  open.addEventListener('click', (e) => e.stopPropagation());
  return el('span', { class: 'page-cell' }, el('span', { class: 'trunc', title: r.value, text: r.path || r.value }), open);
}

// No figures yet: how to get them.
function setup(ctx, data) {
  const admin = ctx.me && ctx.me.admin;
  const linked = (data.engines || []).filter((e) => e.linked);
  const box = el('div', { class: 'welcome card span-12' }, el('div', { class: 'welcome-mark' }, icon('search')));
  if (linked.length) {
    box.append(el('h2', { text: 'The first import is on its way' }),
      el('p', { text: linked.map((e) => e.label).join(' and ') + (linked.length > 1 ? ' are' : ' is') + ' linked to this property. The scheduled jobs import the figures within a few minutes; Google’s 16 months of history follow within the hour.' }));
    if (admin) box.appendChild(el('a', { class: 'btn', href: hrefFor('property', { site: ctx.site, tab: 'search' }) }, icon('gear'), el('span', { text: 'See the import' })));
  } else {
    box.append(el('h2', { text: 'See what people search for' }),
      el('p', { text: 'Google and Bing are the only ones who know the searches that bring visitors. Connect Google Search Console and Bing Webmaster Tools (free, about ten minutes, step by step) and this page shows the queries, clicks, impressions and positions, and the visits each landing page received.' }));
    box.appendChild(admin ? el('a', { class: 'btn primary', href: hrefFor('property', { site: ctx.site, tab: 'search' }) }, icon('plus'), el('span', { text: 'Connect search engines' }))
      : el('p', { class: 'note', text: 'Ask an admin to connect them (Property settings, Search engines).' }));
  }
  return box;
}
