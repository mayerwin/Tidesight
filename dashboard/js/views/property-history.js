// A property's "Import history" settings: what was imported from another tool (Google Analytics 4, Plausible,
// Matomo), and how to import more, step by step. The scheduled jobs carry the imports out (history.py,
// docs/history.md); reports add the imported days before Tidesight's own first day.
import { send, upload } from '../api.js';
import { card, el, icon } from '../charts.js';
import * as fmt from '../fmt.js';
import { hrefFor } from '../state.js';
import { confirm, copyText, field, toast } from '../ui.js';

const link = (href, text) => el('a', { href, target: '_blank', rel: 'noopener', text });
const step = (title, ...body) => el('li', {}, el('b', { text: title }), ...body.map((b) => (typeof b === 'string' ? el('p', { text: b }) : b)));
const STATUS = { queued: ['Waiting', 'warn'], running: ['Importing', 'warn'], done: ['Imported', 'ok'], failed: ['Failed', 'bad'] };

export async function historyImports(root, ctx, p) {
  const admin = ctx.me && ctx.me.admin;
  if (!admin) {
    const c = card({ title: 'Import history', span: 12 });
    c.body.appendChild(el('div', { class: 'notice' }, icon('info'), el('span', { text: 'An admin imports the history of another analytics tool here.' })));
    root.appendChild(c.root);
    return;
  }
  let info = await ctx.rawApi('history', { site: p.id });
  const list = card({ title: 'Import history', span: 12 });
  root.appendChild(list.root);
  const first = info.first_tidesight_day;
  list.body.appendChild(el('p', { class: 'note', text: 'Moving from another tool? Import its figures and the reports go back as far as it did. ' +
    'Tools keep daily totals, not visits: imported days show visitors, visits, pageviews, bounce rate and visit time, per page, source, channel, country, device, browser and system, ' +
    'but no filters, segments or goals. ' + (first ? 'They fill the days before ' + fmt.day(first, true) + ', when Tidesight’s own data starts: nothing is counted twice.'
      : 'They fill the days before Tidesight’s own data starts.') + ' Importing the same days again replaces them.' }));
  const table = el('div');
  list.body.appendChild(table);
  let timer = null;
  const draw = () => {
    table.replaceChildren();
    if (!info.jobs.length) { table.appendChild(el('div', { class: 'empty small', text: 'Nothing imported yet.' })); return; }
    const tbody = el('tbody');
    for (const j of info.jobs) {
      const [label, cls] = STATUS[j.status] || [j.status, ''];
      const what = j.params.property ? 'property ' + j.params.property : j.params.id_site ? (j.params.name || '') + ' (site ' + j.params.id_site + ')' : j.params.name || '';
      const del = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Delete this import' }, icon('trash'));
      del.addEventListener('click', async () => {
        const ok = await confirm({ title: 'Delete this import?', danger: true, confirmLabel: 'Delete',
          message: 'Its figures leave the reports. You can import them again at any time.' });
        if (!ok) return;
        try { info.jobs = (await send('DELETE', 'history', {}, { site: p.id, id: j.id })).jobs; draw(); toast('Import deleted', { kind: 'success' }); } catch (e) { toast(e.message, { kind: 'error' }); }
      });
      const progress = j.status === 'running' && j.progress && j.progress.done ? ' · ' + j.progress.done.filter((k) => !k.includes(':')).length + ' of its parts done' : '';
      tbody.appendChild(el('tr', {},
        el('td', {}, el('b', { text: j.label }), what ? el('div', { class: 'note', text: what }) : null),
        el('td', {}, el('span', { class: 'status-pill ' + cls }, el('i'), el('span', { text: label })),
          j.error ? el('div', { class: 'note error-text', text: j.error }) : null),
        el('td', { text: j.days_kept ? fmt.plural(j.days_kept, 'day') + ', ' + fmt.range(j.kept_from, j.kept_to) : j.first_day ? fmt.range(j.first_day, j.last_day) + progress : '—' }),
        el('td', { class: 'note', text: fmt.when(j.created_at) + (j.created_by ? ' · ' + j.created_by : '') }),
        el('td', { class: 'num' }, del)));
    }
    table.appendChild(el('table', { class: 'data' }, el('thead', {}, el('tr', {}, el('th', { text: 'From' }), el('th', { text: 'Status' }),
      el('th', { text: 'Days in the reports' }), el('th', { text: 'Asked' }), el('th'))), tbody));
    clearTimeout(timer);
    if (info.jobs.some((j) => j.status === 'queued' || j.status === 'running')) {
      table.appendChild(el('p', { class: 'note', text: 'The scheduled jobs carry imports out, a little every few minutes: this list refreshes by itself.' }));
      timer = setTimeout(async () => {
        if (!document.body.contains(table)) return;
        try { info = await ctx.rawApi('history', { site: p.id }); draw(); } catch (e) { /* next time */ }
      }, 15000);
    }
  };
  draw();
  const started = (res) => { info.jobs = res.jobs; draw(); toast('Import queued: it starts within a few minutes', { kind: 'success' }); list.root.scrollIntoView({ behavior: 'smooth' }); };
  const span = () => {
    const from = el('input', { class: 'input', type: 'date', 'aria-label': 'First day' });
    const to = el('input', { class: 'input', type: 'date', value: info.default_to || '', 'aria-label': 'Last day' });
    const box = el('details', { class: 'advanced' }, el('summary', { text: 'Which days' }),
      el('div', { class: 'row-2' }, field('From', from, 'Empty: the first day with data.'), field('To', to, 'The day before Tidesight’s own data, by default.')));
    return { box, value: () => ({ from: from.value || undefined, to: to.value || undefined }) };
  };

  // ---- Plausible
  const pl = card({ title: 'Plausible', span: 4, className: 'engine-card' });
  root.appendChild(pl.root);
  const file = el('input', { type: 'file', accept: '.zip,application/zip', class: 'input', 'aria-label': 'Plausible export (ZIP)' });
  const plSpan = span();
  const plGo = el('button', { type: 'button', class: 'btn primary' }, el('span', { text: 'Import' }));
  const plErr = el('div', { class: 'notice warn', hidden: true });
  plGo.addEventListener('click', async () => {
    plErr.hidden = true;
    const f = file.files[0];
    if (!f) { file.focus(); return; }
    if (f.size > info.max_upload) {
      const cmd = 'python -m tidesight.jobs --config tidesight.toml history plausible ' + f.name + ' --site ' + p.id;
      const copy = el('button', { type: 'button', class: 'btn small ghost', text: 'Copy' });
      copy.addEventListener('click', () => copyText(cmd));
      plErr.replaceChildren(icon('alert'), el('span', {}, 'This file is over ' + Math.round(info.max_upload / 2 ** 20) + ' MB: copy it to the server and run ', el('code', { text: cmd }), ' from the app/server/python folder.'), copy);
      plErr.hidden = false;
      return;
    }
    plGo.disabled = true;
    plGo.firstChild.textContent = 'Uploading…';
    try {
      const v = plSpan.value();
      started(await upload('history_upload', f, { site: p.id, from: v.from, to: v.to }));
      file.value = '';
    } catch (e) {
      plErr.replaceChildren(icon('alert'), el('span', { text: e.message }));
      plErr.hidden = false;
    } finally {
      plGo.disabled = false;
      plGo.firstChild.textContent = 'Import';
    }
  });
  pl.body.appendChild(el('ol', { class: 'setup-steps' },
    step('Export your data', el('p', {}, 'In Plausible, open the site’s ', el('b', { text: 'Site settings → Imports & Exports' }), ' and click ',
      el('b', { text: 'Export to CSV' }), '. Plausible prepares a ZIP and emails you a link (or offers it on the page): download it.')),
    step('Give the ZIP to Tidesight', el('p', { text: 'Choose the file as it is (no need to unpack it).' }), file, plSpan.box,
      el('div', { class: 'row-actions-end' }, plGo))));
  pl.body.appendChild(plErr);

  // ---- Google Analytics 4
  const ga = card({ title: 'Google Analytics 4', span: 4, className: 'engine-card' });
  root.appendChild(ga.root);
  if (!info.google) {
    ga.body.appendChild(el('ol', { class: 'setup-steps' },
      step('Connect a Google service account', el('p', {}, 'Tidesight reads Google Analytics with the same service account as Search Console. Follow steps 1 to 5 of ',
        el('a', { href: hrefFor('property', { site: p.id, tab: 'search' }), text: 'Search engines' }), ', then come back here.'))));
  } else {
    const email = info.google.email;
    const prop = el('input', { class: 'input mono wide', inputmode: 'numeric', placeholder: 'e.g. 312345678', 'aria-label': 'GA4 property id' });
    const gaSpan = span();
    const gaGo = el('button', { type: 'button', class: 'btn primary' }, el('span', { text: 'Import' }));
    const gaErr = el('div', { class: 'notice warn', hidden: true });
    const copy = el('button', { type: 'button', class: 'btn small ghost' }, icon('copy'), el('span', { text: 'Copy' }));
    copy.addEventListener('click', () => copyText(email));
    gaGo.addEventListener('click', async () => {
      gaErr.hidden = true;
      if (!prop.value.trim()) { prop.focus(); return; }
      gaGo.disabled = true;
      gaGo.firstChild.textContent = 'Checking…';
      try {
        started(await send('POST', 'history', Object.assign({ source: 'ga4', property: prop.value.trim() }, gaSpan.value()), { site: p.id }));
      } catch (e) {
        gaErr.replaceChildren(icon('alert'), el('span', { text: e.message }));
        gaErr.hidden = false;
      } finally {
        gaGo.disabled = false;
        gaGo.firstChild.textContent = 'Import';
      }
    });
    ga.body.appendChild(el('ol', { class: 'setup-steps' },
      step('Turn on the Analytics Data API', el('p', {}, 'Open ', link('https://console.cloud.google.com/apis/library/analyticsdata.googleapis.com', 'Google Analytics Data API'),
        ' in the service account’s Google Cloud project and click ', el('b', { text: 'Enable' }), '.')),
      step('Let the service account read your property', el('p', {}, 'In ', link('https://analytics.google.com/', 'Google Analytics'), ', open ',
        el('b', { text: 'Admin → Property access management → + → Add users' }), '. Paste this address, choose the role ', el('b', { text: 'Viewer' }), ', and click ', el('b', { text: 'Add' }), '.'),
      el('div', { class: 'copy-field' }, el('code', { class: 'wrap', text: email }), copy)),
      step('Enter the property id', el('p', {}, 'In ', el('b', { text: 'Admin → Property details' }), ': the number at the top right (not the G-… measurement id).'),
        prop, gaSpan.box, el('div', { class: 'row-actions-end' }, gaGo))));
    ga.body.appendChild(gaErr);
  }

  // ---- Matomo
  const mt = card({ title: 'Matomo', span: 4, className: 'engine-card' });
  root.appendChild(mt.root);
  const url = el('input', { class: 'input wide', placeholder: 'https://matomo.example.com/', 'aria-label': 'Matomo address' });
  const idSite = el('input', { class: 'input', inputmode: 'numeric', placeholder: '1', 'aria-label': 'Site id' });
  const token = el('input', { class: 'input mono wide', type: 'password', autocomplete: 'off', placeholder: 'Auth token', 'aria-label': 'Matomo auth token' });
  const mtSpan = span();
  const mtGo = el('button', { type: 'button', class: 'btn primary' }, el('span', { text: 'Import' }));
  const mtErr = el('div', { class: 'notice warn', hidden: true });
  mtGo.addEventListener('click', async () => {
    mtErr.hidden = true;
    for (const f of [url, idSite, token]) if (!f.value.trim()) { f.focus(); return; }
    mtGo.disabled = true;
    mtGo.firstChild.textContent = 'Checking…';
    try {
      started(await send('POST', 'history', Object.assign({ source: 'matomo', url: url.value.trim(), id_site: idSite.value.trim(), token: token.value.trim() }, mtSpan.value()), { site: p.id }));
      token.value = '';
    } catch (e) {
      mtErr.replaceChildren(icon('alert'), el('span', { text: e.message }));
      mtErr.hidden = false;
    } finally {
      mtGo.disabled = false;
      mtGo.firstChild.textContent = 'Import';
    }
  });
  mt.body.appendChild(el('ol', { class: 'setup-steps' },
    step('Create an auth token', el('p', {}, 'In Matomo, click the ', el('b', { text: 'gear' }), ' icon, then ', el('b', { text: 'Personal → Security → Auth tokens → Create new token' }),
      '. Your user needs view access to the site. Tidesight uses the token for this import only, then forgets it.')),
    step('Find the site id', el('p', {}, el('b', { text: 'Administration → Websites → Manage' }), ': the number in the ID column.')),
    step('Enter them', field('Matomo’s address', url), field('Site id', idSite), field('Auth token', token), mtSpan.box, el('div', { class: 'row-actions-end' }, mtGo))));
  mt.body.appendChild(mtErr);
}
