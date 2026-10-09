// The command palette (Ctrl/⌘ K): go to any report, property or period, and run common actions by typing.
// Also the keyboard shortcuts and their help.
import { DIM_GROUPS, REPORT_DIMS } from './dims.js';
import { el, icon } from './dom.js';
import { openExportDialog } from './export.js';
import { openFilterBuilder } from './filters.js';
import { context, go, PRESETS, state } from './state.js';
import { closePopover, dialog } from './ui.js';

let main = null; // the shell's functions (set by registerShortcuts)

function commands(ctx) {
  const out = [];
  const views = (main && main.views) || [];
  for (const v of views) {
    if (v.group || v.hidden) continue;
    out.push({ group: 'Go to', label: v.title, hint: v.sub || '', run: () => go(v.id) });
  }
  for (const site of state.sites) {
    out.push({ group: 'Properties', label: site.label, hint: (site.domains || []).join(', ') || site.id,
      run: () => go(['properties', 'property', 'session', 'visitor', 'status'].includes(ctx.view) ? 'overview' : ctx.view, { site: site.id, filters: null, goal: null }) });
  }
  for (const p of PRESETS) {
    out.push({ group: 'Period', label: p.label, kbd: p.key ? p.key.toUpperCase() : null, run: () => go(ctx.view, { period: p.id, from: null, to: null }) });
  }
  out.push({ group: 'Compare', label: 'Compare with the previous period', kbd: 'C', run: () => go(ctx.view, { compare: 'previous' }) });
  out.push({ group: 'Compare', label: 'Compare with the same period last year', run: () => go(ctx.view, { compare: 'year' }) });
  if (ctx.compare) out.push({ group: 'Compare', label: 'Stop comparing', run: () => go(ctx.view, { compare: null, cfrom: null, cto: null }) });
  for (const g of DIM_GROUPS) {
    for (const [d, label] of g.dims) {
      if (!REPORT_DIMS.includes(d)) continue;
      out.push({ group: 'Reports', label: label, hint: g.label + ' report', run: () => go('report', { dim: d }) });
    }
  }
  out.push({ group: 'Actions', label: 'Add a filter', kbd: 'F', run: () => { const b = document.querySelector('[data-add-filter]'); if (b) openFilterBuilder(b, context()); } });
  if (ctx.filters.length) out.push({ group: 'Actions', label: 'Clear all filters', run: () => ctx.setFilters([]) });
  out.push({ group: 'Actions', label: 'Export…', hint: 'This report or raw data', run: () => openExportDialog(context()) });
  out.push({ group: 'Actions', label: 'Traffic: humans only', run: () => go(ctx.view, { bots: null }) });
  out.push({ group: 'Actions', label: 'Traffic: humans and bots', run: () => go(ctx.view, { bots: 'include' }) });
  out.push({ group: 'Actions', label: 'Traffic: bots only', run: () => go(ctx.view, { bots: 'only' }) });
  if (state.me && state.me.admin) out.push({ group: 'Actions', label: 'Add a property', run: () => go('properties', { add: '1' }) });
  if (ctx.site) out.push({ group: 'Actions', label: 'Property settings', hint: ctx.siteInfo ? ctx.siteInfo.label : '', run: () => go('property', { site: ctx.site }) });
  out.push({ group: 'Actions', label: 'Switch theme', run: () => main && main.toggleTheme() });
  out.push({ group: 'Actions', label: 'Keyboard shortcuts', kbd: '?', run: openShortcuts });
  return out;
}

function score(label, q) {
  const t = label.toLowerCase();
  if (!q) return 1;
  if (t.startsWith(q)) return 3;
  if (t.includes(q)) return 2;
  let i = 0; // the letters in order
  for (const c of t) if (c === q[i]) i++;
  return i === q.length ? 1 : 0;
}

export function openPalette(ctx) {
  closePopover();
  const all = commands(ctx);
  const input = el('input', { class: 'palette-input', type: 'search', placeholder: 'Go to a report, a property, a period… or type an action', 'aria-label': 'Command' });
  const list = el('div', { class: 'palette-list', role: 'listbox' });
  let items = [];
  let sel = 0;
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    items = all.map((c) => ({ c, sc: Math.max(score(c.label, q), q ? score(c.group + ' ' + c.label, q) * 0.5 : 0) }))
      .filter((x) => x.sc > 0).sort((a, b) => b.sc - a.sc).slice(0, 60).map((x) => x.c);
    if (!q) items = all.filter((c) => c.group === 'Go to' || c.group === 'Actions').slice(0, 40);
    sel = Math.min(sel, Math.max(0, items.length - 1));
    list.replaceChildren();
    let group = null;
    items.forEach((c, i) => {
      if (c.group !== group) { group = c.group; list.appendChild(el('div', { class: 'palette-group', text: group })); }
      const b = el('button', { type: 'button', class: 'palette-item', role: 'option', 'aria-selected': String(i === sel) },
        el('span', { class: 'pi-label', text: c.label }), c.hint ? el('span', { class: 'pi-hint', text: c.hint }) : null,
        c.kbd ? el('kbd', { text: c.kbd }) : null);
      b.addEventListener('click', () => { d.close(); c.run(); });
      b.addEventListener('pointermove', () => { if (sel !== i) { sel = i; mark(); } });
      list.appendChild(b);
    });
    if (!items.length) list.appendChild(el('div', { class: 'empty small', text: 'Nothing matches' }));
  };
  const mark = () => {
    list.querySelectorAll('.palette-item').forEach((b, i) => {
      b.setAttribute('aria-selected', String(i === sel));
      if (i === sel) b.scrollIntoView({ block: 'nearest' });
    });
  };
  input.addEventListener('input', () => { sel = 0; draw(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { sel = Math.min(items.length - 1, sel + 1); mark(); e.preventDefault(); }
    if (e.key === 'ArrowUp') { sel = Math.max(0, sel - 1); mark(); e.preventDefault(); }
    if (e.key === 'Enter' && items[sel]) { e.preventDefault(); const c = items[sel]; d.close(); c.run(); }
  });
  const d = dialog({ title: 'Search', body: el('div', { class: 'palette' }, el('div', { class: 'palette-search' }, icon('search'), input), list),
    width: 620, className: 'palette-dialog' });
  draw();
  setTimeout(() => input.focus(), 0);
}

export function openShortcuts() {
  const rows = [
    ['Ctrl/⌘ K', 'Search reports, properties, periods and actions'], ['?', 'This list'],
    ['F', 'Add a filter'], ['/', 'Search the table on screen'], ['D', 'Choose the period'],
    ['← →', 'Previous or next period'], ['T Y W M A', 'Today, yesterday, last 7 days, last 30 days, all time'],
    ['C', 'Compare with the previous period (again: stop)'], ['E', 'Export'], ['Esc', 'Close a menu or dialog'],
  ];
  const tbl = el('table', { class: 'data shortcuts' }, el('tbody', {}, ...rows.map(([k, t]) => el('tr', {},
    el('td', {}, ...k.split(' ').map((x) => el('kbd', { text: x }))), el('td', { text: t })))));
  dialog({ title: 'Keyboard shortcuts', body: tbl, width: 520 });
}

export function registerShortcuts(shell) {
  main = shell;
  document.addEventListener('keydown', (e) => {
    const target = e.target;
    const typing = target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); openPalette(context()); return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey || document.body.classList.contains('modal-open')) return;
    if (document.querySelector('.popover')) return;
    const ctx = context();
    const key = e.key;
    const preset = PRESETS.find((p) => p.key && p.key === key.toLowerCase());
    if (key === '?') { e.preventDefault(); openShortcuts(); }
    else if (key === '/') { const sBox = document.querySelector('[data-table-search]'); if (sBox) { e.preventDefault(); sBox.focus(); } }
    else if (key === 'f' || key === 'F') { const b = document.querySelector('[data-add-filter]'); if (b) { e.preventDefault(); openFilterBuilder(b, ctx); } }
    else if (key === 'd' || key === 'D') { const b = document.querySelector('.date-btn'); if (b) { e.preventDefault(); b.click(); } }
    else if (key === 'e' || key === 'E') { if (document.querySelector('.date-btn')) { e.preventDefault(); openExportDialog(ctx); } }
    else if (key === 'c' || key === 'C') { if (document.querySelector('.date-btn')) go(ctx.view, ctx.compare ? { compare: null, cfrom: null, cto: null } : { compare: 'previous' }); }
    else if (key === 'ArrowLeft') { if (document.querySelector('.date-btn')) shell.shift(-1); }
    else if (key === 'ArrowRight') { if (document.querySelector('.date-btn')) shell.shift(1); }
    else if (preset && document.querySelector('.date-btn')) go(ctx.view, { period: preset.id, from: null, to: null });
  });
}

