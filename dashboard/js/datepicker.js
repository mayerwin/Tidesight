// The period picker: one-click presets, a two-month calendar for any range, and a comparison period.
import { el, icon } from './dom.js';
import * as fmt from './fmt.js';
import { COMPARE_MODES, comparisonRange, PRESETS, presetRange } from './state.js';
import { closePopover, popover, toggle } from './ui.js';

const WEEKDAYS = (() => {
  const f = new Intl.DateTimeFormat(undefined, { weekday: 'narrow' });
  return [1, 2, 3, 4, 5, 6, 7].map((d) => f.format(new Date(2024, 0, d))); // 2024-01-01 is a Monday
})();
const monthTitle = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' });

export function periodLabel(ctx) {
  const p = PRESETS.find((x) => x.id === ctx.period);
  return p ? p.label : fmt.range(ctx.from, ctx.to);
}

// apply({period, from, to, compare, cfrom, cto})
export function openDatePicker(anchor, ctx, apply) {
  let from = ctx.from, to = ctx.to, period = ctx.period;
  let pending = null; // the first click of a range
  let hover = null;
  let compareOn = !!ctx.compare;
  let mode = ctx.compare ? ctx.compare.mode : 'previous';
  let cfrom = ctx.compare && mode === 'custom' ? ctx.compare.from : null;
  let cto = ctx.compare && mode === 'custom' ? ctx.compare.to : null;
  let view = fmt.parseDay(to); // the right-hand month
  view = new Date(view.getFullYear(), view.getMonth(), 1);
  const today = ctx.today;

  const presets = el('div', { class: 'dp-presets', role: 'listbox', 'aria-label': 'Preset periods' });
  const calendars = el('div', { class: 'dp-cals' });
  const fromIn = el('input', { type: 'date', class: 'input', 'aria-label': 'First day', max: today });
  const toIn = el('input', { type: 'date', class: 'input', 'aria-label': 'Last day', max: today });
  const summary = el('div', { class: 'dp-summary' });
  const cmpSelect = el('select', { class: 'input', 'aria-label': 'Compare with' },
    ...COMPARE_MODES.map((m) => el('option', { value: m.id, text: m.label })));
  const cfromIn = el('input', { type: 'date', class: 'input', 'aria-label': 'Comparison first day', max: today });
  const ctoIn = el('input', { type: 'date', class: 'input', 'aria-label': 'Comparison last day', max: today });
  const customCmp = el('div', { class: 'dp-cmp-custom' }, cfromIn, el('span', { text: '→' }), ctoIn);
  const cmpRange = el('span', { class: 'dp-cmp-range' });
  const cmpToggle = toggle(compareOn, (v) => { compareOn = v; refresh(); }, { label: 'Compare' });

  const cmpDays = () => (compareOn ? comparisonRange(mode, from, to, cfrom, cto) : null);

  const refresh = () => {
    presets.replaceChildren();
    for (const p of PRESETS) {
      const [a, b] = presetRange(p.id, today, ctx.siteInfo && ctx.siteInfo.first_day);
      const selected = period === p.id && a === from && b === to;
      const btn = el('button', { type: 'button', role: 'option', 'aria-selected': String(selected), class: 'dp-preset' },
        el('span', { text: p.label }), p.key ? el('kbd', { text: p.key.toUpperCase() }) : null);
      btn.addEventListener('click', () => {
        period = p.id; from = a; to = b; pending = null;
        if (!compareOn || mode !== 'custom') { done(); return; }  // one click
        view = new Date(fmt.parseDay(to).getFullYear(), fmt.parseDay(to).getMonth(), 1);
        refresh();
      });
      presets.appendChild(btn);
    }
    fromIn.value = from; toIn.value = to;
    cmpSelect.value = mode;
    cmpSelect.disabled = !compareOn;
    customCmp.hidden = !compareOn || mode !== 'custom';
    const c = cmpDays();
    if (mode === 'custom' && compareOn) { cfromIn.value = cfrom || ''; ctoIn.value = cto || ''; }
    cmpRange.textContent = c ? fmt.range(c[0], c[1]) : '';
    const n = fmt.daysBetween(from, to) + 1;
    summary.replaceChildren(el('b', { text: fmt.range(from, to) }), el('span', { text: ' · ' + fmt.plural(n, 'day') }));
    drawCalendars();
  };

  let dayButtons = [];
  // Selection and comparison shading, updated in place (hovering a second day previews the range).
  const paint = () => {
    const c = cmpDays();
    const lo = pending ? (hover && hover < pending ? hover : pending) : from;
    const hi = pending ? (hover && hover > pending ? hover : pending) : to;
    for (const [b, iso] of dayButtons) {
      const inside = iso >= lo && iso <= hi;
      b.classList.toggle('in', inside);
      b.classList.toggle('start', iso === lo);
      b.classList.toggle('end', iso === hi);
      b.classList.toggle('cmp', !!c && iso >= c[0] && iso <= c[1]);
      b.setAttribute('aria-pressed', String(inside));
    }
  };
  const drawCalendars = () => {
    calendars.replaceChildren();
    dayButtons = [];
    const prev = el('button', { type: 'button', class: 'icon-btn dp-nav', 'aria-label': 'Earlier months' }, icon('left'));
    const next = el('button', { type: 'button', class: 'icon-btn dp-nav', 'aria-label': 'Later months' }, icon('right'));
    prev.addEventListener('click', () => { view = new Date(view.getFullYear(), view.getMonth() - 1, 1); drawCalendars(); });
    next.addEventListener('click', () => { view = new Date(view.getFullYear(), view.getMonth() + 1, 1); drawCalendars(); });
    next.disabled = fmt.isoDay(new Date(view.getFullYear(), view.getMonth() + 1, 1)) > today;
    const months = [new Date(view.getFullYear(), view.getMonth() - 1, 1), view];
    months.forEach((m, k) => {
      const box = el('div', { class: 'dp-month' });
      box.appendChild(el('div', { class: 'dp-month-head' }, k === 0 ? prev : el('span'), el('b', { text: monthTitle.format(m) }), k === 1 ? next : el('span')));
      const grid = el('div', { class: 'dp-grid' });
      WEEKDAYS.forEach((w) => grid.appendChild(el('span', { class: 'dp-wd', text: w, 'aria-hidden': 'true' })));
      const offset = (m.getDay() + 6) % 7;
      for (let i = 0; i < offset; i++) grid.appendChild(el('span'));
      const days = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
      for (let d = 1; d <= days; d++) {
        const iso = fmt.isoDay(new Date(m.getFullYear(), m.getMonth(), d));
        const b = el('button', { type: 'button', class: 'dp-day' + (iso === today ? ' today' : ''), text: String(d),
          disabled: iso > today || null, 'aria-label': fmt.day(iso, true) });
        b.addEventListener('click', () => {
          if (!pending) { pending = iso; hover = iso; paint(); return; }
          from = iso < pending ? iso : pending; to = iso < pending ? pending : iso; pending = null; period = 'custom';
          refresh();
        });
        b.addEventListener('pointerenter', () => { if (pending) { hover = iso; paint(); } });
        dayButtons.push([b, iso]);
        grid.appendChild(b);
      }
      box.appendChild(grid);
      calendars.appendChild(box);
    });
    paint();
  };

  const setDate = (which, value) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return;
    if (which === 'from') from = value; else to = value;
    if (from > to) [from, to] = [to, from];
    period = 'custom';
    pending = null;
    view = new Date(fmt.parseDay(to).getFullYear(), fmt.parseDay(to).getMonth(), 1);
    refresh();
  };
  fromIn.addEventListener('change', () => setDate('from', fromIn.value));
  toIn.addEventListener('change', () => setDate('to', toIn.value));
  cmpSelect.addEventListener('change', () => {
    mode = cmpSelect.value;
    if (mode === 'custom' && !cfrom) { const p = comparisonRange('previous', from, to); cfrom = p[0]; cto = p[1]; }
    refresh();
  });
  cfromIn.addEventListener('change', () => { cfrom = cfromIn.value; refresh(); });
  ctoIn.addEventListener('change', () => { cto = ctoIn.value; refresh(); });

  const done = () => {
    closePopover();
    const isPreset = PRESETS.some((p) => p.id === period) && (() => { const [a, b] = presetRange(period, today, ctx.siteInfo && ctx.siteInfo.first_day); return a === from && b === to; })();
    apply({
      period: isPreset ? period : 'custom', from: isPreset ? null : from, to: isPreset ? null : to,
      compare: compareOn ? mode : null,
      cfrom: compareOn && mode === 'custom' ? cfrom : null, cto: compareOn && mode === 'custom' ? cto : null,
    });
  };
  const applyBtn = el('button', { type: 'button', class: 'btn primary', text: 'Apply' });
  const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
  applyBtn.addEventListener('click', done);
  cancel.addEventListener('click', () => closePopover());

  const body = el('div', { class: 'datepicker' },
    el('div', { class: 'dp-main' }, presets,
      el('div', { class: 'dp-right' }, calendars,
        el('div', { class: 'dp-inputs' }, fromIn, el('span', { text: '→' }), toIn, summary))),
    el('div', { class: 'dp-foot' },
      el('div', { class: 'dp-compare' }, cmpToggle, cmpSelect, customCmp, cmpRange),
      el('span', { class: 'spacer' }), cancel, applyBtn));
  refresh();
  return popover(anchor, body, { className: 'dp-pop' });
}
