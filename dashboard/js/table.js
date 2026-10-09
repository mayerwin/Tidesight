// The report table: any dimension (and an optional second one) with sortable metric columns, search, paging,
// totals, shares of the total, comparison changes, a goal's conversions, row actions (filter in or out, open
// the report), plotting rows over time, a column chooser and exports of every matching row.
import { fileUrl } from './api.js';
import { DEFAULT_COLUMNS, DEFAULT_SORT, DIM_GROUPS, DIM_LABEL, EVENT_DIMS, isPath, labelFor, metric, PAGE_DIMS, prefixFor,
  REPORT_DIMS, SCOPE_METRICS, scopeOf } from './dims.js';
import { el, hint, icon, tip } from './dom.js';
import * as fmt from './fmt.js';
import { registerReport } from './export.js';
import { storage } from './state.js';
import { closePopover, copyText, debounce, downloadUrl, menu, popover, toDelimited } from './ui.js';

export const PLOT_COLORS = ['--s1', '--s2', '--s3', '--s4', '--s5'];

// opts: {dim, dim2, sort, dir, q, limit, offset, plot, onPlot, onState, title, compact, emptyHint}
export function reportTable(container, ctx, opts) {
  const st = {
    dim: opts.dim, dim2: opts.dim2 || null, sort: opts.sort || null, dir: opts.dir || 'desc', q: opts.q || '',
    limit: opts.limit || 25, offset: opts.offset || 0,
  };
  let plotted = opts.plotted ? [...opts.plotted] : [];
  let data = null;
  let token = 0;
  const root = el('div', { class: 'rt' });
  const tools = el('div', { class: 'rt-tools' });
  const wrap = el('div', { class: 'table-wrap rt-wrap' });
  const foot = el('div', { class: 'rt-foot' });
  root.append(tools, wrap, foot);
  container.replaceChildren(root);

  const scope = () => scopeOf(st.dim, st.dim2);
  const colKey = () => 'ts_dash_cols_' + scope();
  const columns = () => {
    const saved = storage.json(colKey(), null);
    const avail = SCOPE_METRICS[scope()];
    let cols = Array.isArray(saved) ? saved.filter((c) => avail.includes(c)) : DEFAULT_COLUMNS[scope()];
    if (!cols.length) cols = DEFAULT_COLUMNS[scope()];
    if (ctx.goal) cols = cols.concat(['conversions', 'conversion_rate']);
    return cols;
  };
  const persist = (changes) => { if (opts.onState) opts.onState(changes); };

  // ---- toolbar -----------------------------------------------------------------------------------
  const search = el('input', { class: 'input search', type: 'search', placeholder: 'Search ' + (DIM_LABEL[st.dim] || st.dim).toLowerCase() + '…',
    'aria-label': 'Search rows', value: st.q });
  search.addEventListener('input', debounce(() => { st.q = search.value.trim(); st.offset = 0; persist({ q: st.q || null, offset: null }); load(); }, 300));
  search.dataset.tableSearch = '1';
  const dim2Btn = el('button', { type: 'button', class: 'btn ghost small', 'aria-haspopup': 'dialog' });
  const drawDim2 = () => {
    dim2Btn.replaceChildren(icon(st.dim2 ? 'layers' : 'plus'), el('span', { text: st.dim2 ? DIM_LABEL[st.dim2] : 'Secondary dimension' }));
    dim2Btn.classList.toggle('active', !!st.dim2);
  };
  dim2Btn.addEventListener('click', () => pickDimension(dim2Btn, st.dim2, (d) => {
    st.dim2 = d; st.offset = 0; plotted = []; persist({ dim2: d, offset: null }); drawDim2(); load(); notifyPlot();
  }, { exclude: st.dim, allowNone: true, title: 'Break each row down by' }));
  const colsBtn = el('button', { type: 'button', class: 'btn ghost small' }, icon('columns'), el('span', { text: 'Columns' }));
  colsBtn.addEventListener('click', () => {
    const avail = SCOPE_METRICS[scope()];
    const chosen = new Set(columns().filter((c) => avail.includes(c)));
    const list = el('div', { class: 'col-chooser' }, el('div', { class: 'menu-heading', text: 'Metrics shown' }));
    for (const k of avail) {
      const id = 'col-' + k;
      const box = el('input', { type: 'checkbox', id, checked: chosen.has(k) || null });
      box.addEventListener('change', () => {
        if (box.checked) chosen.add(k); else chosen.delete(k);
        if (!chosen.size) { chosen.add(k); box.checked = true; return; }
        storage.set(colKey(), JSON.stringify(avail.filter((c) => chosen.has(c))));
        render();
      });
      list.appendChild(el('label', { class: 'check-row', for: id }, box, el('span', { text: metric(k).label })));
    }
    const reset = el('button', { type: 'button', class: 'link-btn', text: 'Reset to default' });
    reset.addEventListener('click', () => { storage.set(colKey(), null); render(); colsBtn.click(); colsBtn.click(); });
    list.appendChild(reset);
    popover(colsBtn, list, { align: 'end', width: 240 });
  });
  const exportBtn = el('button', { type: 'button', class: 'btn ghost small', 'aria-haspopup': 'menu' }, icon('download'), el('span', { text: 'Export' }));
  exportBtn.addEventListener('click', () => {
    const n = data ? data.total_rows : 0;
    const params = ctx.query({ dim: st.dim, dim2: st.dim2, sort: st.sort, dir: st.dir, q: st.q || null, goal: ctx.goal, limit: 100000 });
    const file = (format) => downloadUrl(fileUrl('breakdown', Object.assign({}, params, { format })));
    menu(exportBtn, [
      { heading: 'All ' + fmt.num(n) + ' rows' + (ctx.filters.length ? ', with your filters' : '') + (ctx.compare ? ' and the comparison' : '') },
      { label: 'Excel (.xlsx)', hint: 'With a sheet describing the export', icon: 'download', onClick: () => file('xlsx') },
      { label: 'CSV', hint: 'Any spreadsheet or tool', icon: 'download', onClick: () => file('csv') },
      { label: 'TSV', hint: 'Tab-separated', icon: 'download', onClick: () => file('tsv') },
      { label: 'JSON', hint: 'For scripts; includes what was exported', icon: 'download', onClick: () => file('json') },
      'sep',
      { label: 'Copy the rows on screen', hint: 'Paste into a spreadsheet', icon: 'copy', onClick: copyVisible },
    ], { align: 'end', width: 300 });
  });
  const rowsSel = el('select', { class: 'input small', 'aria-label': 'Rows per page' },
    ...[10, 25, 50, 100, 250].map((n) => el('option', { value: n, text: String(n), selected: n === st.limit })));
  rowsSel.addEventListener('change', () => { st.limit = Number(rowsSel.value); st.offset = 0; persist({ rows: st.limit, offset: null }); load(); });
  tools.append(el('div', { class: 'search-box' }, icon('search'), search), opts.noDim2 ? null : dim2Btn, el('span', { class: 'spacer' }), colsBtn, exportBtn);
  drawDim2();

  const copyVisible = () => {
    if (!data) return;
    const cols = columns();
    const head = [DIM_LABEL[st.dim] || st.dim].concat(st.dim2 ? [DIM_LABEL[st.dim2]] : []).concat(cols.map((c) => metric(c).label));
    const rows = data.rows.map((r) => [labelFor(st.dim, r.value, r, ctx.goalNames)].concat(st.dim2 ? [labelFor(st.dim2, r.value2, r, ctx.goalNames)] : [])
      .concat(cols.map((c) => { const v = r[c]; return v === null || v === undefined ? '' : v; })));
    copyText(toDelimited(head, rows, '\t'), 'Copied ' + rows.length + ' rows');
  };

  const notifyPlot = () => { if (opts.onPlot) opts.onPlot(plotted.slice()); };

  // ---- loading -------------------------------------------------------------------------------------
  const load = async () => {
    const mine = ++token;
    root.classList.add('loading');
    try {
      const res = await ctx.api('breakdown', { dim: st.dim, dim2: st.dim2, sort: st.sort, dir: st.dir, limit: st.limit,
        offset: st.offset, q: st.q || null, goal: ctx.goal });
      if (mine !== token) return;
      data = res;
      render();
      registerReport({ label: (opts.title ? opts.title + ': ' : '') + (DIM_LABEL[st.dim] || st.dim) + (st.dim2 ? ' × ' + DIM_LABEL[st.dim2] : ''),
        endpoint: 'breakdown', description: fmt.plural(res.total_rows, 'row') + (st.q ? ' matching “' + st.q + '”' : '') + ', sorted as on screen',
        params: ctx.query({ dim: st.dim, dim2: st.dim2, sort: st.sort, dir: st.dir, q: st.q || null, goal: ctx.goal }) });
      if (opts.onData) opts.onData(res, { ...st });
    } catch (e) {
      if (mine !== token) return;
      wrap.replaceChildren(el('div', { class: 'empty', text: e.message }));
      foot.replaceChildren();
    } finally {
      if (mine === token) root.classList.remove('loading');
    }
  };

  const sortKey = () => st.sort || (data && data.metrics.includes(DEFAULT_SORT[scope()]) ? DEFAULT_SORT[scope()] : (data ? data.metrics[0] : 'visitors'));

  const render = () => {
    if (!data) return;
    const cols = columns().filter((c) => data.metrics.includes(c));
    const comparing = !!data.compare;
    const sk = sortKey();
    const totals = data.totals || {};
    const cmpTotals = data.cmp_totals || {};
    const tbl = el('table', { class: 'data report' + (opts.compact ? ' compact' : '') });
    const thead = el('thead');
    const hr = el('tr');
    if (opts.plot) hr.appendChild(el('th', { class: 'sel', 'aria-label': 'Plot' }));
    const dimTh = (d, key) => {
      const th = el('th', { class: 'dim sortable', scope: 'col', tabindex: 0, 'aria-sort': st.sort === key ? (st.dir === 'asc' ? 'ascending' : 'descending') : null },
        el('span', { text: DIM_LABEL[d] || d }));
      if (st.sort === key) th.appendChild(icon(st.dir === 'asc' ? 'up' : 'down', 'sort-icon'));
      const go = () => setSort(key, st.sort === key && st.dir === 'asc' ? 'desc' : 'asc');
      th.addEventListener('click', go);
      th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
      return th;
    };
    hr.appendChild(dimTh(st.dim, 'value'));
    if (st.dim2) hr.appendChild(dimTh(st.dim2, 'value2'));
    for (const c of cols) {
      const m = metric(c);
      const th = el('th', { class: 'num sortable' + (c === sk ? ' sorted' : ''), scope: 'col', tabindex: 0,
        'aria-sort': c === sk ? (st.dir === 'asc' ? 'ascending' : 'descending') : null });
      if (c === sk) th.appendChild(icon(st.dir === 'asc' ? 'up' : 'down', 'sort-icon'));
      th.appendChild(el('span', { text: m.short || m.label }));
      if (m.help || m.short !== m.label) hint(th, m.label + (m.help ? ': ' + m.help : ''));
      const go = () => setSort(c, c === sk && st.dir === 'desc' ? 'asc' : 'desc');
      th.addEventListener('click', go);
      th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    tbl.appendChild(thead);
    const tbody = el('tbody');
    // Totals: every matching row, not only this page.
    if (opts.totals !== false) {
      const tr = el('tr', { class: 'totals' });
      if (opts.plot) tr.appendChild(el('td', { class: 'sel' }));
      tr.appendChild(el('td', { colspan: st.dim2 ? 2 : 1 }, el('b', { text: 'Total' }), el('span', { class: 'note', text: ' · ' + fmt.plural(data.total_rows, 'row') })));
      for (const c of cols) {
        const td = el('td', { class: 'num' }, el('b', { text: metric(c).format(totals[c]) }));
        if (comparing) td.appendChild(deltaEl(c, totals[c], cmpTotals[c]));
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    const maxOf = Math.max(1e-9, ...data.rows.map((r) => r[sk] || 0));
    data.rows.forEach((r, i) => {
      const tr = el('tr', { class: 'clickable', tabindex: 0 });
      const key = rowKey(r);
      if (opts.plot) {
        const on = plotted.some((p) => p.key === key);
        const idx = plotted.findIndex((p) => p.key === key);
        const box = el('input', { type: 'checkbox', 'aria-label': 'Plot this row', checked: on || null,
          disabled: !on && plotted.length >= PLOT_COLORS.length ? true : null });
        if (on) box.style.accentColor = getComputedStyle(document.documentElement).getPropertyValue(PLOT_COLORS[idx]);
        box.addEventListener('click', (e) => e.stopPropagation());
        box.addEventListener('change', () => {
          if (box.checked) plotted.push({ key, value: r.value, value2: r.value2, label: rowLabel(r) });
          else plotted = plotted.filter((p) => p.key !== key);
          notifyPlot();
          render();
        });
        tr.appendChild(el('td', { class: 'sel' }, box));
      }
      tr.appendChild(dimCell(st.dim, r.value, r, i));
      if (st.dim2) tr.appendChild(dimCell(st.dim2, r.value2, r, i, true));
      for (const c of cols) {
        const m = metric(c);
        const td = el('td', { class: 'num' + (c === sk ? ' sorted' : '') });
        if (c === sk && m.additive && !comparing) {
          td.appendChild(el('span', { class: 'cell-bar', style: { width: Math.max(1, ((r[c] || 0) / maxOf) * 100) + '%' } }));
        }
        td.appendChild(el('span', { class: 'cell-value', text: m.format(r[c]) }));
        if (comparing) {
          const before = r.cmp && r.cmp[c] !== null && r.cmp[c] !== undefined ? r.cmp[c] : m.additive ? 0 : null;
          td.appendChild(deltaEl(c, r[c], before));
        }
        else if (m.additive && totals[c] && c === sk) td.appendChild(el('span', { class: 'cell-share', text: fmt.pct((r[c] || 0) / totals[c]) }));
        tr.appendChild(td);
      }
      tr.addEventListener('click', (e) => { if (!e.target.closest('a, button, input')) ctx.setFilter(st.dim, r.value); });
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target === tr) ctx.setFilter(st.dim, r.value); });
      tbody.appendChild(tr);
    });
    if (!data.rows.length) {
      const span = cols.length + 1 + (st.dim2 ? 1 : 0) + (opts.plot ? 1 : 0);
      tbody.appendChild(el('tr', {}, el('td', { colspan: span, class: 'empty' },
        el('div', { text: st.q ? 'Nothing matches “' + st.q + '”.' : (opts.emptyHint || 'No data for this period' + (ctx.filters.length ? ' and these filters' : '') + '.') }))));
    }
    tbl.appendChild(tbody);
    wrap.replaceChildren(tbl);
    // Paging.
    const first = data.rows.length ? st.offset + 1 : 0, last = st.offset + data.rows.length;
    const prev = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Previous rows', disabled: st.offset <= 0 || null }, icon('left'));
    const next = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Next rows', disabled: last >= data.total_rows || null }, icon('right'));
    prev.addEventListener('click', () => { st.offset = Math.max(0, st.offset - st.limit); persist({ offset: st.offset || null }); load(); });
    next.addEventListener('click', () => { st.offset += st.limit; persist({ offset: st.offset }); load(); });
    foot.replaceChildren(el('span', { class: 'note' }, 'Rows per page '), rowsSel, el('span', { class: 'spacer' }),
      el('span', { class: 'note', text: fmt.num(first) + '–' + fmt.num(last) + ' of ' + fmt.num(data.total_rows) }), prev, next);
    if (opts.plot && plotted.length) {
      const clear = el('button', { type: 'button', class: 'link-btn', text: 'Clear plotted rows' });
      clear.addEventListener('click', () => { plotted = []; notifyPlot(); render(); });
      foot.insertBefore(clear, foot.children[2]);
    }
  };

  const rowKey = (r) => r.value + '\u0000' + (r.value2 ?? '');
  const rowLabel = (r) => labelFor(st.dim, r.value, r, ctx.goalNames) + (st.dim2 ? ' · ' + labelFor(st.dim2, r.value2, r, ctx.goalNames) : '');

  const setSort = (key, dir) => {
    st.sort = key; st.dir = dir; st.offset = 0;
    persist({ sort: key, dir, offset: null });
    load();
  };

  const deltaEl = (c, cur, prev) => {
    const m = metric(c);
    const d = fmt.delta(cur, prev, m.up !== false);
    const span = el('span', { class: 'delta small ' + (d ? d.cls : 'flat'), text: d ? d.text : '–' });
    hint(span, 'Compared period: ' + m.format(prev) + (ctx.compare ? ' (' + fmt.range(ctx.compare.from, ctx.compare.to) + ')' : ''));
    return span;
  };

  const dimCell = (d, value, row, i, second = false) => {
    const td = el('td', { class: 'dim' });
    const label = labelFor(d, value, row, ctx.goalNames);
    const pre = prefixFor(d, { value, country: row.country });
    const text = el('span', { class: 'dim-text' + (isPath(d) || d === 'page' ? ' path' : ''), title: label },
      pre ? el('span', { class: 'flag', text: pre + ' ' }) : null, label);
    if (!second && !opts.compact) text.prepend(el('span', { class: 'rank', text: String(st.offset + i + 1) }));
    td.appendChild(text);
    const acts = el('span', { class: 'row-actions' });
    const act = (name, label2, fn) => {
      const b = el('button', { type: 'button', class: 'icon-btn', 'aria-label': label2 }, icon(name));
      hint(b, label2);
      b.addEventListener('click', (e) => { e.stopPropagation(); tip.hide(); fn(); });
      acts.appendChild(b);
    };
    act('filter', 'Filter: only ' + label, () => ctx.setFilter(d, value));
    act('minus', 'Exclude ' + label, () => ctx.addFilter({ dim: d, op: 'is_not', values: [String(value)] }));
    if (opts.drill !== false && REPORT_DIMS.includes(d) && ctx.view !== 'report') act('arrow', 'Open the ' + (DIM_LABEL[d] || d) + ' report', () => ctx.go('report', { dim: d }));
    const domain = ctx.siteInfo && (ctx.siteInfo.domains || []).find((h) => !h.startsWith('*'));
    // Paths come from visitors' data: only a path proper ("/…", not "//host" or "@host") becomes a link.
    if ((d === 'page' || d === 'entry' || d === 'exit') && domain && typeof value === 'string' && /^\/(?![\/\\])/.test(value)) {
      const a = el('a', { class: 'icon-btn', href: 'https://' + domain + value, target: '_blank', rel: 'noopener noreferrer', 'aria-label': 'Open the page' }, icon('external'));
      hint(a, 'Open the page on ' + domain);
      acts.appendChild(a);
      if (d === 'page') act('heat', 'Heatmap of this page', () => ctx.go('heatmaps', { page: value }));
    }
    if (d === 'visitor' && value && value !== '(none)') act('user', 'Open this visitor', () => ctx.go('visitor', { key: value }));
    td.appendChild(acts);
    return td;
  };

  load();
  return {
    reload: load,
    get data() { return data; },
    get state() { return { ...st }; },
    focusSearch() { search.focus(); },
  };
}

// A dimension picker (grouped, searchable): for secondary dimensions and report switching.
export function pickDimension(anchor, current, onPick, { exclude, allowNone, title = 'Choose a dimension' } = {}) {
  const box = el('div', { class: 'fb' });
  const search = el('input', { class: 'input search', type: 'search', placeholder: 'Find a dimension…', 'aria-label': 'Find a dimension', autofocus: true });
  const list = el('div', { class: 'fb-dims' });
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    list.replaceChildren();
    if (allowNone && current) {
      const none = el('button', { type: 'button', class: 'fb-dim', text: 'None (remove)' });
      none.addEventListener('click', () => { closeAll(); onPick(null); });
      list.appendChild(none);
    }
    for (const g of DIM_GROUPS) {
      const dims = g.dims.filter(([d, label]) => d !== exclude && REPORT_DIMS.includes(d) && (!q || label.toLowerCase().includes(q)));
      if (!dims.length) continue;
      list.appendChild(el('div', { class: 'fb-group', text: g.label }));
      for (const [d, label] of dims) {
        const scopeClash = exclude && ((PAGE_DIMS.has(exclude) && EVENT_DIMS.has(d)) || (EVENT_DIMS.has(exclude) && PAGE_DIMS.has(d)));
        const b = el('button', { type: 'button', class: 'fb-dim' + (d === current ? ' current' : ''), text: label, disabled: scopeClash || null });
        b.addEventListener('click', () => { closeAll(); onPick(d); });
        list.appendChild(b);
      }
    }
  };
  const closeAll = () => closePopover();
  search.addEventListener('input', draw);
  search.addEventListener('keydown', (e) => { if (e.key === 'Enter') { const b = list.querySelector('.fb-dim:not([disabled])'); if (b) b.click(); } });
  box.append(el('div', { class: 'fb-head' }, el('b', { text: title })), search, list);
  draw();
  return popover(anchor, box, { className: 'fb-pop', width: 300 });
}

