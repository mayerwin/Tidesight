// One visit: who, from where, on what, and everything that happened, in order.
import { card, el, table } from '../charts.js';
import * as fmt from '../fmt.js';
import { labelFor } from '../widgets.js';

function kv(pairs) {
  const dl = el('dl', { class: 'kv' });
  for (const [k, v] of pairs) {
    if (v === null || v === undefined || v === '') continue;
    dl.append(el('dt', { text: k }), v instanceof Node ? el('dd', {}, v) : el('dd', { text: String(v) }));
  }
  return dl;
}

function describe(h) {
  const d = h.data || {};
  switch (h.type) {
    case 'pageview': return (d.title ? d.title + ' — ' : '') + (h.url || '').replace(/^https?:\/\/[^/]+/, '');
    case 'event': return (d.n || h.name || '') + (d.p ? ' ' + JSON.stringify(d.p) : '');
    case 'engagement': return fmt.duration(d.ms) + ' engaged (' + (d.why || '') + ')' + (d.sd ? ', scrolled ' + d.sd + '%' : '');
    case 'click': return (d.txt ? '“' + d.txt + '” ' : '') + (d.sel || '') + (d.href ? ' → ' + d.href : '');
    case 'rage': return 'rage click ×' + (d.n || '?') + ' on ' + (d.sel || '');
    case 'dead': return 'dead click on ' + (d.sel || '');
    case 'scroll': return 'scrolled ' + (d.d || d.max) + '%';
    case 'vital': return d.n + ' ' + (d.n === 'CLS' ? Number(d.val).toFixed(3) : fmt.ms(d.val)) + ' (' + (d.r || '') + ')';
    case 'error': return (d.k || 'error') + ': ' + (d.msg || d.res || '');
    case 'form': return 'form ' + (d.a || '') + ' ' + (d.f || '') + (d.fld ? ' · ' + d.fld : '');
    case 'identify': return 'identified as ' + (d.uid || '');
    case 'search': return 'searched “' + (d.q || '') + '”';
    default: return JSON.stringify(d).slice(0, 200);
  }
}

export async function render(root, ctx) {
  const key = ctx.params.get('key');
  const res = await ctx.rawApi('session', { site: ctx.site, key });
  const s = res.session;
  const info = card({ title: 'Visit', span: 5 });
  const visitorLink = el('a', { href: '#/visitor?' + new URLSearchParams({ site: ctx.site, key: s.visitor_key }), text: s.uid || s.visitor_key });
  info.body.appendChild(kv([
    ['Started', fmt.when(s.started_at)], ['Duration', fmt.duration(s.duration_ms)], ['Engaged', fmt.duration(s.engaged_ms)],
    ['Visitor', visitorLink], ['Visit number', s.session_number], ['Pages', s.pageviews], ['Events', s.events],
    ['Landing', s.entry_url], ['Referrer', s.referrer], ['Source', [s.source, s.medium].filter(Boolean).join(' / ')], ['Campaign', s.campaign],
    ['Channel', s.channel], ['Location', [s.city, s.region_name, labelFor('country', s.country)].filter(Boolean).join(', ')],
    ['Network', s.as_org], ['Device', [labelFor('device', s.device_type), s.device_brand, s.device_model].filter(Boolean).join(' · ')],
    ['Browser', [s.browser, s.browser_version].filter(Boolean).join(' ')], ['OS', [s.os, s.os_version].filter(Boolean).join(' ')],
    ['Screen', s.screen], ['Language', s.language], ['Time zone', s.timezone],
    ['Bot', s.is_bot ? (s.bot_kind || 'yes') + (s.bot_name ? ' · ' + s.bot_name : '') : 'no'],
  ]));
  if (s.has_replay) {
    const a = el('a', { class: 'btn primary', href: '#/replays?' + new URLSearchParams({ site: ctx.site, session: s.session_key }), text: 'Watch replay',
      style: { display: 'inline-flex', alignItems: 'center', marginTop: '14px', textDecoration: 'none' } });
    info.body.appendChild(a);
  }
  const tl = card({ title: 'Timeline', span: 7 });
  const list = el('ol', { class: 'timeline' });
  const t0 = res.timeline.length ? res.timeline[0].ts : s.started_at;
  for (const h of res.timeline) {
    if (h.dup) continue;
    list.appendChild(el('li', { class: h.type }, el('span', { class: 't', text: '+' + fmt.duration(h.ts - t0) }), el('span', { class: 'k' }),
      el('span', {}, el('b', { text: h.type + ' ' }), el('span', { text: describe(h) }))));
  }
  tl.body.appendChild(list);
  const pv = card({ title: 'Pages', span: 12 });
  table(pv.body, [
    { key: 'seq', label: '#', num: true }, { key: 'path', label: 'Page', trunc: true }, { key: 'title', label: 'Title', trunc: true },
    { key: 'engaged_ms', label: 'Engaged', num: true, format: fmt.duration }, { key: 'scroll_max', label: 'Scroll', num: true, format: (v) => (v === null ? '—' : v + '%') },
    { key: 'duration_ms', label: 'On page', num: true, format: fmt.duration }, { key: 'nav', label: 'Navigation' },
  ], res.pageviews, { sortKey: null });
  root.append(info.root, tl.root, pv.root);
}
