// Sessions: the latest visits matching the filters; each opens its timeline.
import { card, el, table } from '../charts.js';
import * as fmt from '../fmt.js';
import { labelFor } from '../widgets.js';

export async function render(root, ctx) {
  const offset = Number(ctx.params.get('offset') || 0);
  const c = card({ title: 'Visits', span: 12 });
  root.appendChild(c.root);
  const res = await ctx.api('sessions', { limit: 100, offset });
  table(c.body, [
    { key: 'started_at', label: 'Started', format: (v) => fmt.when(v) },
    { key: 'entry_path', label: 'Landing page', trunc: true },
    { key: 'pageviews', label: 'Pages', num: true, format: fmt.num },
    { key: 'duration_ms', label: 'Duration', num: true, format: fmt.duration },
    { key: 'channel', label: 'Channel', format: (v) => v || '—' },
    { key: 'country', label: 'Country', render: (r) => fmt.flag(r.country) + ' ' + labelFor('country', r.country) },
    { key: 'device_type', label: 'Device', render: (r) => [labelFor('device', r.device_type), r.browser].filter(Boolean).join(' · ') },
    { key: 'flags', label: '', sortable: false, render: (r) => el('span', {}, ...[
      r.is_new ? el('span', { class: 'badge', text: 'new' }) : null,
      r.has_replay ? el('span', { class: 'badge', text: 'replay' }) : null,
      r.errors ? el('span', { class: 'badge', text: r.errors + ' err' }) : null,
      r.is_bot ? el('span', { class: 'badge', text: 'bot' }) : null].filter(Boolean)) },
  ], res.rows, { sortKey: null, exportName: ctx.site + '-visits-' + ctx.from + '_' + ctx.to, searchable: true,
    onRowClick: (r) => ctx.go('session', { key: r.session_key }), empty: 'No visits in this period' + (ctx.filters.length ? ' for these filters.' : '.') });
  const nav = el('div', { style: { display: 'flex', gap: '8px', marginTop: '12px' } });
  if (offset > 0) { const p = el('button', { class: 'btn', type: 'button', text: 'Newer' }); p.addEventListener('click', () => ctx.go('sessions', { offset: Math.max(0, offset - 100) })); nav.appendChild(p); }
  if (res.rows.length === 100) { const n = el('button', { class: 'btn', type: 'button', text: 'Older' }); n.addEventListener('click', () => ctx.go('sessions', { offset: offset + 100 })); nav.appendChild(n); }
  c.body.appendChild(nav);
}
