// Realtime: who is on the site right now (raw hits of the last minutes, refreshed every 10 seconds).
import { barList, card, columnChart, el, table } from '../charts.js';
import * as fmt from '../fmt.js';

let timer = null;

export async function render(root, ctx) {
  clearInterval(timer);
  const now = card({ title: 'Visitors right now', span: 4 });
  const chart = card({ title: 'Pageviews per minute', span: 8, tableToggle: true });
  const pages = card({ title: 'Active pages', span: 6 });
  const refs = card({ title: 'Referrers', span: 6 });
  root.append(now.root, chart.root, pages.root, refs.root);
  let recent = null;
  if (!ctx.public) {
    recent = card({ title: 'Latest activity', span: 12 });
    root.appendChild(recent.root);
  }
  const draw = async () => {
    const d = await ctx.api('realtime');
    now.body.replaceChildren(el('div', { class: 'hero', text: fmt.num(d.active) }),
      el('p', { class: 'note', text: 'visitors in the last 5 minutes · updated ' + new Date().toLocaleTimeString() }));
    const labels = d.per_minute.map((p) => p.m);
    columnChart(chart.body, { labels, values: d.per_minute.map((p) => p.pageviews), name: 'Pageviews', height: 170,
      labelFormat: (m) => (m === 0 ? 'now' : m + ' min') });
    table(chart.alt, [{ key: 'm', label: 'Minute', render: (r) => (r.m === 0 ? 'now' : r.m + ' min') },
      { key: 'pageviews', label: 'Pageviews', num: true, format: fmt.num }, { key: 'visitors', label: 'Visitors', num: true, format: fmt.num }],
    d.per_minute, { sortKey: null });
    barList(pages.body, d.pages.map((p) => ({ label: p.value, value: p.pageviews })), { valueLabel: 'Views' });
    barList(refs.body, d.referrers.map((p) => ({ label: p.value, value: p.pageviews })), { valueLabel: 'Views' });
    if (recent) {
      table(recent.body, [
        { key: 'at', label: 'When', render: (r) => fmt.ago(r.at) },
        { key: 'type', label: 'Type' },
        { key: 'name', label: 'Name', format: (v) => v || '' },
        { key: 'path', label: 'Page', trunc: true },
      ], d.recent, { sortKey: null, empty: 'Nothing in the last minutes' });
    }
  };
  await draw();
  timer = setInterval(() => {
    if (!location.hash.startsWith('#/realtime')) { clearInterval(timer); return; }
    if (document.visibilityState === 'visible') draw().catch(() => {});
  }, 10000);
}
