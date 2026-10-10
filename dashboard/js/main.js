// Tidesight dashboard: sign-in, shell, routing and the report bar. Views live in ./views/<name>.js and export
// render(root, ctx); their state is in the URL hash (state.js).
import { api, ApiError, events, PUBLIC, send } from './api.js';
import { openDatePicker, periodLabel } from './datepicker.js';
import { el, hint, icon, s, tip } from './dom.js';
import { clearReports, openExportDialog } from './export.js';
import { filterChips, loadSegments, openFilterBuilder, segmentButton, trafficButton } from './filters.js';
import * as fmt from './fmt.js';
import { openPalette, openShortcuts, registerShortcuts } from './palette.js';
import { context, go, hrefFor, onReplace, shiftRange, state, storage } from './state.js';
import { closeDialogs, closePopover, menu, popover } from './ui.js';

export const VIEWS = [
  { group: 'Reports' },
  { id: 'overview', title: 'Overview', icon: 'M4 13h4v7H4zM10 8h4v12h-4zM16 4h4v16h-4z' },
  { id: 'realtime', title: 'Realtime', icon: 'M3 12h4l3-7 4 14 3-7h4', bar: 'live' },
  { id: 'sources', title: 'Acquisition', icon: 'M12 3v10M7 8l5 5 5-5M5 21h14', sub: 'Channels, sources, campaigns, referrers' },
  { id: 'pages', title: 'Content', icon: 'M6 3h9l4 4v14H6zM14 3v5h5', sub: 'Pages, entry and exit pages, titles' },
  { id: 'geo', title: 'Locations', icon: 'M12 21s7-6.2 7-12a7 7 0 10-14 0c0 5.8 7 12 7 12zM12 11.5a2.5 2.5 0 100-5 2.5 2.5 0 000 5z', sub: 'Countries, regions, cities, languages' },
  { id: 'devices', title: 'Technology', icon: 'M3 5h14v10H3zM7 19h6M19 9h2v10h-6', sub: 'Devices, browsers, systems, screens' },
  { id: 'events', title: 'Events', icon: 'M13 2L4 14h7l-1 8 9-12h-7z', sub: 'Custom events and their properties' },
  { id: 'goals', title: 'Goals', icon: 'M12 21a9 9 0 100-18 9 9 0 000 18zM12 16a4 4 0 100-8 4 4 0 000 8zM12 12h.01', sub: 'Conversions and conversion rates' },
  { group: 'Explore' },
  { id: 'report', title: 'Explorer', icon: 'M4 5h16v14H4zM9.5 5v14M4 10h16', sub: 'Any dimension, any breakdown' },
  { id: 'funnels', title: 'Funnels', icon: 'M3 5h18l-7 8v6l-4 2v-8z' },
  { id: 'retention', title: 'Retention', icon: 'M4 4h4v4H4zM10 4h4v4h-4zM16 4h4v4h-4zM4 10h4v4H4zM10 10h4v4h-4zM4 16h4v4H4z' },
  { id: 'heatmaps', title: 'Heatmaps', icon: 'M12 3c4 5 6 8 6 11a6 6 0 01-12 0c0-3 2-6 6-11z' },
  { id: 'replays', title: 'Replays', icon: 'M5 4l14 8-14 8z' },
  { id: 'forms', title: 'Forms', icon: 'M4 5h16M4 10h16M4 15h10M4 20h7' },
  { id: 'search', title: 'Site search', icon: 'M10.5 17a6.5 6.5 0 110-13 6.5 6.5 0 010 13zM15.5 15.5L21 21' },
  { group: 'Quality' },
  { id: 'performance', title: 'Performance', icon: 'M12 20a8 8 0 118-8M12 12l5-5' },
  { id: 'errors', title: 'Errors', icon: 'M12 3l10 18H2zM12 10v5M12 18v.5' },
  { group: 'People' },
  { id: 'sessions', title: 'Visits', icon: 'M4 6h16M4 12h16M4 18h16', sub: 'Every visit, newest first' },
  { id: 'visitor', title: 'Visitor', hidden: true, bar: 'none' },
  { id: 'session', title: 'Visit', hidden: true, bar: 'none' },
  { group: 'Admin' },
  { id: 'properties', title: 'Properties', icon: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z', bar: 'none', admin: true },
  { id: 'property', title: 'Property settings', hidden: true, bar: 'none' },
  { id: 'status', title: 'Status', icon: 'M12 3v4M12 17v4M3 12h4M17 12h4M12 15a3 3 0 100-6 3 3 0 000 6z', bar: 'none' },
];
const PUBLIC_VIEWS = new Set(['overview', 'realtime']);
const NO_SITE_VIEWS = new Set(['properties', 'status']);

// ---- rail ------------------------------------------------------------------------------------------
function renderNav(ctx) {
  const nav = document.getElementById('nav');
  nav.replaceChildren();
  for (const v of VIEWS) {
    if (v.group) { if (!PUBLIC) nav.appendChild(el('h3', { text: v.group })); continue; }
    if (v.hidden || (PUBLIC && !PUBLIC_VIEWS.has(v.id))) continue;
    if (PUBLIC && v.id === 'realtime' && !(ctx.siteInfo && (ctx.siteInfo.public_metrics || []).includes('realtime'))) continue;
    if (v.id === 'goals' && !ctx.goals.length && !(state.me && state.me.admin)) continue;
    const a = el('a', { href: hrefFor(v.id), 'aria-current': ctx.view === v.id || (v.id === 'properties' && ctx.view === 'property') ? 'page' : null });
    const ic = s('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' });
    ic.appendChild(s('path', { d: v.icon, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    a.append(ic, el('span', { text: v.title }));
    if (v.id === 'goals' && ctx.goals.length) a.appendChild(el('span', { class: 'nav-count', text: String(ctx.goals.length) }));
    nav.appendChild(a);
  }
}

function renderSitePicker(ctx) {
  const box = document.getElementById('site-picker');
  box.replaceChildren();
  if (PUBLIC) { box.hidden = true; return; }
  box.hidden = false;
  const info = ctx.siteInfo;
  const btn = el('button', { type: 'button', class: 'site-btn', 'aria-haspopup': 'dialog' },
    el('span', { class: 'site-mark', text: info ? (info.label || info.id).replace(/^www\./, '').slice(0, 1).toUpperCase() : '+' }),
    el('span', { class: 'site-text' }, el('b', { text: info ? info.label : 'No property yet' }),
      el('small', { text: info ? (info.label !== (info.domains || [])[0] && (info.domains || [])[0] ? info.domains[0] : info.id) : 'Add one to start' })),
    icon('down', 'caret'));
  btn.addEventListener('click', () => openSiteMenu(btn, ctx));
  box.appendChild(btn);
}

function openSiteMenu(anchor, ctx) {
  const wrap = el('div', { class: 'site-menu' });
  const search = el('input', { class: 'input search', type: 'search', placeholder: 'Find a property…', 'aria-label': 'Find a property' });
  const list = el('div', { class: 'site-list', role: 'listbox' });
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    list.replaceChildren();
    const sites = state.sites.filter((x) => !q || [x.label, x.id, ...(x.domains || [])].some((t) => String(t).toLowerCase().includes(q)));
    for (const x of sites) {
      const b = el('button', { type: 'button', class: 'site-option', role: 'option', 'aria-selected': String(x.id === ctx.site) },
        el('span', { class: 'site-mark small', text: (x.label || x.id).replace(/^www\./, '').slice(0, 1).toUpperCase() }),
        el('span', { class: 'site-text' }, el('b', { text: x.label }), el('small', { text: (x.domains || []).slice(0, 2).join(', ') || x.id })),
        x.id === ctx.site ? icon('check') : null);
      b.addEventListener('click', () => {
        closePopover();
        storage.set('ts_dash_site', x.id);
        const keep = NO_SITE_VIEWS.has(ctx.view) || ['session', 'visitor', 'property'].includes(ctx.view) ? 'overview' : ctx.view;
        go(keep, { site: x.id, filters: null, goal: null });
      });
      list.appendChild(b);
    }
    if (!sites.length) list.appendChild(el('div', { class: 'empty small', text: 'No property matches' }));
  };
  search.addEventListener('input', draw);
  search.addEventListener('keydown', (e) => { if (e.key === 'Enter') { const b = list.querySelector('.site-option'); if (b) b.click(); } });
  const manage = el('a', { class: 'menu-item', href: hrefFor('properties') }, icon('gear'), el('span', { text: 'Manage properties' }));
  manage.addEventListener('click', () => closePopover());
  wrap.append(state.sites.length > 6 ? search : el('span'), list, el('div', { class: 'menu-sep' }), manage);
  if (state.me && state.me.admin) {
    const add = el('a', { class: 'menu-item', href: hrefFor('properties', { add: '1' }) }, icon('plus'), el('span', { text: 'Add a property' }));
    add.addEventListener('click', () => closePopover());
    wrap.appendChild(add);
  }
  draw();
  popover(anchor, wrap, { className: 'site-pop', width: 300 });
}

function renderUser() {
  const box = document.getElementById('rail-user');
  box.replaceChildren();
  if (PUBLIC || !state.me) return;
  const b = el('button', { type: 'button', class: 'user-btn' }, icon('user'), el('span', { text: state.me.user === 'local' ? 'Local mode' : state.me.user }));
  b.addEventListener('click', () => menu(b, [
    { heading: state.me.user === 'local' ? 'Signed in automatically (dev server --no-auth)' : 'Signed in as ' + state.me.user + (state.me.admin ? ' · admin' : '') },
    { label: 'Keyboard shortcuts', icon: 'keyboard', kbd: '?', onClick: openShortcuts },
    { label: 'Switch theme', icon: 'theme', onClick: toggleTheme },
    state.me.can_sign_out ? 'sep' : null,
    state.me.can_sign_out ? { label: 'Sign out', icon: 'logout', onClick: signOut } : null,
  ]));
  box.appendChild(b);
}

async function signOut() {
  try { await send('POST', 'logout'); } catch (e) { /* signed out anyway */ }
  location.reload();
}

// ---- page head: title, freshness ----------------------------------------------------------------------
function renderHead(ctx, meta) {
  const title = document.getElementById('title');
  const sub = document.getElementById('subtitle');
  if (ctx.view === 'overview' && ctx.siteInfo) {
    title.textContent = ctx.siteInfo.label;
    sub.textContent = PUBLIC ? 'Public statistics' : 'Overview' + (ctx.siteInfo.label !== ctx.siteInfo.id ? ' · ' + ctx.siteInfo.id : '');
  } else {
    title.textContent = meta.title;
    sub.textContent = [meta.sub, ctx.siteInfo && !NO_SITE_VIEWS.has(ctx.view) ? ctx.siteInfo.label : null].filter(Boolean).join(' · ');
  }
  document.title = (ctx.view === 'overview' ? '' : meta.title + ' · ') + (ctx.siteInfo ? ctx.siteInfo.label + ' · ' : '') + 'Tidesight';
  const tools = document.getElementById('head-tools');
  tools.replaceChildren();
  if (PUBLIC) return;
  const f = state.freshness;
  if (f) {
    const stale = f.enriched_at && f.last_hit && f.last_hit - f.enriched_at > 30 * 60000;
    const pill = el('span', { class: 'fresh' + (stale ? ' stale' : '') + (!f.enriched_at ? ' never' : ''), tabindex: 0 },
      el('i'), el('span', { text: f.enriched_at ? 'Updated ' + fmt.ago(f.enriched_at) : 'Not processed yet' }));
    hint(pill, (f.enriched_at ? 'Reports include the data processed by the enrichment job ' + fmt.ago(f.enriched_at) + ' (it runs from cron every few minutes). '
      : 'The enrichment job has not run yet: run `jobs.py cron`. ') + (f.last_hit ? 'Last hit received ' + fmt.ago(f.last_hit) + '. ' : '') + 'Realtime shows hits as they arrive.');
    tools.appendChild(pill);
  }
  if (ctx.view === 'overview' && ctx.site) {
    const live = el('a', { class: 'live-now', href: hrefFor('realtime'), hidden: true }, el('i'), el('span'));
    tools.appendChild(live);
    api('live', { site: ctx.site }).then((r) => {
      live.lastChild.textContent = fmt.plural(r.active, 'visitor') + ' now';
      live.hidden = false;
      hint(live, 'Visitors in the last 5 minutes: open Realtime');
    }).catch(() => {});
  }
  const k = el('button', { type: 'button', class: 'ctl ghost', 'aria-label': 'Search and commands' }, icon('search'),
    el('span', { text: 'Search' }), el('kbd', { text: navigator.platform && /Mac/.test(navigator.platform) ? '⌘K' : 'Ctrl K' }));
  k.addEventListener('click', () => openPalette(context()));
  tools.appendChild(k);
}

// ---- report bar --------------------------------------------------------------------------------------------
function renderBar(ctx, meta) {
  const bar = document.getElementById('filters');
  bar.replaceChildren();
  const kind = meta.bar || 'full';
  if (kind === 'none' || !ctx.site) { bar.hidden = true; return; }
  bar.hidden = false;
  if (kind === 'live') {
    bar.appendChild(el('span', { class: 'live-pill' }, el('i'), el('span', { text: 'Live: hits as they arrive, refreshed every 10 seconds' })));
    return;
  }
  const dateWrap = el('div', { class: 'date-ctl' });
  const prev = el('button', { type: 'button', class: 'ctl icon-only', 'aria-label': 'Previous period' }, icon('left'));
  const next = el('button', { type: 'button', class: 'ctl icon-only', 'aria-label': 'Next period', disabled: ctx.to >= ctx.today || null });
  next.appendChild(icon('right'));
  hint(prev, 'Previous period (←)');
  hint(next, 'Next period (→)');
  prev.addEventListener('click', () => shift(ctx, -1));
  next.addEventListener('click', () => shift(ctx, 1));
  const dateBtn = el('button', { class: 'ctl date-btn', type: 'button', 'aria-haspopup': 'dialog' }, icon('calendar'),
    el('span', { class: 'date-text' }, el('b', { text: periodLabel(ctx) }),
      ctx.period !== 'custom' ? el('small', { text: fmt.range(ctx.from, ctx.to) }) : null), icon('down', 'caret'));
  dateBtn.addEventListener('click', () => openDatePicker(dateBtn, ctx, (p) => go(ctx.view, { ...p, offset: null })));
  dateWrap.append(prev, dateBtn, next);
  const left = el('div', { class: 'bar-left' });
  const right = el('div', { class: 'bar-right' });
  bar.append(left, right);
  left.appendChild(dateWrap);
  if (ctx.compare) {
    const x = el('button', { type: 'button', 'aria-label': 'Stop comparing' }, icon('close'));
    x.addEventListener('click', () => go(ctx.view, { compare: null, cfrom: null, cto: null }));
    const cmp = el('span', { class: 'cmp-pill' }, el('i'), el('span', { text: 'vs ' + fmt.range(ctx.compare.from, ctx.compare.to) }), x);
    hint(cmp, { previous: 'Compared with the previous period', year: 'Compared with the same period last year', custom: 'Compared with a custom period' }[ctx.compare.mode]);
    left.appendChild(cmp);
  } else if (!PUBLIC) {
    const c = el('button', { type: 'button', class: 'ctl ghost' }, icon('compare'), el('span', { text: 'Compare' }));
    hint(c, 'Compare with the previous period (C)');
    c.addEventListener('click', () => menu(c, [{ heading: 'Compare with' },
      { label: 'Previous period', hint: fmt.range(...shiftRange(ctx.from, ctx.to, -1)), onClick: () => go(ctx.view, { compare: 'previous' }) },
      { label: 'Same period last year', onClick: () => go(ctx.view, { compare: 'year' }) },
      { label: 'Custom period…', onClick: () => openDatePicker(dateBtn, Object.assign({}, ctx, { compare: { mode: 'custom', from: fmt.addDays(ctx.from, -365), to: fmt.addDays(ctx.to, -365) } }), (p) => go(ctx.view, p)) }]));
    left.appendChild(c);
  }
  if (!PUBLIC) {
    left.appendChild(el('span', { class: 'bar-sep' }));
    const add = el('button', { type: 'button', class: 'ctl', 'aria-haspopup': 'dialog' }, icon('filter'), el('span', { text: 'Filter' }));
    hint(add, 'Filter every report by any dimension (F)');
    add.addEventListener('click', () => openFilterBuilder(add, ctx));
    add.dataset.addFilter = '1';
    left.appendChild(add);
    left.appendChild(filterChips(ctx));
    if (ctx.goal) {
      const x = el('button', { type: 'button', class: 'chip-x', 'aria-label': 'Stop showing conversions' }, icon('close'));
      x.addEventListener('click', () => go(ctx.view, { goal: null }));
      const chip = el('span', { class: 'chip goal' }, el('span', { class: 'chip-main' }, icon('target'),
        el('span', { class: 'chip-dim', text: 'Conversions:' }), el('b', { text: ctx.goalNames[ctx.goal] || ctx.goal })), x);
      hint(chip, 'Reports show the conversions of this goal');
      left.appendChild(chip);
    }
    right.appendChild(segmentButton(ctx));
    right.appendChild(trafficButton(ctx));
    const exp = el('button', { type: 'button', class: 'ctl' }, icon('download'), el('span', { text: 'Export' }));
    hint(exp, 'Download this report or raw data (E)');
    exp.addEventListener('click', () => openExportDialog(ctx));
    right.appendChild(exp);
  }
}

function shift(ctx, dir) {
  const [a, b] = shiftRange(ctx.from, ctx.to, dir);
  if (dir > 0 && a > ctx.today) return;
  go(ctx.view, { period: 'custom', from: a, to: b > ctx.today ? ctx.today : b, offset: null });
}

// ---- theme ---------------------------------------------------------------------------------------------------
function setTheme(next) {
  storage.set('ts_dash_theme', next || null);
  if (next) document.documentElement.setAttribute('data-theme', next); else document.documentElement.removeAttribute('data-theme');
}
export function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  setTheme(current === 'dark' ? 'light' : 'dark');
  render();
}

// ---- render -------------------------------------------------------------------------------------------------------
let renderToken = 0;
export async function render() {
  const token = ++renderToken;
  tip.hide();
  closePopover();
  closeDialogs();
  const ctx = context();
  clearReports();
  if (PUBLIC && !PUBLIC_VIEWS.has(ctx.view)) { go('overview', {}, true); return; }
  const meta = VIEWS.find((v) => v.id === ctx.view) || VIEWS[1];
  if (ctx.site && !PUBLIC) storage.set('ts_dash_site', ctx.site);
  renderNav(ctx);
  renderSitePicker(ctx);
  renderUser();
  renderHead(ctx, meta);
  renderBar(ctx, meta);
  const root = document.getElementById('view');
  if (!ctx.site && !NO_SITE_VIEWS.has(ctx.view) && ctx.view !== 'property') {
    root.replaceChildren(welcome());
    return;
  }
  if (!PUBLIC && ctx.site) loadSegments(ctx).then(() => { if (token === renderToken) renderBar(context(), meta); });
  const previous = root.firstElementChild;
  if (previous) previous.classList.add('loading');
  try {
    const mod = await import('./views/' + (VIEWS.some((v) => v.id === ctx.view) ? ctx.view : 'overview') + '.js');
    if (token !== renderToken) return;
    const fresh = el('div', { class: 'grid' });
    // Most views render off-screen and swap in when ready (the previous render stays visible meanwhile).
    // Views that embed iframes (heatmaps, replays) need to be in the document while they render.
    if (mod.attachFirst) root.replaceChildren(fresh);
    await mod.render(fresh, ctx);
    if (token !== renderToken) return;
    if (!mod.attachFirst) root.replaceChildren(fresh);
  } catch (err) {
    if (token !== renderToken) return;
    if (err instanceof ApiError && err.status === 401) return;
    console.error(err);
    const msg = err instanceof ApiError ? err.message : 'Something went wrong while loading this view.';
    const retry = el('button', { type: 'button', class: 'btn small', text: 'Try again' });
    retry.addEventListener('click', () => render());
    root.replaceChildren(el('div', { class: 'error-box' }, icon('alert'), el('div', {}, el('strong', { text: 'Could not load this view. ' }),
      el('span', { text: msg })), retry));
  }
}

function welcome() {
  const admin = state.me && state.me.admin;
  const box = el('div', { class: 'welcome card span-12' },
    el('div', { class: 'welcome-mark' }, icon('globe')),
    el('h2', { text: 'Add your first property' }),
    el('p', { text: 'A property is a website (or a group of domains) whose visits Tidesight records. Add one, paste the tracking code into its pages, and visits appear here within minutes.' }));
  if (admin) {
    const b = el('a', { class: 'btn primary', href: hrefFor('properties', { add: '1' }) }, icon('plus'), el('span', { text: 'Add a property' }));
    box.appendChild(b);
  } else {
    box.appendChild(el('p', { class: 'note', text: 'Ask an administrator to add a property, or add a [sites.<id>] table to the configuration file.' }));
  }
  const g = el('div', { class: 'grid' });
  g.appendChild(box);
  return g;
}

// ---- sign-in ----------------------------------------------------------------------------------------------------
function showLogin(setup = false, message = '') {
  if (setup) { showSetup(); return; }
  document.body.classList.add('signed-out');
  const root = document.getElementById('login');
  root.hidden = false;
  document.getElementById('shell').hidden = true;
  const user = el('input', { class: 'input', name: 'username', autocomplete: 'username', required: true, autofocus: true, 'aria-label': 'User name' });
  const pass = el('input', { class: 'input', name: 'password', type: 'password', autocomplete: 'current-password', required: true, 'aria-label': 'Password' });
  const show = el('button', { type: 'button', class: 'icon-btn eye', 'aria-label': 'Show the password' }, icon('eye'));
  show.addEventListener('click', () => { pass.type = pass.type === 'password' ? 'text' : 'password'; pass.focus(); });
  const remember = el('input', { type: 'checkbox', id: 'remember', checked: true });
  const err = el('div', { class: 'login-error', role: 'alert', hidden: !message, text: message });
  const submit = el('button', { type: 'submit', class: 'btn primary wide', text: 'Sign in' });
  const form = el('form', { class: 'login-form', novalidate: true },
    el('label', { class: 'field' }, el('span', { class: 'field-label', text: 'User name' }), user),
    el('label', { class: 'field' }, el('span', { class: 'field-label', text: 'Password' }), el('span', { class: 'pw-wrap' }, pass, show)),
    el('label', { class: 'check-row', for: 'remember' }, remember, el('span', { text: 'Keep me signed in on this device' })),
    err, submit);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!user.value || !pass.value) { err.textContent = 'Enter your user name and password.'; err.hidden = false; return; }
    submit.disabled = true;
    submit.textContent = 'Signing in…';
    try {
      await send('POST', 'login', { user: user.value, password: pass.value, remember: remember.checked });
      location.reload(); // the page comes back with the signed-in Content Security Policy
    } catch (ex) {
      err.textContent = ex.status === 401 ? 'Wrong user name or password.' : ex.message;
      err.hidden = false;
      submit.disabled = false;
      submit.textContent = 'Sign in';
      pass.select();
    }
  });
  root.replaceChildren(el('div', { class: 'login-card' }, brand(), el('h1', { text: 'Sign in' }),
    el('p', { class: 'login-sub', text: 'Self-hosted analytics for your sites.' }), form));
  setTimeout(() => user.focus(), 0);
}

// The first account, while Tidesight has no user: the setup code, which Tidesight writes in a file on the server,
// proves the installation is the visitor's own (bots that find a new site first cannot claim it).
// `jobs.py setup-link` prints this page's address with the code after a #setup=, which never reaches the server.
function takeSetupCode() {
  const m = location.hash.match(/^#setup=([0-9A-Za-z-]{4,40})$/);
  if (!m) return '';
  history.replaceState(null, '', location.pathname + location.search); // out of the address bar and the history
  return m[1];
}

function showSetup(code = '') {
  document.body.classList.add('signed-out');
  const root = document.getElementById('login');
  root.hidden = false;
  document.getElementById('shell').hidden = true;
  const field = (label, input, help) => el('label', { class: 'field' }, el('span', { class: 'field-label', text: label }), input, help || null);
  const codeInput = el('input', { class: 'input mono', name: 'setup-code', value: code, required: true, autocomplete: 'one-time-code',
    spellcheck: 'false', autocapitalize: 'characters', placeholder: 'XXXX-XXXX-XXXX-XXXX', 'aria-label': 'Setup code', 'aria-describedby': 'setup-help' });
  const user = el('input', { class: 'input', name: 'username', required: true, autocomplete: 'username', placeholder: 'admin', 'aria-label': 'User name', 'aria-describedby': 'user-help' });
  const pass = el('input', { class: 'input', name: 'new-password', type: 'password', required: true, autocomplete: 'new-password', 'aria-label': 'Password', 'aria-describedby': 'pass-help' });
  const again = el('input', { class: 'input', name: 'new-password-again', type: 'password', required: true, autocomplete: 'new-password', 'aria-label': 'Repeat the password' });
  const show = el('button', { type: 'button', class: 'icon-btn eye', 'aria-label': 'Show the password' }, icon('eye'));
  show.addEventListener('click', () => { const type = pass.type === 'password' ? 'text' : 'password'; pass.type = type; again.type = type; pass.focus(); });
  const remember = el('input', { type: 'checkbox', id: 'remember', checked: true });
  const err = el('div', { class: 'login-error', role: 'alert', hidden: true });
  const submit = el('button', { type: 'submit', class: 'btn primary wide', text: 'Create the account' });
  const fail = (message, input) => { err.textContent = message; err.hidden = false; if (input) input.focus(); };
  const form = el('form', { class: 'login-form', novalidate: true },
    field('Setup code', codeInput, el('span', { class: 'field-help', id: 'setup-help' },
      'On the server: ', el('code', { text: 'cat ~/tidesight-data/setup-code.txt' }),
      ' (the file setup-code.txt of Tidesight\'s data directory). It stops working once the account exists.')),
    field('User name', user, el('span', { class: 'field-help', id: 'user-help', text: 'Letters, digits and . _ @ + -' })),
    field('Password', el('span', { class: 'pw-wrap' }, pass, show), el('span', { class: 'field-help', id: 'pass-help', text: 'At least 8 characters.' })),
    field('Repeat the password', again),
    el('label', { class: 'check-row', for: 'remember' }, remember, el('span', { text: 'Keep me signed in on this device' })),
    err, submit);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.hidden = true;
    if (!codeInput.value.trim()) return fail('Enter the setup code.', codeInput);
    if (!/^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$/.test(user.value)) return fail('Choose a user name: letters, digits and . _ @ + - (starting with a letter or digit).', user);
    if (pass.value.length < 8) return fail('Use a password of at least 8 characters.', pass);
    if (again.value !== pass.value) return fail('The two passwords differ.', again);
    submit.disabled = true;
    submit.textContent = 'Creating the account…';
    try {
      await send('POST', 'setup', { code: codeInput.value, user: user.value, password: pass.value, remember: remember.checked });
      location.reload(); // signed in: the page comes back with the signed-in Content Security Policy
    } catch (ex) {
      if (ex.status === 409) { showLogin(false, 'Tidesight already has an account: sign in.'); return; }
      fail(ex.message, ex.status === 401 ? codeInput : null);
      submit.disabled = false;
      submit.textContent = 'Create the account';
    }
  });
  root.replaceChildren(el('div', { class: 'login-card' }, brand(), el('h1', { text: 'Welcome' }),
    el('p', { class: 'login-sub', text: 'Create the first account of this Tidesight.' }), form));
  setTimeout(() => (code ? user : codeInput).focus(), 0);
}

function brand() {
  const mark = document.querySelector('.brand-mark');
  return el('div', { class: 'login-brand' }, mark ? mark.cloneNode(true) : null, el('span', { class: 'brand-name', text: 'Tidesight' }));
}

// ---- boot ----------------------------------------------------------------------------------------------------------
export async function refreshSites() {
  const res = await api('sites', {});
  state.sites = res.sites || [];
  state.version = res.version || '';
  state.freshness = res.freshness || null;
}

async function boot() {
  const saved = storage.get('ts_dash_theme');
  if (saved) setTheme(saved);
  document.getElementById('theme').addEventListener('click', toggleTheme);
  events.addEventListener('signedout', () => { if (!PUBLIC) showLogin(false, 'Your session ended: sign in again.'); });
  if (!PUBLIC) {
    const setupCode = takeSetupCode();
    try {
      state.me = await api('me');
    } catch (err) {
      if (err.status === 401) { showLogin(); return; }
      if (err.status === 403 && err.body && err.body.setup) { showSetup(setupCode); return; }
      document.getElementById('view').replaceChildren(el('div', { class: 'error-box' }, el('strong', { text: 'The dashboard API is not reachable. ' }), el('span', { text: err.message })));
      return;
    }
    if (state.me && state.me.setup) { showSetup(setupCode); return; }
  }
  try {
    await refreshSites();
    document.getElementById('version').textContent = state.version ? 'v' + state.version : '';
  } catch (err) {
    if (err.status === 401) return;
    document.getElementById('view').replaceChildren(el('div', { class: 'error-box' },
      el('strong', { text: 'The dashboard API is not reachable. ' }), el('span', { text: err.message })));
    return;
  }
  document.getElementById('credit').replaceChildren(
    document.createTextNode('Tidesight — self-hosted analytics. '),
    el('a', { href: 'https://db-ip.com', rel: 'noopener', text: 'IP geolocation by DB-IP' }),
    document.createTextNode(' where DB-IP data is used (CC BY 4.0).'));
  onReplace(render);
  window.addEventListener('hashchange', render);
  if (!PUBLIC) {
    registerShortcuts({ render, views: VIEWS, toggleTheme,
      shift: (dir) => { const ctx = context(); const meta = VIEWS.find((v) => v.id === ctx.view) || {}; if (!meta.bar) shift(ctx, dir); } });
    // Keep "Updated … ago" honest on a long-open page.
    setInterval(() => { if (document.visibilityState === 'visible') refreshSites().then(() => { const c = context(); renderHead(c, VIEWS.find((v) => v.id === c.view) || VIEWS[1]); }).catch(() => {}); }, 5 * 60000);
  }
  render();
}


boot();
