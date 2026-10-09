// The Export dialog: says exactly what each download holds. "Reports" are the aggregated tables on screen
// (with the filters, comparison and sort); "Raw data" are the stored rows themselves, unfiltered.
import { exportUrl, fileUrl } from './api.js';
import { el, icon } from './dom.js';
import * as fmt from './fmt.js';
import { filterText } from './filters.js';
import { dialog, downloadUrl, segmented, spinner } from './ui.js';

let reports = [];
// Views announce the reports they show: {label, endpoint: 'breakdown'|'overview'|'goals', params, rows?}
export function registerReport(report) {
  reports = reports.filter((r) => r.label !== report.label);
  reports.push(report);
}
export function clearReports() { reports = []; }

const FORMATS = [
  { id: 'xlsx', label: 'Excel', hint: '.xlsx, with a sheet describing the export' },
  { id: 'csv', label: 'CSV', hint: 'Any spreadsheet or tool' },
  { id: 'tsv', label: 'TSV', hint: 'Tab-separated' },
  { id: 'json', label: 'JSON', hint: 'For scripts' },
];

export function openExportDialog(ctx) {
  let reportFormat = 'xlsx';
  let rawFormat = 'csv';
  const filters = ctx.filters.map((f) => { const t = filterText(f, ctx); return t.dim + ' ' + t.op + ' ' + t.values.join(', '); });
  const scope = el('div', { class: 'export-scope' },
    el('div', {}, el('span', { class: 'note', text: 'Property' }), el('b', { text: ctx.siteInfo ? ctx.siteInfo.label : ctx.site })),
    el('div', {}, el('span', { class: 'note', text: 'Period' }), el('b', { text: fmt.range(ctx.from, ctx.to) })),
    ctx.compare ? el('div', {}, el('span', { class: 'note', text: 'Compared with' }), el('b', { text: fmt.range(ctx.compare.from, ctx.compare.to) })) : null,
    el('div', {}, el('span', { class: 'note', text: 'Filters' }), el('b', { text: filters.length ? filters.join(' · ') : 'none' })),
    el('div', {}, el('span', { class: 'note', text: 'Traffic' }), el('b', { text: ctx.bots === 'only' ? 'bots only' : ctx.bots === 'include' ? 'humans and bots' : 'humans only' })));

  // Reports on screen.
  const reportList = el('div', { class: 'export-list' });
  const drawReports = () => {
    reportList.replaceChildren();
    if (!reports.length) {
      reportList.appendChild(el('p', { class: 'note', text: 'This page has no table to export. Open a report (Acquisition, Content, Explorer…) or use the raw data below.' }));
      return;
    }
    for (const r of reports) {
      const b = el('button', { type: 'button', class: 'btn small' }, icon('download'), el('span', { text: 'Download' }));
      b.addEventListener('click', () => downloadUrl(fileUrl(r.endpoint, Object.assign({}, r.params, { format: reportFormat, limit: 100000 }))));
      reportList.appendChild(el('div', { class: 'export-row' },
        el('div', { class: 'export-what' }, el('b', { text: r.label }), el('span', { class: 'note', text: r.description || 'Every row, with the columns of the report' + (ctx.compare ? ' and the compared period' : '') })),
        b));
    }
  };
  const reportFormats = segmented(FORMATS.map((f) => ({ id: f.id, label: f.label, title: f.hint })), reportFormat, (v) => { reportFormat = v; }, { label: 'Report format', small: true });

  // Raw datasets, with their row counts for this period.
  const rawList = el('div', { class: 'export-list' }, el('div', { class: 'empty small' }, spinner(), ' Counting rows…'));
  const rawFormats = segmented([{ id: 'csv', label: 'CSV' }, { id: 'json', label: 'JSON' }, { id: 'jsonl', label: 'JSON Lines' }], rawFormat,
    (v) => { rawFormat = v; }, { label: 'Raw data format', small: true });
  ctx.rawApi('export_catalog', { site: ctx.site, from: ctx.from, to: ctx.to }).then((cat) => {
    rawList.replaceChildren();
    for (const d of cat.datasets) {
      const b = el('button', { type: 'button', class: 'btn small', disabled: !d.rows || null }, icon('download'), el('span', { text: 'Download' }));
      b.addEventListener('click', () => downloadUrl(exportUrl({ site: ctx.site, what: d.id, from: ctx.from, to: ctx.to, format: rawFormat })));
      const cols = el('details', { class: 'export-cols' }, el('summary', { text: d.columns.length + ' columns' }), el('code', { text: d.columns.join(', ') }));
      rawList.appendChild(el('div', { class: 'export-row' },
        el('div', { class: 'export-what' }, el('b', {}, d.label, el('span', { class: 'count', text: d.rows === null ? '' : fmt.num(d.rows) + ' rows' })),
          el('span', { class: 'note', text: d.description + (d.utc_days ? '' : '') }), d.columns.length ? cols : null), b));
    }
  }).catch((e) => rawList.replaceChildren(el('p', { class: 'note', text: e.message })));

  drawReports();
  const body = el('div', { class: 'export' }, scope,
    el('section', {}, el('div', { class: 'export-head' }, el('h3', { text: 'Reports on this page' }), reportFormats),
      el('p', { class: 'note', text: 'The aggregated rows of the tables you see: all of them, not only the page on screen, with your filters, traffic setting and comparison. Durations in seconds, rates as fractions.' }),
      reportList),
    el('section', {}, el('div', { class: 'export-head' }, el('h3', { text: 'Raw data' }), rawFormats),
      el('p', { class: 'note', text: 'One row per stored record of this property and period: everything, unfiltered (bots and internal visits are included and flagged in columns). Days are the property’s days; raw hits and visitors use UTC days.' }),
      rawList));
  dialog({ title: 'Export', subtitle: 'Choose what to download', body, width: 760, className: 'export-dialog' });
}
