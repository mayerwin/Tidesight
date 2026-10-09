// Events: custom events with counts, visitors and values (a full report); one event's trend and the values of its
// properties.
import { barList, card, el, lineChart, table } from '../charts.js';
import * as fmt from '../fmt.js';
import { reportPage } from './report-page.js';

export async function render(root, ctx) {
  const name = ctx.params.get('event');
  if (name) return detail(root, ctx, name);
  reportPage(root, ctx, { title: 'Custom events', tabs: [{ dim: 'event', label: 'Events' }],
    emptyHint: 'No custom events in this period. Send them with tidesight("track", name, props, value).' });
  const help = card({ title: 'Event properties', span: 12 });
  help.body.appendChild(el('p', { class: 'note', text: 'Open an event to see its trend and the values of its properties: ' }));
  const list = el('div', { class: 'quick-dims' });
  help.body.appendChild(list);
  root.appendChild(help.root);
  try {
    const res = await ctx.api('breakdown', { dim: 'event', limit: 30 }, { compare: false });
    for (const r of res.rows) {
      const b = el('a', { class: 'pill', href: '#/events?' + new URLSearchParams({ ...Object.fromEntries(ctx.params), event: r.value }), text: r.value + ' · ' + fmt.compact(r.count) });
      list.appendChild(b);
    }
    if (!res.rows.length) list.appendChild(el('span', { class: 'note', text: 'None yet.' }));
  } catch (e) { list.textContent = e.message; }
}

async function detail(root, ctx, name) {
  const d = await ctx.api('events', { name }, { compare: false });
  const head = card({ title: 'Event', span: 12 });
  const back = el('a', { class: 'card-link', href: '#/events?' + new URLSearchParams([...ctx.params].filter(([k]) => k !== 'event')) }, '← All events');
  head.body.append(el('div', { class: 'event-head' }, el('code', { class: 'event-name', text: name }), el('span', { class: 'spacer' }),
    el('div', { class: 'kpi' }, el('span', { class: 'note', text: 'Count' }), el('b', { text: fmt.num(d.count) })),
    el('div', { class: 'kpi' }, el('span', { class: 'note', text: 'Total value' }), el('b', { text: d.total_value ? fmt.dec(d.total_value, 2) : '—' })),
    back));
  const filterBtn = el('button', { type: 'button', class: 'btn small', text: 'Filter reports to visits with this event' });
  filterBtn.addEventListener('click', () => ctx.go('overview', { filters: JSON.stringify([...ctx.filters.map((f) => [f.dim, f.op, f.values]), ['event', 'is', [name]]]) }));
  head.body.appendChild(filterBtn);
  root.appendChild(head.root);
  const trend = card({ title: 'Trend', span: 12, tableToggle: true });
  root.appendChild(trend.root);
  lineChart(trend.body, { labels: d.series.map((p) => p.t), series: [{ name: 'Count', values: d.series.map((p) => p.count), color: '--s1', area: true }],
    labelFormat: (x) => fmt.day(x), titleFormat: (x) => fmt.day(x, true), height: 200 });
  table(trend.alt, [{ key: 't', label: 'Day', render: (r) => fmt.day(r.t, true), exportValue: (r) => r.t }, { key: 'count', label: 'Count', num: true, format: fmt.num }],
    d.series, { sortKey: null, exportName: ctx.site + '-event-' + name });
  const keys = Object.keys(d.props);
  if (!keys.length) {
    const none = card({ title: 'Properties', span: 12 });
    none.body.appendChild(el('div', { class: 'empty', text: 'This event has no properties.' }));
    root.appendChild(none.root);
  }
  for (const k of keys.slice(0, 12)) {
    const c = card({ title: k, span: keys.length === 1 ? 12 : 6, tableToggle: true });
    barList(c.body, d.props[k].map((p) => ({ label: p.value, value: p.count })), { valueLabel: 'Count', format: fmt.num });
    table(c.alt, [{ key: 'value', label: k, trunc: true }, { key: 'count', label: 'Count', num: true, format: fmt.num }], d.props[k],
      { sortKey: 'count', exportName: ctx.site + '-event-' + name + '-' + k });
    root.appendChild(c.root);
  }
}
