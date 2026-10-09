// Goals: conversions and conversion rates per goal, the chosen goal's trend, and what converts (by channel, source,
// landing page, country, device). A chosen goal also adds conversion columns to every report.
import { card, el, icon, lineChart, table } from '../charts.js';
import { metric } from '../dims.js';
import { hint } from '../dom.js';
import { registerReport } from '../export.js';
import * as fmt from '../fmt.js';
import { hrefFor } from '../state.js';
import { segmented } from '../ui.js';
import { bucketFormat } from '../widgets.js';
import { reportPage } from './report-page.js';

export async function render(root, ctx) {
  if (!ctx.goals.length) {
    const c = card({ title: 'Goals', span: 12 });
    const box = el('div', { class: 'welcome' }, el('div', { class: 'welcome-mark' }, icon('target')),
      el('h2', { text: 'Measure what matters' }),
      el('p', { text: 'A goal is a visit that did something you care about: sent an event (a signup, a purchase) or reached a page (a thank-you page). Tidesight then shows conversions and conversion rates in every report.' }));
    if (ctx.siteInfo && ctx.siteInfo.managed && ctx.me && ctx.me.admin) {
      box.appendChild(el('a', { class: 'btn primary', href: hrefFor('property', { site: ctx.site, tab: 'goals' }) }, icon('plus'), el('span', { text: 'Set up goals' })));
    } else {
      box.appendChild(el('p', { class: 'note', text: 'Goals are part of the property settings' + (ctx.siteInfo && !ctx.siteInfo.managed ? ' (goals = [...] in the configuration file).' : '.') }));
    }
    c.body.appendChild(box);
    root.appendChild(c.root);
    return;
  }
  const res = await ctx.api('goals', { interval: ctx.params.get('interval') || undefined, goal: ctx.goal });
  registerReport({ label: 'Goals', endpoint: 'goals', params: ctx.query({}) });
  const selected = res.selected;
  const list = card({ title: 'Goals', span: 12, info: 'Conversions are visits that completed the goal; the rate is conversions per visit.' });
  const manage = el('a', { class: 'card-link', href: hrefFor('property', { site: ctx.site, tab: 'goals' }) }, icon('gear'), el('span', { text: 'Manage goals' }));
  if (ctx.siteInfo && ctx.siteInfo.managed) list.tools.appendChild(manage);
  const comparing = !!res.compare;
  const withDelta = (key, upIsGood = true) => (r) => {
    const m = metric(key);
    const v = el('span', {}, el('span', { text: m.format(r[key]) }));
    if (comparing) {
      const d = fmt.delta(r[key], (r.cmp || {})[key], upIsGood);
      const dEl = el('span', { class: 'delta small ' + (d ? d.cls : 'flat'), text: d ? ' ' + d.text : '' });
      if (d) hint(dEl, 'Compared period: ' + m.format((r.cmp || {})[key]));
      v.appendChild(dEl);
    }
    return v;
  };
  table(list.body, [
    { key: 'name', label: 'Goal', render: (r) => el('span', { class: 'goal-name' + (r.id === selected ? ' on' : '') }, r.id === selected ? icon('target') : null, r.name), exportValue: (r) => r.name },
    { key: 'type', label: 'Type', render: (r) => el('span', { class: 'badge', text: r.type === 'event' ? 'Event' : 'Page' }), exportValue: (r) => r.type },
    { key: 'match', label: 'Matches', render: (r) => el('code', { text: r.match }), exportValue: (r) => r.match },
    { key: 'conversions', label: 'Conversions', num: true, render: withDelta('conversions'), exportValue: (r) => r.conversions },
    { key: 'conversion_rate', label: 'Conversion rate', num: true, render: withDelta('conversion_rate'), exportValue: (r) => r.conversion_rate },
    { key: 'visitors', label: 'Converted visitors', num: true, format: fmt.num },
    { key: 'value', label: 'Value', num: true, format: (v) => (v ? fmt.dec(v, 2) : '—') },
  ], res.goals, { sortKey: null, exportName: ctx.site + '-goals-' + ctx.from + '_' + ctx.to,
    onRowClick: (r) => ctx.go('goals', { goal: r.id }) });
  list.body.appendChild(el('p', { class: 'note', text: 'Click a goal to chart it and to add its conversions to every report (Acquisition, Content…). ' +
    fmt.num(res.sessions) + ' visits in this period.' }));
  root.appendChild(list.root);

  const goal = res.goals.find((g) => g.id === selected);
  if (!goal) return;
  let which = ctx.params.get('metric') === 'conversion_rate' ? 'conversion_rate' : 'conversions';
  const pick = segmented([{ id: 'conversions', label: 'Conversions' }, { id: 'conversion_rate', label: 'Rate' }], which,
    (v) => { which = v; ctx.patch({ metric: v }); draw(); }, { small: true });
  const trend = card({ title: 'Trend · ' + goal.name, span: 12, actions: [pick] });
  root.appendChild(trend.root);
  const f = bucketFormat(res.interval);
  const draw = () => {
    const m = metric(which);
    const series = [{ name: fmt.range(ctx.from, ctx.to), values: res.series.map((p) => p[which]), color: '--s3', area: true, labels: res.series.map((p) => f.long(p.t)) }];
    if (res.compare && res.compare.series) {
      series.push({ name: fmt.range(res.compare.from, res.compare.to), dashed: true, color: 'prev',
        values: res.series.map((_, i) => (res.compare.series[i] || {})[which] ?? null), labels: res.series.map((_, i) => (res.compare.series[i] ? f.long(res.compare.series[i].t) : '')) });
    }
    lineChart(trend.body, { labels: res.series.map((p) => p.t), series, format: m.compact || m.format, labelFormat: f.short,
      titleFormat: series.length > 1 ? () => m.label : f.long, compareIndex: series.length > 1 ? 1 : null, height: 220 });
  };
  draw();
  if (ctx.goal !== selected) {
    // The breakdowns below, and the bar's goal chip, follow the goal in the state: render again with it.
    ctx.go('goals', { goal: selected }, true);
    return;
  }
  reportPage(root, ctx, { title: 'What converts', chart: false, tabs: [
    { dim: 'channel', label: 'Channels' }, { dim: 'source', label: 'Sources' }, { dim: 'campaign', label: 'Campaigns' },
    { dim: 'entry', label: 'Landing pages' }, { dim: 'country', label: 'Countries' }, { dim: 'device', label: 'Devices' }] });
}
