// A report page: one or more dimensions (tabs), the rows' trend over time (the top rows, or those ticked in the
// table), and the full report table. Its state (tab, sort, search, page, second dimension, chart metric) is
// in the URL, so every report can be bookmarked and shared as it is.
import { card, el } from '../charts.js';
import { DIM_LABEL, labelFor, metric, SCOPE_METRICS, scopeOf } from '../dims.js';
import { reportTable } from '../table.js';
import { segmented } from '../ui.js';
import { trendChart } from '../widgets.js';

// tabs: [{dim, label}]; chart: false for a table alone
export function reportPage(root, ctx, { tabs, chart = true, title, emptyHint, tableSpan = 12 }) {
  const p = ctx.params;
  const dim = tabs.some((t) => t.dim === p.get('dim')) ? p.get('dim') : tabs[0].dim;
  const scope = scopeOf(dim, p.get('dim2'));
  const metrics = SCOPE_METRICS[scope].filter((m) => !['views_per_session', 'avg_value', 'new_visitors'].includes(m));
  let chartMetric = metrics.includes(p.get('metric')) ? p.get('metric') : metrics[0];
  let chartCard = null;
  let plotted = [];
  let explicit = false; // rows ticked by the user (else the top five of the table)
  if (chart) {
    const pick = segmented(metrics.slice(0, 6).map((m) => ({ id: m, label: metric(m).short || metric(m).label })), chartMetric,
      (m) => { chartMetric = m; ctx.patch({ metric: m }); drawChart(); }, { label: 'Metric', small: true });
    chartCard = card({ title: 'Over time', span: 12, actions: [pick], className: 'trend-card' });
    chartCard.body.append(el('div', { class: 'chart-box' }), el('p', { class: 'note chart-note' }));
    root.appendChild(chartCard.root);
  }
  let chartToken = 0;
  const drawChart = async () => {
    if (!chartCard) return;
    const mine = ++chartToken;
    const box = chartCard.body.firstChild;
    const note = chartCard.body.lastChild;
    try {
      await trendChart(box, ctx, { metricKey: chartMetric, plotted, dim, interval: p.get('interval') || undefined, height: 220 });
      if (mine !== chartToken) return;
      note.textContent = plotted.length
        ? (explicit ? 'The rows you ticked in the table.' : 'The top ' + plotted.length + ' rows of the table: tick rows to choose others.')
        : 'All rows together.';
    } catch (e) {
      if (mine === chartToken) box.replaceChildren(el('div', { class: 'empty', text: e.message }));
    }
  };
  const tableCard = card({ title: title || 'Report', span: tableSpan, className: 'report-card',
    tabs: tabs.length > 1 ? tabs.map((t) => ({ key: t.dim, label: t.label || DIM_LABEL[t.dim] })) : null, active: dim,
    onTab: (k) => ctx.go(ctx.view, { dim: k, dim2: null, sort: null, dir: null, q: null, offset: null, metric: null }, true) });
  root.appendChild(tableCard.root);
  const top = (data, st) => (!data || st.dim2 ? [] : data.rows.slice(0, 5).map((r) => ({ key: r.value + '\u0000', value: r.value,
    label: labelFor(dim, r.value, r, ctx.goalNames) })));
  const handle = reportTable(tableCard.body, ctx, {
    dim, dim2: p.get('dim2') || null, sort: p.get('sort'), dir: p.get('dir') || 'desc', q: p.get('q') || '',
    limit: Number(p.get('rows')) || 25, offset: Number(p.get('offset')) || 0, plot: chart, title: title || null, emptyHint,
    onState: (changes) => ctx.patch(changes),
    onPlot: (rows) => { explicit = rows.length > 0; plotted = explicit ? rows : top(handle.data, handle.state); drawChart(); },
    onData: (data, st) => { if (chart && !explicit) { plotted = top(data, st); drawChart(); } },
  });
  return handle;
}
