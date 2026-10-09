// Retention: visitors grouped by the week (or day) of their first visit, and how many came back.
import { card, heatTable } from '../charts.js';
import * as fmt from '../fmt.js';

export async function render(root, ctx) {
  const period = ctx.params.get('period_unit') || 'week';
  const c = card({ title: 'Returning visitors by cohort', span: 12, tabs: [{ key: 'week', label: 'Weekly' }, { key: 'day', label: 'Daily' }],
    onTab: (k) => ctx.go('retention', { period_unit: k }) });
  if (period === 'day') c.head.querySelectorAll('.tabs button').forEach((b, i) => b.setAttribute('aria-selected', i === 1 ? 'true' : 'false'));
  root.appendChild(c.root);
  const res = await ctx.api('retention', { period });
  const unit = period === 'day' ? 'Day' : 'Week';
  heatTable(c.body, {
    colLabels: Array.from({ length: 12 }, (_, i) => unit + ' ' + i),
    rows: res.cohorts.map((r) => ({ label: fmt.day(r.cohort), pre: [{ label: 'Visitors', text: fmt.num(r.visitors) }],
      cells: r.retention.map((v, i) => (i === 0 ? 1 : v)) })),
    format: (v) => fmt.pct(v),
    max: 1,
  });
  c.body.appendChild(Object.assign(document.createElement('p'), { className: 'note',
    textContent: 'Each row is the visitors first seen in that ' + unit.toLowerCase() + '; cells show the share who visited again in later ' + unit.toLowerCase() + 's.' }));
}
