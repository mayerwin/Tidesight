// Tidesight dashboard: shell, routing, filters. Views live in ./views/<name>.js and export render(root, ctx).
import { api, exportUrl, PUBLIC, PUBLIC_SITE, ApiError } from './api.js';
import { el, s, tip } from './charts.js';
import * as fmt from './fmt.js';

const VIEWS = [
  { group: 'Traffic' },
  { id: 'overview', title: 'Overview', icon: 'M4 13h4v7H4zM10 8h4v12h-4zM16 4h4v16h-4z' },
  { id: 'realtime', title: 'Realtime', icon: 'M3 12h4l3-7 4 14 3-7h4' },
  { id: 'pages', title: 'Pages', icon: 'M6 3h9l4 4v14H6zM14 3v5h5' },
  { id: 'sources', title: 'Sources', icon: 'M12 3v10M7 8l5 5 5-5M5 21h14' },
  { id: 'geo', title: 'Geography', icon: 'M12 21s7-6.2 7-12a7 7 0 10-14 0c0 5.8 7 12 7 12zM12 11.5a2.5 2.5 0 100-5 2.5 2.5 0 000 5z' },
  { id: 'devices', title: 'Devices', icon: 'M3 5h14v10H3zM7 19h6M19 9h2v10h-6' },
  { group: 'Behaviour' },
  { id: 'events', title: 'Events', icon: 'M13 2L4 14h7l-1 8 9-12h-7z' },
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
  { id: 'sessions', title: 'Sessions', icon: 'M4 6h16M4 12h16M4 18h16' },
  { id: 'visitor', title: 'Visitor', icon: 'M12 12a4 4 0 100-8 4 4 0 000 8zM4 21a8 8 0 0116 0', hidden: true },
  { id: 'session', title: 'Session', icon: '', hidden: true },
  { group: 'System' },
  { id: 'status', title: 'Status', icon: 'M12 3v4M12 17v4M3 12h4M17 12h4M12 15a3 3 0 100-6 3 3 0 000 6z' },
];
const PUBLIC_VIEWS = new Set(['overview', 'realtime']);

const PRESETS = [
  { id: 'today', label: 'Today' }, { id: 'yesterday', label: 'Yesterday' }, { id: '7d', label: 'Last 7 days' },
  { id: '30d', label: 'Last 30 days' }, { id: '90d', label: 'Last 90 days' }, { id: 'mtd', label: 'Month to date' },
  { id: 'lastmonth', label: 'Last month' }, { id: 'ytd', label: 'Year to date' }, { id: '12m', label: 'Last 12 months' },
];

const FILTER_LABELS = {
  source: 'Source', medium: 'Medium', campaign: 'Campaign', channel: 'Channel', referrer: 'Referrer', country: 'Country',
  region: 'Region', city: 'City', device: 'Device', browser: 'Browser', os: 'OS', screen: 'Screen', language: 'Language',
  entry: 'Entry page', exit: 'Exit page', page: 'Page', event: 'Event', visitor: 'Visitor', uid: 'User', hour: 'Hour',
  timezone: 'Time zone', asn: 'Network', brand: 'Brand', model: 'Model', category: 'Source category', new: 'New visitor',
};

const state = { sites: [], version: '' };

function presetRange(id) {
  const today = fmt.isoDay(new Date());
  const d = new Date();
  switch (id) {
    case 'today': return [today, today];
    case 'yesterday': { const y = fmt.addDays(today, -1); return [y, y]; }
    case '7d': return [fmt.addDays(today, -6), today];
    case '90d': return [fmt.addDays(today, -89), today];
    case 'mtd': return [fmt.isoDay(new Date(d.getFullYear(), d.getMonth(), 1)), today];
    case 'lastmonth': return [fmt.isoDay(new Date(d.getFullYear(), d.getMonth() - 1, 1)), fmt.isoDay(new Date(d.getFullYear(), d.getMonth(), 0))];
    case 'ytd': return [fmt.isoDay(new Date(d.getFullYear(), 0, 1)), today];
    case '12m': return [fmt.addDays(today, -364), today];
    default: return [fmt.addDays(today, -29), today];
  }
}

// ---- route state (all in the hash, so views are bookmarkable) ----------------------------------
export function route() {
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?');
  const params = new URLSearchParams(query || '');
  return { view: path || 'overview', params };
}

export function go(view, changes = {}, replace = false) {
  const { params } = route();
  for (const [k, v] of Object.entries(changes)) {
    if (v === null || v === undefined || v === '') params.delete(k); else params.set(k, v);
  }
  const hash = '#/' + view + (params.toString() ? '?' + params.toString() : '');
  if (replace) history.replaceState(null, '', hash); else location.hash = hash;
  if (replace) render();
}

function context() {
  const { view, params } = route();
  const site = PUBLIC ? PUBLIC_SITE : params.get('site') || localStorage.getItem('ts_dash_site') || (state.sites[0] && state.sites[0].id);
  const period = params.get('period') || '30d';
  let [from, to] = presetRange(period);
  if (period === 'custom' && params.get('from') && params.get('to')) { from = params.get('from'); to = params.get('to'); }
  const filters = {};
  for (const [k, v] of params) if (k.startsWith('f.')) filters[k.slice(2)] = v;
  const bots = params.get('bots') === '1';
  const common = { site, from, to, ...(bots ? { bots: '1' } : {}) };
  for (const [k, v] of Object.entries(filters)) common['f.' + k] = v;
  return {
    view, params, site, from, to, period, filters, bots, public: PUBLIC,
    siteInfo: state.sites.find((x) => x.id === site),
    api: (endpoint, extra = {}) => api(endpoint, { ...common, ...extra }),
    rawApi: api,
    setFilter: (dim, value) => go(view === 'overview' || PUBLIC ? view : view, { ['f.' + dim]: value }),
    go,
    fmt,
  };
}

// ---- shell -----------------------------------------------------------------------------------
function renderNav(ctx) {
  const nav = document.getElementById('nav');
  nav.replaceChildren();
  for (const v of VIEWS) {
    if (v.group) { if (!PUBLIC) nav.appendChild(el('h3', { text: v.group })); continue; }
    if (v.hidden || (PUBLIC && !PUBLIC_VIEWS.has(v.id))) continue;
    if (PUBLIC && v.id === 'realtime' && !(ctx.siteInfo && (ctx.siteInfo.public_metrics || []).includes('realtime'))) continue;
    const params = new URLSearchParams(ctx.params);
    for (const k of [...params.keys()]) if (!['site', 'period', 'from', 'to', 'bots'].includes(k) && !k.startsWith('f.')) params.delete(k);
    const a = el('a', { href: '#/' + v.id + (params.toString() ? '?' + params : ''), 'aria-current': ctx.view === v.id ? 'page' : undefined });
    const ic = s('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' });
    ic.appendChild(s('path', { d: v.icon, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    a.append(ic, el('span', { text: v.title }));
    nav.appendChild(a);
  }
}

function renderSitePicker(ctx) {
  const sel = document.getElementById('site');
  sel.replaceChildren(...state.sites.map((x) => el('option', { value: x.id, text: x.name || x.id, selected: x.id === ctx.site })));
  sel.parentElement.hidden = PUBLIC || state.sites.length < 1;
  sel.onchange = () => { localStorage.setItem('ts_dash_site', sel.value); go(ctx.view === 'session' || ctx.view === 'visitor' ? 'overview' : ctx.view, { site: sel.value }); };
}

let openMenu = null;
function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
document.addEventListener('click', (e) => { if (openMenu && !openMenu.contains(e.target) && !e.target.closest('[data-menu]')) closeMenu(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });

function dateMenu(anchor, ctx) {
  closeMenu();
  const r = anchor.getBoundingClientRect();
  const menu = el('div', { class: 'menu', role: 'menu', style: { left: r.left + 'px', top: r.bottom + 6 + window.scrollY + 'px' } });
  for (const p of PRESETS) {
    const b = el('button', { type: 'button', role: 'menuitemradio', 'aria-checked': ctx.period === p.id ? 'true' : 'false' },
      el('span', { class: 'check', text: ctx.period === p.id ? '✓' : '' }), p.label);
    b.addEventListener('click', () => { closeMenu(); go(ctx.view, { period: p.id, from: null, to: null }); });
    menu.appendChild(b);
  }
  const from = el('input', { type: 'date', value: ctx.from, 'aria-label': 'From' });
  const to = el('input', { type: 'date', value: ctx.to, 'aria-label': 'To' });
  const apply = el('button', { type: 'button', class: 'btn', text: 'Apply', style: { width: 'auto' } });
  apply.addEventListener('click', () => { closeMenu(); go(ctx.view, { period: 'custom', from: from.value, to: to.value }); });
  menu.appendChild(el('div', { class: 'foot' }, from, el('span', { text: '→' }), to, apply));
  document.body.appendChild(menu);
  openMenu = menu;
}

function periodLabel(ctx) {
  const p = PRESETS.find((x) => x.id === ctx.period);
  if (p) return p.label;
  return ctx.from === ctx.to ? fmt.day(ctx.from) : fmt.day(ctx.from) + ' – ' + fmt.day(ctx.to);
}

function renderFilters(ctx) {
  const bar = document.getElementById('filters');
  bar.replaceChildren();
  if (['session', 'visitor', 'status'].includes(ctx.view)) { bar.hidden = true; return; }
  bar.hidden = false;
  const cal = s('svg', { viewBox: '0 0 24 24' });
  cal.appendChild(s('path', { d: 'M4 6h16v14H4zM4 10h16M8 3v4M16 3v4', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7 }));
  const dateBtn = el('button', { class: 'ctl', type: 'button', 'data-menu': '1', 'aria-haspopup': 'menu' }, cal, el('span', { text: periodLabel(ctx) }));
  dateBtn.addEventListener('click', (e) => { e.stopPropagation(); openMenu ? closeMenu() : dateMenu(dateBtn, ctx); });
  bar.appendChild(dateBtn);
  if (ctx.view !== 'realtime') bar.appendChild(el('span', { class: 'note', text: fmt.day(ctx.from, true) + (ctx.from !== ctx.to ? ' → ' + fmt.day(ctx.to, true) : '') }));
  for (const [dim, value] of Object.entries(ctx.filters)) {
    const shown = dim === 'country' ? fmt.country(value) : value;
    const x = el('button', { type: 'button', 'aria-label': 'Remove filter', text: '×' });
    x.addEventListener('click', () => go(ctx.view, { ['f.' + dim]: null }));
    bar.appendChild(el('span', { class: 'chip' }, el('span', { text: (FILTER_LABELS[dim] || dim) + ' is ' }), el('b', { text: shown }), x));
  }
  bar.appendChild(el('span', { class: 'spacer' }));
  if (!PUBLIC) {
    const bots = el('button', { class: 'ctl' + (ctx.bots ? ' active' : ''), type: 'button', 'aria-pressed': ctx.bots ? 'true' : 'false',
      title: 'Include traffic flagged as bots', text: ctx.bots ? 'Bots included' : 'Bots excluded' });
    bots.addEventListener('click', () => go(ctx.view, { bots: ctx.bots ? null : '1' }));
    bar.appendChild(bots);
    const exp = el('a', { class: 'ctl', href: exportUrl({ site: ctx.site, what: 'sessions', from: ctx.from, to: ctx.to, format: 'csv' }),
      title: 'Download the sessions of this period as CSV', text: 'Export CSV' });
    bar.appendChild(exp);
  }
}

function setTheme(next) {
  if (next) localStorage.setItem('ts_dash_theme', next); else localStorage.removeItem('ts_dash_theme');
  if (next) document.documentElement.setAttribute('data-theme', next); else document.documentElement.removeAttribute('data-theme');
}

let renderToken = 0;
async function render() {
  const token = ++renderToken;
  tip.hide();
  closeMenu();
  const ctx = context();
  if (PUBLIC && !PUBLIC_VIEWS.has(ctx.view)) { go('overview', {}, true); return; }
  const meta = VIEWS.find((v) => v.id === ctx.view) || VIEWS[1];
  document.getElementById('title').textContent = ctx.view === 'overview' && ctx.siteInfo ? (ctx.siteInfo.name || ctx.site) : meta.title;
  document.getElementById('subtitle').textContent = ctx.view === 'overview' ? 'Overview' + (PUBLIC ? ' · public stats' : '') : (ctx.siteInfo ? ctx.siteInfo.name || ctx.site : '');
  document.title = meta.title + (ctx.siteInfo ? ' · ' + (ctx.siteInfo.name || ctx.site) : '') + ' · Tidesight';
  renderNav(ctx);
  renderSitePicker(ctx);
  renderFilters(ctx);
  const root = document.getElementById('view');
  if (!ctx.site) {
    root.replaceChildren(el('div', { class: 'card span-12' }, el('p', { text: 'No site is configured yet. Add a [sites.<id>] table to the configuration.' })));
    return;
  }
  const previous = root.firstElementChild;
  if (previous) previous.classList.add('loading');
  try {
    const mod = await import('./views/' + ctx.view + '.js');
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
    console.error(err);
    const msg = err instanceof ApiError ? err.message : 'Something went wrong while loading this view.';
    root.replaceChildren(el('div', { class: 'error-box' }, el('strong', { text: 'Could not load this view. ' }), el('span', { text: msg })));
  }
}

async function boot() {
  const saved = localStorage.getItem('ts_dash_theme');
  if (saved) setTheme(saved);
  document.getElementById('theme').addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme') ||
      (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    setTheme(current === 'dark' ? 'light' : 'dark');
    render();
  });
  try {
    const res = await api('sites', PUBLIC ? {} : {});
    state.sites = res.sites || [];
    state.version = res.version || '';
    document.getElementById('version').textContent = state.version ? 'v' + state.version : '';
  } catch (err) {
    document.getElementById('view').replaceChildren(el('div', { class: 'error-box login-hint' },
      el('strong', { text: 'The dashboard API is not reachable. ' }), el('span', { text: err.message })));
    return;
  }
  document.getElementById('credit').replaceChildren(
    document.createTextNode('Tidesight — self-hosted analytics. '),
    el('a', { href: 'https://db-ip.com', rel: 'noopener', text: 'IP geolocation by DB-IP' }),
    document.createTextNode(' where DB-IP data is used (CC BY 4.0).'));
  window.addEventListener('hashchange', render);
  render();
}

boot();
