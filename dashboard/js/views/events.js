// Events: custom events with counts and visitors; one event's trend and property breakdowns.
import { barList, card, el, lineChart, table } from '../charts.js';
import * as fmt from '../fmt.js';

export async function render(root, ctx) {
  const list = card({ title: 'Custom events', span: 12 });
  root.appendChild(list.root);
  const res = await ctx.api('events', { limit: 200 });
  const name = ctx.params.get('event') || (res.rows[0] && res.rows[0].value);
  table(list.body, [
    { key: 'value', label: 'Event', trunc: true, render: (r) => {
      const b = el('span', { text: r.value });
      if (r.value === name) b.style.fontWeight = '600';
      return b;
    } },
    { key: 'count', label: 'Count', num: true, format: fmt.num },
    { key: 'visitors', label: 'Visitors', num: true, format: fmt.num },
    { key: 'sessions', label: 'Visits', num: true, format: fmt.num },
    { key: 'total_value', label: 'Value', num: true, format: (v) => (v ? fmt.num(v) : '—') },
  ], res.rows, { sortKey: 'count', onRowClick: (r) => ctx.go('events', { event: r.value }), empty: 'No custom events in this period. Send them with tidesight("track", name, props).' });
  if (!name) return;

  const detail = await ctx.api('events', { name });
  const trend = card({ title: 'Trend · ' + name, span: 12, tableToggle: true });
  root.appendChild(trend.root);
  lineChart(trend.body, { labels: detail.series.map((p) => p.t), series: [{ name: 'Count', values: detail.series.map((p) => p.count), color: '--s1', area: true }],
    labelFormat: (d) => fmt.day(d), titleFormat: (d) => fmt.day(d, true), height: 200 });
  table(trend.alt, [{ key: 't', label: 'Day', render: (r) => fmt.day(r.t, true) }, { key: 'count', label: 'Count', num: true, format: fmt.num }],
    detail.series, { sortKey: null });
  const keys = Object.keys(detail.props);
  if (!keys.length) {
    const none = card({ title: 'Properties', span: 12 });
    none.body.appendChild(el('div', { class: 'empty', text: 'This event has no properties.' }));
    root.appendChild(none.root);
  }
  for (const k of keys.slice(0, 12)) {
    const c = card({ title: k, span: keys.length === 1 ? 12 : 6 });
    barList(c.body, detail.props[k].map((p) => ({ label: p.value, value: p.count })), { valueLabel: 'Count', format: fmt.num });
    root.appendChild(c.root);
  }
}
