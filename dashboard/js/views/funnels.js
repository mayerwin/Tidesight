// Funnels: ordered steps (pages or events, * wildcards) completed within one visit.
import { card, cssVar, el, table } from '../charts.js';
import * as fmt from '../fmt.js';

const storeKey = (site) => 'ts_dash_funnel_' + site;

function loadSteps(site) {
  try {
    const v = JSON.parse(localStorage.getItem(storeKey(site)));
    if (Array.isArray(v) && v.length) return v;
  } catch (e) { /* none */ }
  return [{ type: 'page', value: '/' }, { type: 'page', value: '/pricing*' }, { type: 'event', value: 'signup' }];
}

export async function render(root, ctx) {
  const editor = card({ title: 'Steps', span: 12 });
  const result = card({ title: 'Conversion', span: 12, tableToggle: true });
  root.append(editor.root, result.root);
  let steps = loadSteps(ctx.site);

  const drawEditor = () => {
    editor.body.replaceChildren();
    const list = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px' } });
    steps.forEach((st, i) => {
      const type = el('select', { class: 'input', 'aria-label': 'Step type' },
        el('option', { value: 'page', text: 'Page', selected: st.type === 'page' }), el('option', { value: 'event', text: 'Event', selected: st.type === 'event' }));
      type.addEventListener('change', () => { st.type = type.value; });
      const value = el('input', { class: 'input', value: st.value, 'aria-label': 'Step value', placeholder: st.type === 'page' ? '/path or /blog/*' : 'event name', style: { flex: '1' } });
      value.addEventListener('input', () => { st.value = value.value; });
      const del = el('button', { class: 'btn', type: 'button', text: 'Remove', disabled: steps.length <= 1 });
      del.addEventListener('click', () => { steps.splice(i, 1); drawEditor(); });
      list.appendChild(el('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } }, el('span', { class: 'badge', text: String(i + 1) }), type, value, del));
    });
    const add = el('button', { class: 'btn', type: 'button', text: 'Add step', disabled: steps.length >= 10 });
    add.addEventListener('click', () => { steps.push({ type: 'page', value: '' }); drawEditor(); });
    const run = el('button', { class: 'btn primary', type: 'button', text: 'Show funnel' });
    run.addEventListener('click', () => { localStorage.setItem(storeKey(ctx.site), JSON.stringify(steps)); draw(); });
    editor.body.append(list, el('div', { style: { display: 'flex', gap: '8px', marginTop: '12px' } }, add, run),
      el('p', { class: 'note', text: 'A visit counts for a step when it reaches it after the previous steps, in order. Use * as a wildcard in paths.' }));
  };

  const draw = async () => {
    result.root.classList.add('loading');
    try {
      const res = await ctx.api('funnel', { steps: JSON.stringify(steps.filter((s) => s.value)) });
      result.body.replaceChildren();
      const first = res.steps.length ? res.steps[0].sessions : 0;
      const n = res.steps.length;
      res.steps.forEach((st, i) => {
        // Ordered stages take a one-hue ramp, darkest first.
        const color = cssVar(['--seq-600', '--seq-500', '--seq-400', '--seq-400', '--seq-300'][Math.min(4, Math.floor((i / Math.max(1, n - 1)) * 4))]);
        const w = first ? (st.sessions / first) * 100 : 0;
        const label = (st.step.type === 'event' ? 'Event · ' : '') + st.step.value;
        const track = el('div', { class: 'track' }, el('span', { style: { width: w + '%', background: color } }));
        result.body.appendChild(el('div', { class: 'funnel-step' },
          el('div', { class: 'trunc', title: label, text: (i + 1) + '. ' + label }), track,
          el('div', { class: 'num' }, el('b', { text: fmt.num(st.sessions) }), el('span', { class: 'note', text: ' ' + (i ? fmt.pct(st.from_previous, 1) : '') }))));
      });
      result.body.appendChild(el('p', { class: 'note', text: fmt.num(res.sessions_considered) + ' visits considered. Overall conversion ' +
        (res.steps.length > 1 && res.steps[0].sessions ? fmt.pct(res.steps[res.steps.length - 1].conversion, 1) : '—') + '.' }));
      table(result.alt, [
        { key: 'i', label: 'Step', render: (r) => (r.step.type === 'event' ? 'Event · ' : '') + r.step.value, sortable: false },
        { key: 'sessions', label: 'Visits', num: true, format: fmt.num },
        { key: 'conversion', label: 'From start', num: true, format: (v) => fmt.pct(v, 1) },
        { key: 'from_previous', label: 'From previous', num: true, format: (v) => fmt.pct(v, 1) },
        { key: 'median_ms', label: 'Median time', num: true, format: fmt.duration },
      ], res.steps, { sortKey: null, exportName: ctx.site + '-funnel-' + ctx.from + '_' + ctx.to });
    } catch (err) {
      result.body.replaceChildren(el('div', { class: 'empty', text: err.message }));
    } finally {
      result.root.classList.remove('loading');
    }
  };
  drawEditor();
  await draw();
}
