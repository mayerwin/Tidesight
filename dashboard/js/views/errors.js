// Errors: JavaScript errors, unhandled rejections and failed resources, grouped; occurrences with stacks.
import { card, el, table } from '../charts.js';
import * as fmt from '../fmt.js';

export async function render(root, ctx) {
  const res = await ctx.api('errors', { limit: 200 });
  const c = card({ title: 'Error groups', span: 12 });
  root.appendChild(c.root);
  const grp = ctx.params.get('grp');
  table(c.body, [
    { key: 'msg', label: 'Message', trunc: true, render: (r) => r.msg || r.res || '(no message)' },
    { key: 'kind', label: 'Kind' },
    { key: 'src', label: 'Source', trunc: true, render: (r) => {
      const where = r.src || r.res;
      return where ? where.replace(/^https?:\/\/[^/]+/, '') + (r.line ? ':' + r.line : '') : '—';
    } },
    { key: 'occurrences', label: 'Occurrences', num: true, format: fmt.num },
    { key: 'sessions', label: 'Visits', num: true, format: fmt.num },
    { key: 'last_seen', label: 'Last seen', num: true, format: (v) => fmt.when(v) },
  ], res.rows, { sortKey: 'occurrences', onRowClick: (r) => ctx.go('errors', { grp: r.grp }), empty: 'No errors in this period.' });
  if (!grp) return;
  const occ = await ctx.api('errors', { grp });
  const d = card({ title: 'Occurrences', span: 12 });
  root.appendChild(d.root);
  for (const o of occ.occurrences.slice(0, 25)) {
    const head = el('div', { style: { display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'baseline' } },
      el('b', { text: o.msg || o.res || '(no message)' }), el('span', { class: 'note', text: fmt.when(o.ts) }),
      el('span', { class: 'badge', text: [o.browser, o.os, o.device_type].filter(Boolean).join(' · ') || 'unknown device' }),
      el('a', { href: '#/session?' + new URLSearchParams({ site: ctx.site, key: o.session_key }), text: 'Open visit' }));
    const block = el('div', { style: { padding: '10px 0', borderBottom: '1px solid var(--grid)' } }, head,
      el('div', { class: 'note', text: (o.path || '') + (o.src ? ' · ' + o.src + (o.line ? ':' + o.line + ':' + (o.col || 0) : '') : '') }));
    if (o.stack) block.appendChild(el('pre', { class: 'stack mono', text: o.stack }));
    d.body.appendChild(block);
  }
}
