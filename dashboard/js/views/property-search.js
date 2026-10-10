// A property's search engines (its settings' "Search engines" tab): the Google and Bing accounts, connected once
// for every property, step by step; the Search Console property and the Bing site this property reads; and how
// its imports are going (jobs.py cron fetches the figures: searchengines.py, docs/search-engines.md).
import { send } from '../api.js';
import { card, el, icon } from '../charts.js';
import { hint } from '../dom.js';
import * as fmt from '../fmt.js';
import { hrefFor } from '../state.js';
import { confirm, copyText, toast } from '../ui.js';

const ENGINES = {
  google: {
    title: 'Google Search Console', setting: 'search_console', product: 'Search Console property',
    pitch: 'The searches that bring visitors from Google: queries, clicks, impressions, average position, and the landing pages they lead to. One Google service account serves every property.',
  },
  bing: {
    title: 'Bing Webmaster Tools', setting: 'bing_site', product: 'Bing site',
    pitch: 'The same for Bing’s own results (DuckDuckGo and Yahoo show Bing’s results, but Bing does not report their searches). One API key serves every property.',
  },
};

const link = (href, text) => el('a', { href, target: '_blank', rel: 'noopener', text });
const step = (title, ...body) => el('li', {}, el('b', { text: title }), ...body.map((b) => (typeof b === 'string' ? el('p', { text: b }) : b)));

export async function searchEngines(root, ctx, p, { disabled, save }) {
  const admin = ctx.me && ctx.me.admin;
  let info = null;
  if (admin) {
    try {
      info = await ctx.rawApi('search_engines', { site: p.id });
    } catch (e) {
      if (e.status !== 403) throw e;
    }
  }
  const intro = card({ title: 'Search engines', span: 12 });
  intro.body.append(el('p', { class: 'note', text: 'Only Google and Bing know what people searched for before they clicked: Tidesight reads it from their webmaster tools, every few minutes from the scheduled jobs, and shows it in the Search engines report, next to the visits it recorded.' }),
    el('a', { class: 'btn small', href: hrefFor('seo', { site: p.id }) }, icon('chart'), el('span', { text: 'Open the report' })));
  root.appendChild(intro.root);
  for (const engine of ['google', 'bing']) {
    const c = card({ title: ENGINES[engine].title, span: 6, className: 'engine-card' });
    root.appendChild(c.root);
    drawEngine(c.body, ctx, p, engine, info, { disabled, save, admin });
  }
}

function drawEngine(box, ctx, p, engine, info, opts) {
  box.replaceChildren();
  const meta = ENGINES[engine];
  const account = info && info[engine];
  const target = p[meta.setting] || '';
  box.appendChild(el('p', { class: 'note', text: meta.pitch }));
  if (!info) { // a viewer: what is linked, nothing to change
    box.appendChild(target ? el('div', { class: 'notice' }, icon('check'), el('span', {}, 'Linked to ', el('code', { text: target }), '.'))
      : el('div', { class: 'notice' }, icon('info'), el('span', { text: 'Not linked: an admin can connect it here.' })));
    return;
  }
  if (!account) {
    box.appendChild(connectSteps(engine, (fresh) => {
      Object.assign(info, fresh);
      drawEngine(box, ctx, p, engine, info, opts);
    }, opts.admin));
    return;
  }
  // Connected: the account, the property's site, the import.
  const who = engine === 'google'
    ? el('div', { class: 'copy-field' }, el('code', { class: 'wrap', text: account.email }), copyBtn(account.email))
    : el('div', { class: 'copy-field' }, el('span', { text: 'API key ' }), el('code', { text: account.key_hint }));
  const off = el('button', { type: 'button', class: 'btn small ghost danger', text: 'Disconnect', disabled: !opts.admin || null });
  off.addEventListener('click', async () => {
    const ok = await confirm({ title: 'Disconnect ' + meta.title + '?', danger: true, confirmLabel: 'Disconnect',
      message: 'Every property stops importing from ' + meta.title.split(' ')[0] + '. The figures imported so far stay. You can connect again at any time.' });
    if (!ok) return;
    try {
      Object.assign(info, await send('DELETE', 'search_engines', {}, { engine }));
      toast(meta.title + ' disconnected', { kind: 'success' });
      drawEngine(box, ctx, p, engine, info, opts);
    } catch (e) { toast(e.message, { kind: 'error', timeout: 6000 }); }
  });
  box.appendChild(el('div', { class: 'engine-account' }, el('span', { class: 'status-pill ok' }, el('i'), el('span', { text: 'Connected' })),
    who, el('span', { class: 'spacer' }), off));
  if (account.added_at) box.appendChild(el('p', { class: 'note', text: 'Connected ' + fmt.when(account.added_at) + (account.added_by ? ' by ' + account.added_by : '') + '. It serves every property.' }));
  const list = el('ol', { class: 'setup-steps' });
  if (engine === 'google') {
    list.appendChild(step('Let it read this site in Search Console',
      el('p', {}, 'In ', link('https://search.google.com/search-console', 'Search Console'), ', choose the site, then ', el('b', { text: 'Settings → Users and permissions → Add user' }),
        '. Paste the address above, keep the permission ', el('b', { text: 'Restricted' }), ' (reading is all it does), and click ', el('b', { text: 'Add' }), '.')));
  } else {
    list.appendChild(step('Check the site is in Bing Webmaster Tools',
      el('p', {}, 'It must be added and verified in ', link('https://www.bing.com/webmasters', 'Bing Webmaster Tools'), ' with the account the key belongs to.')));
  }
  const pick = sitePicker(ctx, p, engine, target, opts);
  list.appendChild(step('Choose what ' + (p.label || p.id) + ' reads', pick));
  box.appendChild(list);
  box.appendChild(importStatus(engine, info.imports && info.imports[engine], target));
}

function copyBtn(text) {
  const b = el('button', { type: 'button', class: 'btn small ghost' }, icon('copy'), el('span', { text: 'Copy' }));
  b.addEventListener('click', () => copyText(text));
  return b;
}

// Steps to connect an account, ending with the form that checks it with the engine and keeps it.
function connectSteps(engine, onConnected, admin) {
  const list = el('ol', { class: 'setup-steps' });
  const err = el('div', { class: 'notice warn', hidden: true });
  const go = el('button', { type: 'button', class: 'btn primary', disabled: !admin || null }, el('span', { text: 'Connect' }));
  let input;
  if (engine === 'google') {
    list.append(
      step('Create a Google Cloud project', el('p', {}, 'Open ', link('https://console.cloud.google.com/projectcreate', 'Google Cloud: new project'),
        ', name it (e.g. Tidesight) and click ', el('b', { text: 'Create' }), '. It is free: no billing account is needed.')),
      step('Turn on the Search Console API', el('p', {}, 'Open ', link('https://console.cloud.google.com/apis/library/searchconsole.googleapis.com', 'Google Search Console API'),
        ', check that the new project is selected at the top of the page, and click ', el('b', { text: 'Enable' }), '.')),
      step('Create a service account', el('p', {}, 'Open ', link('https://console.cloud.google.com/iam-admin/serviceaccounts/create', 'Create service account'),
        ', name it tidesight, click ', el('b', { text: 'Create and continue' }), ', skip the optional role and access steps, and click ', el('b', { text: 'Done' }), '.')),
      step('Download its key', el('p', {}, 'Click the new service account, open the ', el('b', { text: 'Keys' }), ' tab, then ', el('b', { text: 'Add key → Create new key → JSON → Create' }),
        '. Your browser downloads a .json file.'),
      el('p', { class: 'note', text: 'If Google answers that key creation is disabled by an organization policy (Google Workspace), create the project with a personal Google account, or ask your Workspace admin to allow keys for it.' })));
    const file = el('input', { type: 'file', accept: '.json,application/json', class: 'input', disabled: !admin || null, 'aria-label': 'The key file' });
    input = el('textarea', { class: 'input mono key-input', rows: 4, spellcheck: 'false', placeholder: '{ "type": "service_account", … }', disabled: !admin || null, 'aria-label': 'The key file’s content' });
    file.addEventListener('change', async () => { if (file.files[0]) input.value = await file.files[0].text(); });
    list.appendChild(step('Give the key file to Tidesight', el('p', { text: 'Choose the file, or paste its content. Tidesight checks it with Google, keeps it on this server only (never shown again), and reads with it: it can change nothing.' }),
      file, input, el('div', { class: 'row-actions-end' }, go)));
  } else {
    list.append(
      step('Add your site to Bing Webmaster Tools', el('p', {}, 'Sign in to ', link('https://www.bing.com/webmasters', 'Bing Webmaster Tools'), ' and add your site. Once Search Console is set up, ',
        el('b', { text: 'Import from Google Search Console' }), ' adds and verifies your sites in one click.')),
      step('Copy your API key', el('p', {}, 'Click the ', el('b', { text: 'settings' }), ' icon (the gear, top right), then ', el('b', { text: 'API access → API key' }),
        ', generate the key if there is none, and copy it.')));
    input = el('input', { class: 'input mono wide', spellcheck: 'false', autocomplete: 'off', placeholder: 'e.g. 0123456789abcdef0123456789abcdef', disabled: !admin || null, 'aria-label': 'Bing API key' });
    list.appendChild(step('Give the key to Tidesight', el('p', { text: 'Tidesight checks it with Bing and keeps it on this server only (never shown again).' }),
      input, el('div', { class: 'row-actions-end' }, go)));
  }
  go.addEventListener('click', async () => {
    err.hidden = true;
    if (!input.value.trim()) { input.focus(); return; }
    go.disabled = true;
    go.firstChild.textContent = 'Checking…';
    try {
      const fresh = await send('PUT', 'search_engines', { engine, key: input.value });
      toast(ENGINES[engine].title + ' connected', { kind: 'success' });
      onConnected(fresh);
    } catch (e) {
      err.replaceChildren(icon('alert'), el('span', { text: e.message }));
      err.hidden = false;
      go.disabled = false;
      go.firstChild.textContent = 'Connect';
    }
  });
  if (!admin) return el('div', { class: 'notice' }, icon('info'), el('span', { text: 'An admin connects search engines.' }));
  return el('div', { class: 'stack' }, list, err);
}

// The engine's sites this account sees, the one this property reads preselected (else the best match).
function sitePicker(ctx, p, engine, target, { disabled, save }) {
  const meta = ENGINES[engine];
  const select = el('select', { class: 'input wide', disabled: true, 'aria-label': meta.product });
  const refresh = el('button', { type: 'button', class: 'btn small ghost' }, icon('undo'), el('span', { text: 'Refresh the list' }));
  const keep = el('button', { type: 'button', class: 'btn small primary', text: 'Save', disabled: true });
  const note = el('p', { class: 'note' });
  const wrap = el('div', { class: 'stack tight' }, el('div', { class: 'picker-row' }, select, keep), el('div', { class: 'picker-row' }, refresh, note));
  if (!p.managed) {
    note.textContent = 'This property is defined in the configuration file: set ' + meta.setting + ' = "…" in its [sites.' + p.id + '] table.';
  }
  const load = async () => {
    select.disabled = true;
    select.replaceChildren(el('option', { text: 'Asking ' + meta.title.split(' ')[0] + '…' }));
    let sites = [];
    try {
      sites = (await ctx.rawApi('search_engine_sites', { engine })).sites || [];
    } catch (e) {
      select.replaceChildren(el('option', { value: target, text: target || '—' }));
      note.textContent = e.message;
      return;
    }
    const best = target || guess(p, sites, engine);
    select.replaceChildren(el('option', { value: '', text: 'None (no import)' }),
      ...sites.map((s) => el('option', { value: s.url, text: s.url + (s.permission === 'not verified' ? ' (not verified)' : ''), selected: s.url === best || null })));
    if (target && !sites.some((s) => s.url === target)) {
      select.appendChild(el('option', { value: target, text: target + ' (not visible to this account any more)', selected: true }));
    }
    select.disabled = disabled || !p.managed;
    keep.disabled = select.disabled || select.value === target;
    if (!p.managed) return;
    note.textContent = sites.length ? (best && best !== target ? 'Suggested from the property’s domains: check it and save.' : '')
      : engine === 'google' ? 'This account sees no Search Console property yet: add it as a user (step above), then refresh.'
        : 'This account has no site yet: add one in Bing Webmaster Tools, then refresh.';
  };
  select.addEventListener('change', () => { keep.disabled = select.value === target; });
  keep.addEventListener('click', () => save({ [meta.setting]: select.value }, select.value ? meta.title.split(' ')[0] + ': the first import starts within a few minutes' : 'Import stopped'));
  refresh.addEventListener('click', load);
  load();
  return wrap;
}

function guess(p, sites, engine) {
  const domains = (p.domains || []).filter((d) => !d.startsWith('*.'));
  const bare = domains.map((d) => d.replace(/^www\./, ''));
  for (const s of sites) {
    const u = s.url.toLowerCase();
    if (engine === 'google' && u.startsWith('sc-domain:') && bare.includes(u.slice(10))) return s.url;
  }
  for (const s of sites) {
    let host = '';
    try { host = new URL(s.url).hostname; } catch (e) { continue; }
    if (domains.includes(host) || bare.includes(host.replace(/^www\./, ''))) return s.url;
  }
  return '';
}

function importStatus(engine, st, target) {
  if (!target) return el('span');
  const box = el('div', { class: 'import-status' });
  if (!st || (!st.last_run && !st.days)) {
    box.appendChild(el('div', { class: 'notice' }, icon('info'), el('span', { text: 'Waiting for the first import: it runs with the scheduled jobs (every few minutes).' })));
    return box;
  }
  if (st.error) {
    const retry = st.retry_at ? ' Next try ' + (st.retry_at > Date.now() ? 'in ' + fmt.duration(st.retry_at - Date.now()) : 'at the next scheduled run') + ' (or as soon as a setting here changes).' : '';
    box.appendChild(el('div', { class: 'notice warn' }, icon('alert'), el('span', { text: st.error + retry })));
  }
  const lines = [];
  if (st.days) lines.push(fmt.plural(st.days, 'day') + ' of figures, ' + fmt.day(st.first_day, true) + ' to ' + fmt.day(st.last_day, true));
  if (engine === 'google' && st.days_checked !== undefined && st.days_checked < st.history_days) {
    lines.push('history: ' + fmt.num(st.days_checked) + ' of ' + fmt.num(st.history_days) + ' days checked so far');
  }
  if (st.last_ok) lines.push('last import ' + fmt.ago(st.last_ok));
  if (lines.length) {
    const p = el('p', { class: 'note', text: lines.join(' · ') + '.' });
    if (engine === 'google') hint(p, 'Google’s figures arrive two to three days late, and its days are Pacific Time days. Recent days are fetched again until Google stops changing them.');
    box.appendChild(p);
  }
  return box;
}
