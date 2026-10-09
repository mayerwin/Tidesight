// Filters, segments and the traffic (bots) setting: the report bar's controls.
import { send } from './api.js';
import { DIM_GROUPS, DIM_LABEL, EXACT_ONLY, labelFor, OP_LABEL, OPS, prefixFor, scopeOf } from './dims.js';
import { el, icon } from './dom.js';
import * as fmt from './fmt.js';
import { sameFilters, state } from './state.js';
import { closePopover, debounce, dialog, field, menu, popover, segmented, tagInput, toast } from './ui.js';

const TEXT_OPS = new Set(['contains', 'not_contains', 'starts_with']);

export function filterText(f, ctx) {
  const values = f.values.map((v) => labelFor(f.dim, v, null, ctx.goalNames));
  return { dim: DIM_LABEL[f.dim] || f.dim, op: OP_LABEL[f.op] || f.op, values };
}

// The chips of the active filters (click one to edit it).
export function filterChips(ctx) {
  const box = el('div', { class: 'chips' });
  ctx.filters.forEach((f, i) => {
    const t = filterText(f, ctx);
    const shown = t.values.length > 2 ? t.values.slice(0, 2).join(', ') + ' +' + (t.values.length - 2) : t.values.join(', ');
    const chip = el('span', { class: 'chip' + (f.op === 'is_not' || f.op === 'not_contains' ? ' negative' : '') });
    const edit = el('button', { type: 'button', class: 'chip-main', title: t.dim + ' ' + t.op + ' ' + t.values.join(', ') },
      el('span', { class: 'chip-dim', text: t.dim }), el('span', { class: 'chip-op', text: ' ' + t.op + ' ' }), el('b', { text: shown }));
    edit.addEventListener('click', () => openFilterBuilder(edit, ctx, { initial: f, index: i }));
    const x = el('button', { type: 'button', class: 'chip-x', 'aria-label': 'Remove filter ' + t.dim }, icon('close'));
    x.addEventListener('click', () => ctx.removeFilter(i));
    chip.append(edit, x);
    box.appendChild(chip);
  });
  if (ctx.filters.length > 1) {
    const clear = el('button', { type: 'button', class: 'link-btn', text: 'Clear all' });
    clear.addEventListener('click', () => ctx.setFilters([]));
    box.appendChild(clear);
  }
  return box;
}

// The filter builder: pick a dimension, an operator, then values (suggested from the data, with counts).
export function openFilterBuilder(anchor, ctx, { initial = null, index = null } = {}) {
  let dim = initial ? initial.dim : null;
  let op = initial ? initial.op : 'is';
  let values = initial ? [...initial.values] : [];
  const body = el('div', { class: 'fb' });
  const others = ctx.filters.filter((_, i) => i !== index);

  const pickDim = () => {
    body.replaceChildren();
    const search = el('input', { class: 'input search', type: 'search', placeholder: 'Filter by…', 'aria-label': 'Find a dimension', autofocus: true });
    const list = el('div', { class: 'fb-dims', role: 'listbox' });
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      list.replaceChildren();
      const buttons = [];
      for (const g of DIM_GROUPS) {
        const dims = g.dims.filter(([d, label]) => (d !== 'goal' || ctx.goals.length) && (!q || label.toLowerCase().includes(q) || d.includes(q)));
        if (!dims.length) continue;
        list.appendChild(el('div', { class: 'fb-group', text: g.label }));
        for (const [d, label] of dims) {
          const b = el('button', { type: 'button', class: 'fb-dim', role: 'option', text: label });
          b.addEventListener('click', () => { dim = d; op = EXACT_ONLY.has(d) ? (op === 'is_not' ? 'is_not' : 'is') : op; values = []; pickValues(); });
          buttons.push(b);
          list.appendChild(b);
        }
      }
      if (!buttons.length) list.appendChild(el('div', { class: 'empty small', text: 'No dimension matches' }));
      search.onkeydown = (e) => { if (e.key === 'Enter' && buttons[0]) { e.preventDefault(); buttons[0].click(); } if (e.key === 'ArrowDown' && buttons[0]) { buttons[0].focus(); e.preventDefault(); } };
    };
    search.addEventListener('input', draw);
    body.append(el('div', { class: 'fb-head' }, el('b', { text: 'Add a filter' })), search, list);
    draw();
    setTimeout(() => search.focus(), 0);
  };

  const pickValues = () => {
    body.replaceChildren();
    const back = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Choose another dimension' }, icon('left'));
    back.addEventListener('click', pickDim);
    const ops = OPS.filter((o) => !EXACT_ONLY.has(dim) || o.id === 'is' || o.id === 'is_not');
    const opSelect = el('select', { class: 'input', 'aria-label': 'Condition' }, ...ops.map((o) => el('option', { value: o.id, text: o.label, selected: o.id === op })));
    const area = el('div', { class: 'fb-values' });
    const apply = el('button', { type: 'button', class: 'btn primary', text: index === null ? 'Add filter' : 'Update filter' });
    const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
    const refreshApply = () => { apply.disabled = !values.length; };
    opSelect.addEventListener('change', () => {
      const wasText = TEXT_OPS.has(op);
      op = opSelect.value;
      if (wasText !== TEXT_OPS.has(op)) values = [];
      drawValues();
    });
    const drawValues = () => {
      area.replaceChildren();
      if (TEXT_OPS.has(op)) {
        const tags = tagInput(values, { placeholder: op === 'starts_with' ? 'e.g. /blog/' : 'Type a text and press Enter',
          onChange: (v) => { values = v; refreshApply(); } });
        area.append(tags, el('p', { class: 'note', text: 'Matches any of these texts, ignoring case. Press Enter after each.' }));
        apply.onclick = () => { values = tags.values(); refreshApply(); if (values.length) done(); };
        setTimeout(() => { const i = tags.querySelector('input'); i && i.focus(); }, 0);
      } else {
        drawExact();
        apply.onclick = done;
      }
      refreshApply();
    };
    const drawExact = () => {
      const picked = el('div', { class: 'fb-picked' });
      const search = el('input', { class: 'input search', type: 'search', placeholder: 'Search values…', 'aria-label': 'Search values' });
      const list = el('div', { class: 'fb-list', role: 'listbox', 'aria-multiselectable': 'true' });
      const drawPicked = () => {
        picked.replaceChildren(...values.map((v) => {
          const x = el('button', { type: 'button', 'aria-label': 'Remove' }, icon('close'));
          x.addEventListener('click', () => { values = values.filter((y) => y !== v); drawPicked(); load(); });
          return el('span', { class: 'tag' }, el('span', { text: labelFor(dim, v, null, ctx.goalNames) }), x);
        }));
        refreshApply();
      };
      const toggleValue = (v) => {
        values = values.includes(v) ? values.filter((y) => y !== v) : [...values, v];
        drawPicked();
      };
      const fixed = dim === 'goal' ? ctx.goals.map((g) => ({ value: g.id, label: g.name }))
        : dim === 'new' ? [{ value: '1', label: 'New visitor' }, { value: '0', label: 'Returning visitor' }]
          : dim === 'hour' ? Array.from({ length: 24 }, (_, h) => ({ value: String(h), label: String(h).padStart(2, '0') + ':00' })) : null;
      let token = 0;
      const load = async () => {
        const q = search.value.trim();
        const mine = ++token;
        list.replaceChildren(el('div', { class: 'empty small', text: 'Loading…' }));
        let rows;
        if (fixed) {
          rows = fixed.filter((r) => !q || r.label.toLowerCase().includes(q.toLowerCase()));
        } else {
          try {
            const res = await ctx.api('breakdown', { dim, limit: 50, q, filters: others.length ? JSON.stringify(others) : null }, { compare: false });
            const metricKey = res.scope === 'page' ? 'pageviews' : res.scope === 'event' ? 'count' : 'visitors';
            rows = res.rows.map((r) => ({ value: r.value, label: labelFor(dim, r.value, r), count: r[metricKey], prefix: prefixFor(dim, r),
              unit: res.scope === 'page' ? 'views' : res.scope === 'event' ? 'events' : 'visitors' }));
          } catch (e) {
            rows = [];
            if (mine === token) list.replaceChildren(el('div', { class: 'empty small', text: e.message }));
            return;
          }
        }
        if (mine !== token) return;
        list.replaceChildren();
        if (q && !fixed && !rows.some((r) => r.value === q)) {
          const custom = el('button', { type: 'button', class: 'fb-option custom' }, el('span', { class: 'fb-check' }, icon('plus')),
            el('span', { class: 'fb-label', text: 'Use “' + q + '”' }));
          custom.addEventListener('click', () => { if (!values.includes(q)) toggleValue(q); load(); });
          list.appendChild(custom);
        }
        for (const r of rows) {
          const on = values.includes(r.value);
          const b = el('button', { type: 'button', class: 'fb-option', role: 'option', 'aria-selected': String(on) },
            el('span', { class: 'fb-check' }, on ? icon('check') : null),
            el('span', { class: 'fb-label' }, r.prefix ? el('span', { class: 'flag', text: r.prefix + ' ' }) : null, r.label),
            r.count !== undefined ? el('span', { class: 'fb-count', text: fmt.compact(r.count) }) : null);
          b.title = r.count !== undefined ? r.label + ' · ' + fmt.num(r.count) + ' ' + r.unit : r.label;
          b.addEventListener('click', () => { toggleValue(r.value); b.setAttribute('aria-selected', String(values.includes(r.value)));
            b.firstChild.replaceChildren(values.includes(r.value) ? icon('check') : ''); });
          list.appendChild(b);
        }
        if (!list.childNodes.length) list.appendChild(el('div', { class: 'empty small', text: 'No values in this period' }));
      };
      search.addEventListener('input', debounce(load, 250));
      search.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const first = list.querySelector('.fb-option');
          if (first) first.click(); else if (values.length) done();
        }
      });
      area.append(picked, search, list);
      drawPicked();
      load();
      setTimeout(() => search.focus(), 0);
    };
    const done = () => {
      if (!values.length) return;
      closePopover();
      const f = { dim, op, values };
      if (index === null) ctx.addFilter(f);
      else ctx.setFilters(ctx.filters.map((x, i) => (i === index ? f : x)));
    };
    cancel.addEventListener('click', () => closePopover());
    body.append(el('div', { class: 'fb-head' }, back, el('b', { text: DIM_LABEL[dim] || dim }), el('span', { class: 'spacer' }), opSelect),
      area, el('div', { class: 'fb-foot' }, el('span', { class: 'note', text: scopeOf(dim) === 'session' ? '' : dim === 'event' ? 'Keeps visits with this event' : 'Keeps visits that viewed such a page' }),
        el('span', { class: 'spacer' }), cancel, apply));
    drawValues();
  };

  if (dim) pickValues(); else pickDim();
  const panel = popover(anchor, body, { className: 'fb-pop', width: 380 });
  // The panel changes height between steps: keep it in view.
  if (panel && window.ResizeObserver) new ResizeObserver(() => panel.isConnected && panel.reposition()).observe(body);
  return panel;
}

// ---- segments ------------------------------------------------------------------------------------------
export async function loadSegments(ctx, force = false) {
  if (state.segments && state.segments.site === ctx.site && !force) return state.segments.list;
  try {
    const res = await ctx.rawApi('segments', { site: ctx.site });
    state.segments = { site: ctx.site, list: res.segments || [] };
  } catch (e) {
    state.segments = { site: ctx.site, list: [] };
  }
  return state.segments.list;
}

export function activeSegment(ctx) {
  const list = state.segments && state.segments.site === ctx.site ? state.segments.list : [];
  return ctx.filters.length ? list.find((s) => sameFilters(s.filters, ctx.filters)) : null;
}

export async function openSegments(anchor, ctx) {
  const list = await loadSegments(ctx);
  const items = [];
  if (list.length) items.push({ heading: 'Saved segments' });
  for (const sgm of list) {
    items.push({ label: sgm.name, hint: describe(sgm.filters, ctx) + (sgm.site_id ? '' : ' · all properties'),
      checked: sameFilters(sgm.filters, ctx.filters), onClick: () => ctx.setFilters(sgm.filters) });
  }
  if (!list.length) items.push({ heading: 'Save a set of filters to apply it again in one click' });
  items.push('sep');
  items.push({ label: 'Save current filters as a segment…', icon: 'plus', disabled: !ctx.filters.length, onClick: () => saveSegmentDialog(ctx) });
  if (list.length) items.push({ label: 'Manage segments…', icon: 'gear', onClick: () => manageSegments(ctx) });
  menu(anchor, items, { width: 340 });
}

function describe(filters, ctx) {
  return filters.map((f) => { const t = filterText(f, ctx); return t.dim + ' ' + t.op + ' ' + t.values.join(', '); }).join(' · ');
}

function saveSegmentDialog(ctx, existing = null) {
  const name = el('input', { class: 'input wide', value: existing ? existing.name : '', placeholder: 'e.g. Mobile visitors from France', maxlength: 100 });
  const scope = segmented([{ id: 'site', label: 'This property' }, { id: 'all', label: 'All properties' }],
    existing && !existing.site_id ? 'all' : 'site', (v) => { which = v; });
  let which = existing && !existing.site_id ? 'all' : 'site';
  const save = el('button', { type: 'button', class: 'btn primary', text: existing ? 'Save' : 'Save segment' });
  const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
  const filters = existing ? existing.filters : ctx.filters;
  const body = el('div', { class: 'stack' }, field('Name', name),
    existing ? null : field('Available in', scope, 'Segments with a goal filter belong to one property.'),
    el('div', { class: 'note', text: describe(filters, ctx) }));
  const d = dialog({ title: existing ? 'Rename segment' : 'Save as segment', body, actions: [cancel, save], width: 460 });
  cancel.addEventListener('click', () => d.close());
  save.addEventListener('click', async () => {
    if (!name.value.trim()) { name.focus(); return; }
    save.disabled = true;
    try {
      if (existing) await send('PUT', 'segment', { name: name.value.trim(), site: existing.site_id || undefined }, { id: existing.id });
      else await send('POST', 'segments', { name: name.value.trim(), filters, site: which === 'site' ? ctx.site : null });
      await loadSegments(ctx, true);
      d.close();
      toast(existing ? 'Segment renamed' : 'Segment saved', { kind: 'success' });
      ctx.go(ctx.view, {}, true);
    } catch (e) {
      save.disabled = false;
      toast(e.message, { kind: 'error' });
    }
  });
}

async function manageSegments(ctx) {
  const list = await loadSegments(ctx, true);
  const body = el('div', { class: 'seg-manage' });
  const draw = (items) => {
    body.replaceChildren();
    if (!items.length) { body.appendChild(el('p', { class: 'note', text: 'No saved segments.' })); return; }
    for (const sgm of items) {
      const rename = el('button', { type: 'button', class: 'btn small ghost', text: 'Rename' });
      const del = el('button', { type: 'button', class: 'btn small ghost danger', text: 'Delete' });
      rename.addEventListener('click', () => { d.close(); saveSegmentDialog(ctx, sgm); });
      del.addEventListener('click', async () => {
        try {
          await send('DELETE', 'segment', {}, { id: sgm.id });
          const fresh = await loadSegments(ctx, true);
          draw(fresh);
          toast('Segment deleted', { kind: 'success' });
        } catch (e) { toast(e.message, { kind: 'error' }); }
      });
      body.appendChild(el('div', { class: 'seg-row' }, el('div', {}, el('b', { text: sgm.name }),
        el('div', { class: 'note', text: describe(sgm.filters, ctx) + (sgm.site_id ? '' : ' · all properties') + (sgm.created_by ? ' · by ' + sgm.created_by : '') })),
      el('span', { class: 'spacer' }), rename, del));
    }
  };
  const d = dialog({ title: 'Segments', body, width: 560, onClose: () => ctx.go(ctx.view, {}, true) });
  draw(list);
}

// ---- traffic: humans, everything, or bots ---------------------------------------------------------------
export const TRAFFIC = [
  { id: 'exclude', label: 'Humans only', short: 'Humans', hint: 'Default. Leaves out visits detected as bots: crawlers, headless browsers, scripted traffic from data centres.' },
  { id: 'include', label: 'Humans and bots', short: 'All traffic', hint: 'Everything that was recorded, including detected bots.' },
  { id: 'only', label: 'Bots only', short: 'Bots', hint: 'Only the visits detected as bots: see what crawls the site.' },
];
export function trafficButton(ctx) {
  const current = TRAFFIC.find((t) => t.id === ctx.bots) || TRAFFIC[0];
  const b = el('button', { type: 'button', class: 'ctl' + (ctx.bots !== 'exclude' ? ' active' : ''), 'aria-haspopup': 'menu' },
    icon(ctx.bots === 'exclude' ? 'user' : 'bot'), el('span', { class: 'ctl-label', text: 'Traffic: ' }), el('b', { text: current.short }), icon('down', 'caret'));
  b.title = current.hint;
  b.addEventListener('click', () => menu(b, [{ heading: 'Which visits the reports count' },
    ...TRAFFIC.map((t) => ({ label: t.label, hint: t.hint, checked: t.id === ctx.bots, onClick: () => ctx.go(ctx.view, { bots: t.id === 'exclude' ? null : t.id }) }))],
  { align: 'end', width: 330 }));
  return b;
}

export function segmentButton(ctx) {
  const active = activeSegment(ctx);
  const b = el('button', { type: 'button', class: 'ctl' + (active ? ' active' : ''), 'aria-haspopup': 'menu' },
    icon('segment'), el('span', { text: active ? active.name : 'Segments' }), icon('down', 'caret'));
  b.addEventListener('click', () => openSegments(b, ctx));
  return b;
}

