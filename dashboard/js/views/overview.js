// Overview: KPI tiles (choose the charted metric), the trend with the comparison period and annotations, then the
// main breakdowns, each linking to its full report.
import { send } from '../api.js';
import { barList, card, el, icon, sparkline, table, worldMap } from '../charts.js';
import { metric } from '../dims.js';
import { hint } from '../dom.js';
import { registerReport } from '../export.js';
import * as fmt from '../fmt.js';
import { dialog, field, segmented, toast } from '../ui.js';
import { breakdownCard, bucketFormat, drawTrend, intervalsFor, renderBreakdown, seriesValue, statTile } from '../widgets.js';

const TILES = ['visitors', 'sessions', 'pageviews', 'views_per_session', 'bounce_rate', 'avg_duration'];

export async function render(root, ctx) {
  const interval = ctx.params.get('interval') || undefined;
  const data = await ctx.api('overview', { interval, goal: ctx.goal });
  if (!ctx.public) registerReport({ label: 'Trend', endpoint: 'overview', description: 'One row per ' + data.interval + ' with every metric' +
    (data.compare ? ', and the compared period' : ''), params: ctx.query({ interval: data.interval, goal: ctx.goal }) });
  const totals = data.totals, prev = data.compare ? data.compare.totals : data.previous || {};
  const keys = TILES.filter((k) => totals[k] !== undefined).concat(ctx.goal ? ['conversions', 'conversion_rate'] : []);
  const selected = keys.includes(ctx.params.get('metric')) ? ctx.params.get('metric') : keys[0];
  const vsText = data.compare ? 'vs ' + fmt.range(data.compare.from, data.compare.to) : 'vs the previous ' + fmt.plural(fmt.daysBetween(ctx.from, ctx.to) + 1, 'day');

  const main = card({ title: '', span: 12, tableToggle: true, className: 'overview-main' });
  main.head.remove(); // the tiles open the card; the table toggle sits with the chart's controls
  const tiles = el('div', { class: 'tiles' });
  for (const k of keys) {
    const m = metric(k);
    const sparkVals = data.series.map((p) => seriesValue(p, k) || 0);
    const cmpVals = data.compare ? data.series.map((_, i) => seriesValue(data.compare.series[i], k) || 0) : null;
    tiles.appendChild(statTile({
      label: m.label, value: (m.compact || m.format)(totals[k]), pressed: k === selected,
      delta: ctx.public ? null : fmt.delta(totals[k], prev[k], m.up !== false),
      compareText: ctx.public || prev[k] === undefined || prev[k] === null ? null : 'was ' + (m.compact || m.format)(prev[k]),
      spark: sparkVals.length > 2 ? sparkline(sparkVals, { width: 110, height: 24, color: k === selected ? '--s1' : '--other', compare: cmpVals }) : null,
      help: m.help ? m.label + ': ' + m.help : null,
      onClick: () => ctx.go('overview', { metric: k }),
    }));
  }
  main.body.appendChild(tiles);
  const chartHead = el('div', { class: 'chart-head' });
  if (!ctx.public) {
    chartHead.appendChild(el('span', { class: 'note', text: data.compare ? 'Solid: ' + fmt.range(ctx.from, ctx.to) + ' · dashed: ' + fmt.range(data.compare.from, data.compare.to)
      : 'Changes are ' + vsText + '. Use Compare to chart another period.' }));
    chartHead.appendChild(el('span', { class: 'spacer' }));
    const iv = segmented(intervalsFor(ctx).map((x) => ({ id: x.id, label: x.label, disabled: x.disabled })), data.interval,
      (v) => ctx.go('overview', { interval: v }), { label: 'Interval', small: true });
    chartHead.appendChild(iv);
    const note = el('button', { type: 'button', class: 'btn ghost small' }, icon('note'), el('span', { text: 'Annotations' + (data.annotations && data.annotations.length ? ' (' + data.annotations.length + ')' : '') }));
    hint(note, 'Notes on the chart: launches, campaigns, incidents. Double-click the chart to add one.');
    note.addEventListener('click', () => annotationsDialog(ctx, data));
    chartHead.appendChild(note);
  }
  if (ctx.public) chartHead.appendChild(el('span', { class: 'spacer' }));
  chartHead.appendChild(main.tools);
  main.body.appendChild(chartHead);
  const chartBox = el('div', { class: 'chart-box' });
  main.body.appendChild(chartBox);
  drawTrend(chartBox, ctx, data, selected, { height: 270, onAnnotate: ctx.public ? null : (t) => annotationsDialog(ctx, data, t) });
  const f = bucketFormat(data.interval);
  const cols = [{ key: 't', label: { hour: 'Hour', day: 'Day', week: 'Week', month: 'Month' }[data.interval] || 'Period', render: (r) => f.long(r.t), exportValue: (r) => r.t }];
  for (const k of ['visitors', 'sessions', 'pageviews', 'bounce_rate', 'avg_duration', 'avg_engaged'].concat(ctx.goal ? ['conversions'] : [])) {
    if (data.series.length && data.series[0][k] !== undefined) cols.push({ key: k, label: metric(k).label, num: true, format: metric(k).format });
  }
  table(main.alt, cols, data.series, { sortKey: null, exportName: ctx.site + '-trend-' + ctx.from + '_' + ctx.to });
  root.appendChild(main.root);

  const allowed = (ctx.siteInfo && ctx.siteInfo.public_metrics) || [];
  const show = (m) => !ctx.public || allowed.includes(m);
  if (show('sources')) {
    root.appendChild(breakdownCard(ctx, { title: 'Acquisition', span: 6, tabs: [
      { dim: 'channel', label: 'Channels' }, { dim: 'source', label: 'Sources' }, { dim: 'referrer', label: 'Referrers' },
      { dim: 'campaign', label: 'Campaigns' }], info: 'Where visits come from. Click a row to filter every report by it.' }));
  }
  if (show('pages')) {
    root.appendChild(breakdownCard(ctx, { title: 'Content', span: 6, tabs: [
      { dim: 'page', label: 'Top pages', metric: 'pageviews' }, { dim: 'entry', label: 'Entry', metric: 'sessions' },
      { dim: 'exit', label: 'Exit', metric: 'sessions' }] }));
  }
  if (show('countries')) {
    const geo = card({ title: 'Locations', span: 6, tabs: [{ key: 'map', label: 'Map' }, { key: 'country', label: 'Countries' },
      { key: 'region', label: 'Regions' }, { key: 'city', label: 'Cities' }], onTab: (k) => drawGeo(k) });
    root.appendChild(geo.root);
    const drawGeo = async (k) => {
      geo.root.classList.add('loading');
      try {
        if (k === 'map') {
          const res = await ctx.api('breakdown', { dim: 'country', limit: 300 }, { compare: false });
          const vals = {};
          for (const r of res.rows) if (r.value && r.value !== '(none)') vals[r.value] = r.visitors;
          await worldMap(geo.body, { values: vals, label: 'Visitors', onClick: ctx.public ? null : (cc) => ctx.setFilter('country', cc) });
        } else {
          await renderBreakdown(ctx, geo.body, null, { dim: k, label: k });
        }
      } catch (e) {
        geo.body.replaceChildren(el('div', { class: 'empty', text: e.message }));
      } finally {
        geo.root.classList.remove('loading');
      }
    };
    drawGeo('map');
  }
  if (show('devices')) {
    root.appendChild(breakdownCard(ctx, { title: 'Technology', span: 6, tabs: [
      { dim: 'device', label: 'Devices' }, { dim: 'browser', label: 'Browsers' }, { dim: 'os', label: 'Systems' }, { dim: 'screen', label: 'Screens' }] }));
  }
  if (!ctx.public || show('events')) {
    root.appendChild(breakdownCard(ctx, { title: 'Events', span: ctx.goals.length && !ctx.public ? 6 : 12, metric: 'count', tabs: [{ dim: 'event', label: 'Custom events', metric: 'count' }] }));
  }
  if (!ctx.public && ctx.goals.length) root.appendChild(goalsCard(ctx));
}

function goalsCard(ctx) {
  const c = card({ title: 'Goals', span: 6, info: 'Visits that completed each goal (set them up in the property settings).' });
  const link = el('a', { class: 'card-link', href: '#/goals?' + new URLSearchParams({ site: ctx.site }) }, el('span', { text: 'Details' }), icon('arrow'));
  c.root.appendChild(el('div', { class: 'card-foot' }, link));
  ctx.api('goals').then((res) => {
    const rows = res.goals.map((g) => ({ label: g.name, value: g.conversions, prev: res.compare ? (g.cmp || {}).conversions : undefined,
      sub: fmt.pct(g.conversion_rate, 1), id: g.id }));
    barList(c.body, rows, { valueLabel: 'Conversions', format: fmt.num, onClick: (r) => ctx.go('goals', { goal: r.id }),
      empty: 'No conversions in this period' });
  }).catch((e) => c.body.replaceChildren(el('div', { class: 'empty', text: e.message })));
  return c.root;
}

// Notes shown on every chart of the property (marketing campaigns, releases, outages…).
function annotationsDialog(ctx, data, day) {
  const list = el('div', { class: 'ann-list' });
  const items = [...(data.annotations || [])];
  const draw = () => {
    list.replaceChildren();
    if (!items.length) list.appendChild(el('p', { class: 'note', text: 'No annotations in this period.' }));
    for (const a of items) {
      const del = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Delete' }, icon('trash'));
      del.addEventListener('click', async () => {
        try {
          await send('DELETE', 'annotation', {}, { id: a.id, site: ctx.site });
          items.splice(items.indexOf(a), 1);
          changed = true;
          draw();
        } catch (e) { toast(e.message, { kind: 'error' }); }
      });
      list.appendChild(el('div', { class: 'ann-row' }, el('span', { class: 'ann-day', text: fmt.day(a.day, true) }),
        el('span', { class: 'ann-text', text: a.text }), el('span', { class: 'note', text: a.created_by || '' }), del));
    }
  };
  let changed = false;
  const dayIn = el('input', { type: 'date', class: 'input', value: /^\d{4}-\d{2}-\d{2}$/.test(day || '') ? day : ctx.to, max: ctx.today });
  const text = el('input', { class: 'input wide', placeholder: 'e.g. Newsletter sent, price change, outage', maxlength: 500 });
  const add = el('button', { type: 'button', class: 'btn primary', text: 'Add' });
  add.addEventListener('click', async () => {
    if (!text.value.trim()) { text.focus(); return; }
    try {
      const a = await send('POST', 'annotations', { site: ctx.site, day: dayIn.value, text: text.value.trim() });
      items.push(a);
      items.sort((x, y) => (x.day < y.day ? -1 : 1));
      text.value = '';
      changed = true;
      draw();
    } catch (e) { toast(e.message, { kind: 'error' }); }
  });
  text.addEventListener('keydown', (e) => { if (e.key === 'Enter') add.click(); });
  draw();
  dialog({ title: 'Annotations', subtitle: 'Shared with everyone who sees this property', width: 620,
    body: el('div', { class: 'stack' }, el('div', { class: 'ann-add' }, field('Day', dayIn), field('Note', text, null, { wide: true }), add), list),
    onClose: () => { if (changed) ctx.go(ctx.view, {}, true); } });
}
