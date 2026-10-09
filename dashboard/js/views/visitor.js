// A visitor: stitched identity (with confidence) and every visit.
import { card, el, table } from '../charts.js';
import * as fmt from '../fmt.js';
import { labelFor } from '../widgets.js';

export async function render(root, ctx) {
  const key = ctx.params.get('key');
  const res = await ctx.rawApi('visitor', { site: ctx.site, key });
  const v = res.visitor;
  const info = card({ title: 'Visitor', span: 4 });
  const dl = el('dl', { class: 'kv' });
  for (const [k, val] of [['User id', v.uid], ['First seen', fmt.when(v.first_seen)], ['Last seen', fmt.when(v.last_seen)],
    ['Visits', fmt.num(v.sessions)], ['Pageviews', fmt.num(v.pageviews)], ['Engaged', fmt.duration(v.engaged_ms)],
    ['Country', labelFor('country', v.country)], ['Device', [labelFor('device', v.device_type), v.browser, v.os].filter(Boolean).join(' · ')],
    ['Identity confidence', fmt.pct(v.confidence)]]) {
    if (val === null || val === undefined || val === '') continue;
    dl.append(el('dt', { text: k }), el('dd', { text: String(val) }));
  }
  info.body.appendChild(dl);
  const ids = card({ title: 'Linked browser ids', span: 8 });
  table(ids.body, [{ key: 'vid', label: 'Visitor id', render: (r) => el('code', { text: r.vid }) }, { key: 'first_seen', label: 'First seen', format: (x) => fmt.when(x) },
    { key: 'confidence', label: 'Confidence', num: true, format: (x) => fmt.pct(x) }], res.ids, { sortKey: 'first_seen', sortDir: 1 });
  ids.body.appendChild(el('p', { class: 'note', text: 'Ids are linked by a shared user id, a cookie/storage conflict in one browser, or the same fingerprint on the same day and network.' }));
  const visits = card({ title: 'Visits', span: 12 });
  table(visits.body, [
    { key: 'started_at', label: 'Started', format: (x) => fmt.when(x) }, { key: 'entry_path', label: 'Landing page', trunc: true },
    { key: 'pageviews', label: 'Pages', num: true }, { key: 'duration_ms', label: 'Duration', num: true, format: fmt.duration },
    { key: 'channel', label: 'Channel' }, { key: 'city', label: 'Location', render: (r) => [r.city, labelFor('country', r.country)].filter(Boolean).join(', ') },
    { key: 'browser', label: 'Browser' },
  ], res.sessions, { sortKey: null, onRowClick: (r) => ctx.go('session', { key: r.session_key }) });
  root.append(info.root, ids.root, visits.root);
}
