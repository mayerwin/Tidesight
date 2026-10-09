// Pages: views, engagement, scroll depth, entries and exits per page.
import { card, el, table } from '../charts.js';
import * as fmt from '../fmt.js';
import { breakdownCard } from '../widgets.js';

export async function render(root, ctx) {
  const c = card({ title: 'All pages', span: 12, tabs: [{ key: 'page', label: 'Paths' }, { key: 'title', label: 'Titles' }],
    onTab: (k) => draw(k) });
  root.appendChild(c.root);
  const draw = async (by) => {
    c.root.classList.add('loading');
    const res = by === 'title' ? await ctx.api('breakdown', { dim: 'title', limit: 500 }) : await ctx.api('pages', { limit: 500 });
    const cols = [
      { key: 'value', label: by === 'title' ? 'Title' : 'Page', trunc: true },
      { key: 'pageviews', label: 'Views', num: true, format: fmt.num },
      { key: 'visitors', label: 'Visitors', num: true, format: fmt.num },
      { key: 'avg_engaged', label: 'Engaged', num: true, format: fmt.duration },
      { key: 'avg_scroll', label: 'Scroll', num: true, format: (v) => (v === null || v === undefined ? '—' : v + '%') },
      { key: 'entries', label: 'Entries', num: true, format: fmt.num },
      { key: 'exit_rate', label: 'Exit rate', num: true, format: (v) => fmt.pct(v) },
      { key: 'bounce_rate', label: 'Bounce', num: true, format: (v) => fmt.pct(v) },
    ].filter((col) => !res.rows.length || res.rows[0][col.key] !== undefined);
    if (by !== 'title') {
      cols.push({ key: 'heat', label: '', sortable: false, render: (r) => {
        const a = el('a', { href: '#/heatmaps?' + new URLSearchParams({ site: ctx.site, page: r.value }), text: 'Heatmap' });
        a.addEventListener('click', (e) => e.stopPropagation());
        return a;
      } });
    }
    table(c.body, cols, res.rows, { sortKey: 'pageviews', onRowClick: by === 'title' ? null : (r) => ctx.setFilter('page', r.value) });
    c.root.classList.remove('loading');
  };
  await draw('page');
  root.appendChild(breakdownCard(ctx, { title: 'Entry pages', span: 6, tabs: [{ dim: 'entry', label: 'Entry', metric: 'sessions' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Exit pages', span: 6, tabs: [{ dim: 'exit', label: 'Exit', metric: 'sessions' }] }));

  // Pages the site flagged as errors (<meta name="ts:status">), with the links that lead to them.
  const nf = card({ title: 'Not found and error pages', span: 12 });
  root.appendChild(nf.root);
  const errors = await ctx.api('not_found', { limit: 100 });
  table(nf.body, [
    { key: 'value', label: 'Page', trunc: true },
    { key: 'status', label: 'Status', num: true },
    { key: 'pageviews', label: 'Views', num: true, format: fmt.num },
    { key: 'visitors', label: 'Visitors', num: true, format: fmt.num },
    { key: 'referrers', label: 'Coming from', sortable: false, trunc: true,
      format: (refs) => (refs.length ? refs.map((x) => x.value + ' (' + fmt.num(x.count) + ')').join(' · ') : '—') },
    { key: 'last_seen', label: 'Last seen', num: true, format: fmt.when },
  ], errors.rows, { sortKey: 'pageviews',
    empty: 'No error pages in this period. Pages are flagged by <meta name="ts:status" content="404"> or tidesight("status", 404).' });
}
