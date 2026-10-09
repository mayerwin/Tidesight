// Forms: starts, submissions and abandonment per form; time and drop-off per field (values are never recorded).
import { card, el, table } from '../charts.js';
import * as fmt from '../fmt.js';

export async function render(root, ctx) {
  const res = await ctx.api('forms');
  const overview = card({ title: 'Forms', span: 12 });
  root.appendChild(overview.root);
  table(overview.body, [
    { key: 'form', label: 'Form', trunc: true },
    { key: 'starts', label: 'Started', num: true, format: fmt.num },
    { key: 'submits', label: 'Submitted', num: true, format: fmt.num },
    { key: 'abandons', label: 'Abandoned', num: true, format: fmt.num },
    { key: 'conversion', label: 'Completion', num: true, format: (v) => fmt.pct(v, 1) },
  ], res.rows, { sortKey: 'starts', empty: 'No form interactions in this period.' });
  for (const f of res.rows.slice(0, 6)) {
    const c = card({ title: 'Fields · ' + f.form, span: 6 });
    table(c.body, [
      { key: 'field', label: 'Field', trunc: true },
      { key: 'interactions', label: 'Uses', num: true, format: fmt.num },
      { key: 'median_ms', label: 'Median time', num: true, format: fmt.duration },
      { key: 'filled_rate', label: 'Filled', num: true, format: (v) => fmt.pct(v) },
      { key: 'abandoned_here', label: 'Left here', num: true, format: fmt.num },
    ], f.fields, { sortKey: 'interactions' });
    root.appendChild(c.root);
  }
  if (!res.rows.length) overview.body.appendChild(el('p', { class: 'note', text: 'Form analytics needs the forms feature (on by default).' }));
}
