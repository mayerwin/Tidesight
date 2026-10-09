// Content: pages with views, engagement, scroll depth, entries and exits; entry and exit pages; titles; and the
// pages flagged as errors with the links that lead to them.
import { card, table } from '../charts.js';
import * as fmt from '../fmt.js';
import { reportPage } from './report-page.js';

export async function render(root, ctx) {
  reportPage(root, ctx, { title: 'Content', tabs: [
    { dim: 'page', label: 'Pages' }, { dim: 'entry', label: 'Entry pages' }, { dim: 'exit', label: 'Exit pages' },
    { dim: 'title', label: 'Page titles' }] });
  // Pages the site flagged as errors (<meta name="ts:status">), with the links that lead to them.
  const nf = card({ title: 'Not found and error pages', span: 12, info: 'Pages flagged by <meta name="ts:status" content="404"> or tidesight("status", 404).' });
  root.appendChild(nf.root);
  try {
    const errors = await ctx.api('not_found', { limit: 100 }, { compare: false });
    table(nf.body, [
      { key: 'value', label: 'Page', trunc: true },
      { key: 'status', label: 'Status', num: true },
      { key: 'pageviews', label: 'Views', num: true, format: fmt.num },
      { key: 'visitors', label: 'Visitors', num: true, format: fmt.num },
      { key: 'referrers', label: 'Coming from', sortable: false, trunc: true,
        format: (refs) => (refs.length ? refs.map((x) => x.value + ' (' + fmt.num(x.count) + ')').join(' · ') : '—') },
      { key: 'last_seen', label: 'Last seen', num: true, format: fmt.when },
    ], errors.rows, { sortKey: 'pageviews', exportName: ctx.site + '-error-pages-' + ctx.from + '_' + ctx.to,
      empty: 'No error pages in this period.' });
  } catch (e) {
    nf.body.textContent = e.message;
  }
}
