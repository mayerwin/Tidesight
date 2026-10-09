// Site search: what visitors searched for (from URL parameters and tidesight('search', …)).
import { card, table } from '../charts.js';
import * as fmt from '../fmt.js';

export async function render(root, ctx) {
  const c = card({ title: 'Search queries', span: 12 });
  root.appendChild(c.root);
  const res = await ctx.api('search', { limit: 500 });
  table(c.body, [
    { key: 'value', label: 'Query', trunc: true },
    { key: 'searches', label: 'Searches', num: true, format: fmt.num },
    { key: 'visitors', label: 'Visitors', num: true, format: fmt.num },
    { key: 'avg_results', label: 'Avg results', num: true, format: (v) => (v === null || v === undefined ? '—' : v.toFixed(1)) },
  ], res.rows, { sortKey: 'searches', empty: 'No site searches in this period. Configure search_params or call tidesight("search", query, results).' });
}
