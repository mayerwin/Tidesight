// Status: storage, enrichment progress and flagged traffic.
import { card, el, table } from '../charts.js';
import * as fmt from '../fmt.js';

export async function render(root, ctx) {
  const st = await ctx.rawApi('status', {});
  const files = card({ title: 'Storage', span: 6 });
  table(files.body, [{ key: 'name', label: 'Database' }, { key: 'mb', label: 'Size', num: true, format: (v) => v + ' MB' }],
    Object.entries(st.files || {}).map(([name, mb]) => ({ name, mb })), { sortKey: null, empty: 'No database files yet.' });
  files.body.appendChild(el('p', { class: 'note', text: 'Storage mode: ' + st.storage_mode + ' · Tidesight ' + st.version }));
  const enrich = card({ title: 'Enrichment', span: 6 });
  const dl = el('dl', { class: 'kv' });
  const e = st.enrich || {};
  for (const [k, v] of [['Last run', e.updated_at ? fmt.when(e.updated_at) + ' (' + fmt.ago(e.updated_at) + ')' : 'never'],
    ['Last processed hit', e.last_id], ['Last hit received', st.last_hit ? fmt.when(st.last_hit) : '—'], ['Version', e.version]]) {
    dl.append(el('dt', { text: k }), el('dd', { class: k === 'Version' ? 'mono' : '', text: v === undefined || v === null ? '—' : String(v) }));
  }
  enrich.body.appendChild(dl);
  const flagged = card({ title: 'Flagged requests · last 24 hours', span: 12 });
  table(flagged.body, [{ key: 'status', label: 'Status' }, { key: 'n', label: 'Requests', num: true, format: fmt.num }],
    Object.entries(st.flagged_today || {}).map(([status, n]) => ({ status, n })), { sortKey: 'n', empty: 'Nothing flagged: every request matched a configured site and origin.' });
  flagged.body.appendChild(el('p', { class: 'note', text: 'Flagged requests (unknown site, wrong origin, malformed…) are stored but kept out of reports.' }));
  root.append(files.root, enrich.root, flagged.root);
}
