// Overview: KPI tiles (selectable) over the main time series, then the main breakdowns.
import { card, el, lineChart, sparkline, table, worldMap } from '../charts.js';
import * as fmt from '../fmt.js';
import { breakdownCard, renderBreakdown, statTile } from '../widgets.js';

const METRICS = [
  { key: 'visitors', label: 'Visitors', series: 'visitors', format: fmt.compact, total: (t) => t.visitors },
  { key: 'sessions', label: 'Visits', series: 'sessions', format: fmt.compact, total: (t) => t.sessions },
  { key: 'pageviews', label: 'Pageviews', series: 'pageviews', format: fmt.compact, total: (t) => t.pageviews },
  { key: 'views_per_session', label: 'Views per visit', format: (v) => (v === null || v === undefined ? '—' : v.toFixed(2)), total: (t) => t.views_per_session },
  { key: 'bounce_rate', label: 'Bounce rate', series: 'bounce_rate', format: (v) => fmt.pct(v), total: (t) => t.bounce_rate, upIsGood: false },
  { key: 'avg_duration', label: 'Visit duration', series: 'avg_duration', format: fmt.duration, total: (t) => t.avg_duration },
];

export async function render(root, ctx) {
  const data = await ctx.api('overview');
  const totals = data.totals, prev = data.previous || {};
  const selected = ctx.params.get('metric') || 'visitors';
  const available = METRICS.filter((m) => totals[m.key] !== undefined && (m.key !== 'views_per_session' || totals.views_per_session !== undefined));
  const metric = available.find((m) => m.key === selected) || available[0];
  const seriesValue = (p, m) => (m.key === 'bounce_rate' ? (p.sessions ? p.bounces / p.sessions : null) : p[m.series || m.key]);

  const main = card({ title: '', span: 12, tableToggle: true });
  main.head.firstChild.remove();
  const tiles = el('div', { class: 'tiles' });
  for (const m of available) {
    const sparkVals = m.series ? data.series.map((p) => seriesValue(p, m) || 0) : null;
    tiles.appendChild(statTile({
      label: m.label, value: m.format(m.total(totals)), pressed: m.key === metric.key,
      delta: ctx.public ? null : fmt.delta(m.total(totals), m.total(prev), m.upIsGood !== false),
      spark: sparkVals && sparkVals.length > 2 ? sparkline(sparkVals, { width: 110, height: 22, color: m.key === metric.key ? '--s1' : '--other' }) : null,
      onClick: m.series ? () => ctx.go('overview', { metric: m.key }) : null,
    }));
  }
  main.body.appendChild(tiles);
  const chartBox = el('div');
  main.body.appendChild(chartBox);
  const labels = data.series.map((p) => p.t);
  const lf = data.interval === 'hour' ? (h) => String(h).padStart(2, '0') + ':00' : data.interval === 'day' ? (d) => fmt.day(d) : (x) => String(x);
  const values = data.series.map((p) => seriesValue(p, metric.series ? metric : METRICS[0]));
  const series = [{ name: metric.label, values, color: '--s1', area: true }];
  lineChart(chartBox, { labels, series, format: metric.series ? metric.format : fmt.compact, height: 260, labelFormat: lf,
    titleFormat: data.interval === 'day' ? (d) => fmt.day(d, true) : lf });
  table(main.alt, [
    { key: 't', label: data.interval === 'day' ? 'Day' : data.interval === 'hour' ? 'Hour' : 'Period', render: (r) => lf(r.t) },
    { key: 'visitors', label: 'Visitors', num: true, format: fmt.num },
    { key: 'sessions', label: 'Visits', num: true, format: fmt.num },
    { key: 'pageviews', label: 'Pageviews', num: true, format: fmt.num },
    { key: 'bounces', label: 'Bounces', num: true, format: fmt.num },
    { key: 'avg_duration', label: 'Avg duration', num: true, format: fmt.duration },
  ].filter((c) => data.series.length === 0 || data.series[0][c.key] !== undefined || c.key === 't'), data.series, { sortKey: null });
  root.appendChild(main.root);

  const allowed = (ctx.siteInfo && ctx.siteInfo.public_metrics) || [];
  const show = (m) => !ctx.public || allowed.includes(m);
  if (show('pages')) {
    root.appendChild(breakdownCard(ctx, { title: 'Pages', span: 6, metric: 'pageviews', tabs: [
      { dim: 'page', label: 'Top pages', metric: 'pageviews' }, { dim: 'entry', label: 'Entry', metric: 'sessions' },
      { dim: 'exit', label: 'Exit', metric: 'sessions' }] }));
  }
  if (show('sources')) {
    root.appendChild(breakdownCard(ctx, { title: 'Sources', span: 6, tabs: [
      { dim: 'channel', label: 'Channels' }, { dim: 'source', label: 'Sources' }, { dim: 'referrer', label: 'Referrers' },
      { dim: 'campaign', label: 'Campaigns' }] }));
  }
  if (show('countries')) {
    const geo = card({ title: 'Locations', span: 6, tabs: [{ key: 'map', label: 'Map' }, { key: 'country', label: 'Countries' }, { key: 'city', label: 'Cities' }],
      onTab: (k) => drawGeo(k) });
    root.appendChild(geo.root);
    const drawGeo = async (k) => {
      if (k === 'map') {
        const res = await ctx.api('breakdown', { dim: 'country', limit: 300 });
        const vals = {};
        for (const r of res.rows) if (r.value && r.value !== '(none)') vals[r.value] = r.visitors;
        await worldMap(geo.body, { values: vals, label: 'Visitors', onClick: ctx.public ? null : (cc) => ctx.setFilter('country', cc) });
      } else {
        await renderBreakdown(ctx, geo.body, null, { dim: k, label: k === 'city' ? 'City' : 'Country' });
      }
    };
    await drawGeo('map');
  }
  if (show('devices')) {
    root.appendChild(breakdownCard(ctx, { title: 'Devices', span: 6, tabs: [
      { dim: 'device', label: 'Device' }, { dim: 'browser', label: 'Browser' }, { dim: 'os', label: 'OS' }, { dim: 'screen', label: 'Screen' }] }));
  }
  if (show('events') || !ctx.public) {
    root.appendChild(breakdownCard(ctx, { title: 'Events', span: 12, metric: 'count', tabs: [{ dim: 'event', label: 'Custom events', metric: 'count' }] }));
  }
}
