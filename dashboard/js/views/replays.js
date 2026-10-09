// Session replays: recorded visits (sampled) and an rrweb player. Inputs are masked at recording time.
import { card, el, table } from '../charts.js';
import * as fmt from '../fmt.js';
import { loadPlayer, sanitizeEvents } from '../player.js';
import { labelFor } from '../widgets.js';

export const attachFirst = true;

export async function render(root, ctx) {
  const session = ctx.params.get('session');
  if (session) return renderPlayer(root, ctx, session);
  const c = card({ title: 'Recorded visits', span: 12 });
  root.appendChild(c.root);
  const res = await ctx.api('replays', { limit: 200 });
  table(c.body, [
    { key: 'started_at', label: 'Started', format: (v) => fmt.when(v) },
    { key: 'entry_path', label: 'Landing page', trunc: true },
    { key: 'pageviews', label: 'Pages', num: true },
    { key: 'duration_ms', label: 'Duration', num: true, format: fmt.duration },
    { key: 'country', label: 'Location', render: (r) => fmt.flag(r.country) + ' ' + [r.city, labelFor('country', r.country)].filter(Boolean).join(', ') },
    { key: 'device_type', label: 'Device', render: (r) => [labelFor('device', r.device_type), r.browser].filter(Boolean).join(' · ') },
    { key: 'signals', label: '', sortable: false, render: (r) => el('span', {}, ...[
      r.errors ? el('span', { class: 'badge', text: r.errors + ' errors' }) : null,
      r.rage_clicks ? el('span', { class: 'badge', text: r.rage_clicks + ' rage' }) : null].filter(Boolean)) },
  ], res.rows, { sortKey: null, onRowClick: (r) => ctx.go('replays', { session: r.session_key }),
    empty: 'No recorded visits in this period. Session replay is off by default: enable features.replay (and its sample_rate) for the site.' });
}

async function renderPlayer(root, ctx, key) {
  const c = card({ title: 'Replay', span: 9 });
  const side = card({ title: 'Visit', span: 3 });
  root.append(c.root, side.root);
  const back = el('a', { href: '#/replays?' + new URLSearchParams({ site: ctx.site }), text: '← All recordings' });
  c.head.appendChild(back);
  const [rep, sess] = await Promise.all([ctx.rawApi('replay', { site: ctx.site, session: key }), ctx.rawApi('session', { site: ctx.site, key })]);
  const s = sess.session;
  const dl = el('dl', { class: 'kv' });
  for (const [k, v] of [['Started', fmt.when(s.started_at)], ['Duration', fmt.duration(s.duration_ms)], ['Pages', s.pageviews],
    ['Landing', s.entry_path], ['Source', s.channel], ['Location', [s.city, labelFor('country', s.country)].filter(Boolean).join(', ')],
    ['Device', [labelFor('device', s.device_type), s.browser, s.os].filter(Boolean).join(' · ')]]) {
    if (v === null || v === undefined || v === '') continue;
    dl.append(el('dt', { text: k }), el('dd', { text: String(v) }));
  }
  side.body.append(dl, el('p', {}, el('a', { href: '#/session?' + new URLSearchParams({ site: ctx.site, key }), text: 'Open the visit timeline' })));
  if (!rep.events.length) {
    c.body.appendChild(el('div', { class: 'empty', text: 'No replay data for this visit.' }));
    return;
  }
  const Player = await loadPlayer();
  const wrap = el('div', { class: 'player-wrap' });
  c.body.appendChild(wrap);
  const width = Math.max(320, Math.min(1100, wrap.clientWidth || 900));
  new Player({ target: wrap, props: { events: sanitizeEvents(rep.events), width, height: Math.round(width * 0.62), autoPlay: false, showController: true, skipInactive: true } });
  c.body.appendChild(el('p', { class: 'note', text: fmt.num(rep.events.length) + ' recorded events in ' + rep.chunks + ' chunks. Password fields are never recorded; other inputs are masked unless configured otherwise.' }));
  if (rep.truncated) c.body.appendChild(el('p', { class: 'note', text: 'This recording is larger than one view decodes: only its beginning is shown.' }));
}
