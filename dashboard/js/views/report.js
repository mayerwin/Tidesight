// Explorer: a full report for any dimension, with a second dimension, comparison, plotted rows and exports.
import { card, el, icon } from '../charts.js';
import { DIM_LABEL, REPORT_DIMS } from '../dims.js';
import { pickDimension } from '../table.js';
import { reportPage } from './report-page.js';

const QUICK = ['channel', 'source', 'campaign', 'page', 'entry', 'country', 'city', 'device', 'browser', 'os', 'language', 'event'];

export function render(root, ctx) {
  const dim = REPORT_DIMS.includes(ctx.params.get('dim')) ? ctx.params.get('dim') : 'source';
  const head = card({ title: 'Report on', span: 12, className: 'explorer-head' });
  const btn = el('button', { type: 'button', class: 'ctl dim-btn', 'aria-haspopup': 'dialog' }, icon('layers'), el('b', { text: DIM_LABEL[dim] || dim }), icon('down', 'caret'));
  const pick = (d) => ctx.go('report', { dim: d, dim2: null, sort: null, dir: null, q: null, offset: null, metric: null });
  btn.addEventListener('click', () => pickDimension(btn, dim, pick, { title: 'Report on' }));
  const quick = el('div', { class: 'quick-dims' });
  for (const d of QUICK) {
    const b = el('button', { type: 'button', class: 'pill' + (d === dim ? ' on' : ''), text: DIM_LABEL[d] });
    b.addEventListener('click', () => pick(d));
    quick.appendChild(b);
  }
  head.body.append(el('div', { class: 'explorer-row' }, btn, quick));
  root.appendChild(head.root);
  reportPage(root, ctx, { tabs: [{ dim }], title: DIM_LABEL[dim] || dim });
}
