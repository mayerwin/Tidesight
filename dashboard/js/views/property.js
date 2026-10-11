// A property's settings: general, tracking code (with what arrives and what is refused), goals, search engines, data collection,
// privacy, public sharing, and deletion. Properties defined in the configuration file are shown read-only.
import { collectorBase, send } from '../api.js';
import { card, columnChart, el, icon } from '../charts.js';
import { hint } from '../dom.js';
import * as fmt from '../fmt.js';
import { refreshSites } from '../main.js';
import { go, hrefFor } from '../state.js';
import { confirm, copyText, field, segmented, tagInput, toast, toggle } from '../ui.js';
import { historyImports } from './property-history.js';
import { searchEngines } from './property-search.js';
import { cleanDomain, statusOf, validDomain, zoneSelect } from './properties.js';

const TABS = [
  { key: 'general', label: 'General' }, { key: 'tracking', label: 'Tracking code' }, { key: 'goals', label: 'Goals' },
  { key: 'search', label: 'Search engines' }, { key: 'history', label: 'Import history' }, { key: 'collection', label: 'Data collection' }, { key: 'privacy', label: 'Privacy' }, { key: 'sharing', label: 'Sharing' },
  { key: 'danger', label: 'Delete' },
];

// What each switch records (docs/datapoints.md has the details).
const FEATURES = [
  { group: 'Visits', items: [
    ['pageviews', 'Pageviews', 'Every page viewed, with its title and referrer.'],
    ['spa', 'Single-page apps', 'Route changes (History API and #hash routes) count as pageviews.'],
    ['engagement', 'Engagement time', 'Time the page was visible and in use; bounces follow from it.'],
    ['context', 'Browser context', 'Screen, viewport, language, time zone, device hints.'],
    ['site_search', 'Site search', 'Search queries found in the URL or sent with tidesight("search").'],
    ['not_found', 'Error pages', 'Pages flagged with <meta name="ts:status">.'],
    ['visibility', 'Tab visibility', 'When the page is hidden and shown again.'],
    ['resize', 'Resizes', 'Viewport size changes.']] },
  { group: 'Interactions', items: [
    ['clicks', 'Clicks', 'Clicks on links, buttons and other elements (text up to 100 characters).'],
    ['outbound', 'Outbound links', 'Clicks that leave the site.'], ['downloads', 'Downloads', 'Clicks on files (PDF, ZIP…).'],
    ['heatmap_clicks', 'Click heatmaps', 'Click positions on the page for heatmaps.'],
    ['rage_clicks', 'Rage clicks', 'Repeated clicks in one spot: frustration.'],
    ['dead_clicks', 'Dead clicks', 'Clicks that change nothing on the page.'],
    ['error_clicks', 'Error clicks', 'Clicks followed by a JavaScript error.'],
    ['scroll', 'Scroll depth', 'How far down visitors scroll (25, 50, 75, 90, 100%).'],
    ['forms', 'Form analytics', 'Starts, field timings, submits and abandons. Never the values typed.'],
    ['copy_paste', 'Copy and paste', 'Copy, cut and paste events (no clipboard content).'],
    ['text_selection', 'Text selection', 'Which text is selected (no content beyond a short excerpt).']] },
  { group: 'Performance and errors', items: [
    ['vitals', 'Web Vitals', 'LCP, INP, CLS, FCP, TTFB, as Google measures them.'],
    ['navigation_timing', 'Navigation timing', 'DNS, connection, server, download and load phases.'],
    ['long_tasks', 'Long tasks', 'Main-thread tasks over 50 ms.'],
    ['errors', 'JavaScript errors', 'Errors and unhandled rejections with their stack.'],
    ['resource_errors', 'Failed resources', 'Images, scripts and styles that fail to load.']] },
  { group: 'Recordings', items: [
    ['replay', 'Session replay', 'Records sampled visits to replay them. Inputs are masked; passwords never recorded.', 'replay'],
    ['mouse_moves', 'Mouse movement', 'Pointer trails for movement heatmaps (sampled).', 'mouse_moves'],
    ['dom_snapshots', 'Page snapshots', 'A DOM snapshot per page, so heatmaps show the page as visitors saw it.', 'dom_snapshots']] },
  { group: 'Identification', items: [
    ['fingerprint', 'Device fingerprint', 'A hash of stable browser traits: links visits when cookies are cleared, flags bots.'],
    ['canvas_fingerprint', 'Canvas', 'Part of the fingerprint.'], ['webgl_fingerprint', 'WebGL', 'Part of the fingerprint.'],
    ['audio_fingerprint', 'Audio', 'Part of the fingerprint.'], ['font_fingerprint', 'Fonts', 'Part of the fingerprint (slower).'],
    ['bot_signals', 'Bot signals', 'Signs of automation (webdriver, headless browsers).'],
    ['adblock', 'Ad blocker detection', 'Whether an ad blocker is active.']] },
];
const PUBLIC_METRICS = [['visitors', 'Visitors'], ['pageviews', 'Pageviews'], ['pages', 'Top pages'], ['sources', 'Sources'],
  ['countries', 'Countries'], ['devices', 'Devices'], ['events', 'Events'], ['realtime', 'Realtime']];

export async function render(root, ctx) {
  const site = ctx.params.get('site') || ctx.site;
  let p;
  try {
    p = await ctx.rawApi('property', { site });
  } catch (e) {
    if (e.status === 404) { go('properties'); return; }
    throw e;
  }
  const tab = TABS.some((t) => t.key === ctx.params.get('tab')) ? ctx.params.get('tab') : 'general';
  const head = card({ title: '', span: 12, className: 'prop-settings-head' });
  head.head.remove();
  const st = statusOf(p);
  head.body.appendChild(el('div', { class: 'prop-top' },
    el('span', { class: 'site-mark big', text: (p.label || p.id).replace(/^www\./, '').slice(0, 1).toUpperCase() }),
    el('div', { class: 'prop-title' }, el('h2', { text: p.label }), el('span', { class: 'note' }, el('code', { text: p.id }), ' · ' + (p.domains.join(', ') || 'any domain'))),
    el('span', { class: 'spacer' }), el('span', { class: 'status-pill ' + st.cls }, el('i'), el('span', { text: st.text })),
    el('a', { class: 'btn small', href: hrefFor('overview', { site: p.id, filters: null, goal: null }) }, icon('chart'), el('span', { text: 'Reports' }))));
  if (!p.managed) {
    head.body.appendChild(el('div', { class: 'notice' }, icon('info'), el('span', { text: 'This property is defined in the configuration file (tidesight.toml), so it is shown read-only here: change it in the file, and the collector picks the change up within seconds.' })));
  } else if (!p.can_edit) {
    head.body.appendChild(el('div', { class: 'notice' }, icon('info'), el('span', { text: 'Only dashboard admins can change properties.' })));
  }
  const tabs = el('div', { class: 'tabs big', role: 'tablist' });
  for (const t of TABS) {
    if (t.key === 'danger' && !p.managed) continue;
    const a = el('a', { role: 'tab', 'aria-selected': String(t.key === tab), href: hrefFor('property', { site: p.id, tab: t.key }), text: t.label });
    tabs.appendChild(a);
  }
  head.body.appendChild(tabs);
  root.appendChild(head.root);
  const disabled = !p.can_edit;
  const save = async (changes, message = 'Saved') => {
    try {
      const fresh = await send('PUT', 'property', changes, { site: p.id });
      toast(message, { kind: 'success' });
      await refreshSites();
      ctx.go('property', { site: p.id, tab }, true);
      return fresh;
    } catch (e) {
      toast(e.message, { kind: 'error', timeout: 6000 });
      return null;
    }
  };
  const view = { general, tracking, goals, search: searchEngines, history: historyImports, collection, privacy, sharing, danger }[tab];
  await view(root, ctx, p, { disabled, save });
}

function saveBar(onSave, disabled) {
  const btn = el('button', { type: 'button', class: 'btn primary', text: 'Save changes', disabled: true });
  const reset = el('button', { type: 'button', class: 'btn ghost', text: 'Discard', hidden: true });
  const bar = el('div', { class: 'save-bar', hidden: disabled }, el('span', { class: 'note dirty-note', text: '' }), el('span', { class: 'spacer' }), reset, btn);
  btn.addEventListener('click', async () => { btn.disabled = true; btn.textContent = 'Saving…'; await onSave(); btn.textContent = 'Save changes'; });
  reset.addEventListener('click', () => location.reload());
  return { bar, dirty(on = true) { btn.disabled = !on; reset.hidden = !on; bar.querySelector('.dirty-note').textContent = on ? 'Unsaved changes' : ''; } };
}

// ---- general ---------------------------------------------------------------------------------------------------
function general(root, ctx, p, { disabled, save }) {
  const c = card({ title: 'General', span: 12 });
  const changes = {};
  const sb = saveBar(() => save(changes), disabled);
  const name = el('input', { class: 'input wide', value: p.name, placeholder: p.domains[0] || p.id, maxlength: 100, disabled: disabled || null });
  name.addEventListener('input', () => { changes.name = name.value.trim(); sb.dirty(); });
  const domains = tagInput(p.origins.map((o) => o.replace(/^https:\/\//, '')), {
    placeholder: 'Add a domain and press Enter', disabled,
    normalize: (v) => { const http = /^http:\/\//i.test(v); const d = cleanDomain(v); return http ? 'http://' + d : d; },
    validate: (v) => (validDomain(v) || v === '*' ? null : '“' + v + '” is not a domain (example.com, *.example.com, localhost:3000)'),
    onChange: (v) => { changes.origins = v; sb.dirty(v.length > 0); },
  });
  const zone = zoneSelect(p.timezone);
  zone.disabled = disabled;
  zone.addEventListener('change', () => { changes.timezone = zone.value; sb.dirty(); });
  const quota = el('input', { class: 'input', type: 'number', min: 0, step: 1000, value: p.max_hits_per_day, disabled: disabled || null });
  quota.addEventListener('input', () => { changes.max_hits_per_day = Math.max(0, parseInt(quota.value || '0', 10)); sb.dirty(); });
  const timeout = el('input', { class: 'input', type: 'number', min: 1, max: 1440, value: p.session_timeout_min, disabled: disabled || null });
  timeout.addEventListener('input', () => { changes.session_timeout_min = Math.min(1440, Math.max(1, parseInt(timeout.value || '30', 10))); sb.dirty(); });
  const idBox = el('div', { class: 'copy-field' }, el('code', { text: p.id }), copyButton(p.id));
  c.body.append(el('div', { class: 'form' },
    field('Name', name, 'Shown in the property list and reports. Leave empty to show the domain.', { wide: true }),
    field('Domains', domains, 'Pages on these domains may send data; anything else is stored flagged and kept out of reports. *.example.com covers every subdomain; http:// for sites without TLS.', { wide: true }),
    el('div', { class: 'row-2' }, field('Time zone', zone, 'Days start at midnight in this zone. A change applies to data processed from now on.'),
      field('Property id', idBox, 'The tracking code’s data-site. It cannot change.')),
    el('div', { class: 'row-2' }, field('Daily hit limit', quota, '0 = no limit. Beyond it, the day’s further hits are refused (a flood cannot fill the disk).'),
      field('Visit timeout (minutes)', timeout, 'A new visit starts after this long without activity.'))), sb.bar);
  root.appendChild(c.root);
}

function copyButton(text, label = 'Copy') {
  const b = el('button', { type: 'button', class: 'btn small ghost' }, icon('copy'), el('span', { text: label }));
  b.addEventListener('click', () => copyText(text));
  return b;
}

// ---- tracking code -------------------------------------------------------------------------------------------------
export function snippetBlock(p, publicUrl) {
  const base = collectorBase(publicUrl);
  const tag = '<script async src="' + base + 'ts.js" data-site="' + p.id + '"></script>';
  const all = '<script async src="' + base + 'ts.all.js" data-site="' + p.id + '"></script>';
  const pixel = '<noscript><img src="' + base + 'p.gif?site=' + encodeURIComponent(p.id) + '" alt="" width="1" height="1"></noscript>';
  const block = (title, code, help) => el('div', { class: 'snippet' }, el('div', { class: 'snippet-head' }, el('b', { text: title }), el('span', { class: 'spacer' }), copyButton(code)),
    el('pre', { class: 'code', text: code }), help ? el('p', { class: 'note', text: help }) : null);
  return el('div', { class: 'snippets' },
    block('Tracking code', tag, 'Loads the core (about 8 KB) and only the plugins this property switches on.'),
    el('details', { class: 'advanced' }, el('summary', { text: 'Other ways to install' }),
      block('One file with every plugin', all, 'One request instead of several; larger.'),
      block('Visitors without JavaScript', pixel, 'Counts pageviews when scripts are off (no engagement, no events).'),
      el('p', { class: 'note', text: 'With a Content Security Policy, allow ' + base.replace(/\/$/, '') + ' in script-src and connect-src (img-src for the pixel). Custom events: tidesight("track", "signup", {plan: "pro"}). See docs/tracker.md.' })));
}

const FLAG_TEXT = {
  origin_mismatch: 'from a domain the property does not list', no_origin: 'without an Origin (not from a browser page)',
  unknown_site: 'for an unknown property id', invalid: 'malformed', too_large: 'too large', missing_site: 'without a property id',
  feature_off: 'recordings of a switched-off feature',
};

async function tracking(root, ctx, p, { disabled, save }) {
  const code = card({ title: 'Install', span: 7 });
  code.body.append(el('p', {}, 'Add this tag to the ', el('code', { text: '<head>' }), ' of every page:'), snippetBlock(p, ctx.me && ctx.me.public_url));
  root.appendChild(code.root);
  const live = card({ title: 'What arrives', span: 5 });
  const st = statusOf(p);
  live.body.append(el('div', { class: 'wait-box ' + (st.cls === 'ok' ? 'ok' : '') }, icon(st.cls === 'ok' ? 'check' : 'info'),
    el('div', {}, el('b', { text: st.text }), el('div', { class: 'note', text: p.last_hit ? 'Last hit ' + fmt.when(p.last_hit) + ' (' + fmt.ago(p.last_hit) + ')' : 'No hit received yet.' }))));
  const chartBox = el('div');
  live.body.append(el('div', { class: 'note', text: 'Hits stored per day (UTC), last 14 days' }), chartBox);
  columnChart(chartBox, { labels: (p.hits_series || []).map((x) => x.day), values: (p.hits_series || []).map((x) => x.hits), height: 120,
    labelFormat: (d) => fmt.day(d), name: 'Hits' });
  const flagged = Object.entries(p.flagged_7d || {});
  if (flagged.length) {
    live.body.appendChild(el('div', { class: 'note', text: 'Refused or flagged in the last 7 days:' }));
    live.body.appendChild(el('ul', { class: 'flag-list' }, ...flagged.map(([k, n]) => el('li', {}, el('b', { text: fmt.num(n) }), ' ' + (FLAG_TEXT[k] || k)))));
  }
  for (const r of p.rejected_origins || []) {
    const allow = el('button', { type: 'button', class: 'btn small', disabled: disabled || null, text: 'Allow' });
    allow.addEventListener('click', () => save({ origins: p.origins.concat([r.origin]) }, r.origin + ' allowed'));
    live.body.appendChild(el('div', { class: 'notice warn' }, icon('alert'),
      el('span', {}, fmt.plural(r.requests, 'request') + ' from ', el('code', { text: r.origin }), ' were refused: it is not one of the domains.'), allow));
  }
  const refused = Object.entries(p.refused_today || {});
  if (refused.length) {
    live.body.appendChild(el('div', { class: 'note', text: 'Refused today: ' + refused.map(([k, n]) => fmt.num(n) + ' ' + k.replace(/_/g, ' ')).join(', ') }));
  }
  root.appendChild(live.root);
}

// ---- goals ------------------------------------------------------------------------------------------------------------
async function goals(root, ctx, p, { disabled, save }) {
  const c = card({ title: 'Goals', span: 12, info: 'A goal is reached when a visit sends a custom event, or views a page, matching it. * matches anything: /thank-you*, purchase_*.' });
  root.appendChild(c.root);
  let list = p.goals.map((g) => ({ ...g }));
  const tbody = el('tbody');
  const sb = saveBar(() => save({ goals: list.map((g) => ({ id: g.id, name: g.name, type: g.type, match: g.match })) }, 'Goals saved'), disabled);
  const draw = () => {
    tbody.replaceChildren();
    if (!list.length) tbody.appendChild(el('tr', {}, el('td', { colspan: 4, class: 'empty', text: 'No goals yet: add one below.' })));
    list.forEach((g, i) => {
      const del = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Remove ' + g.name, disabled: disabled || null }, icon('trash'));
      del.addEventListener('click', () => { list.splice(i, 1); draw(); sb.dirty(); });
      tbody.appendChild(el('tr', {}, el('td', {}, el('b', { text: g.name })), el('td', {}, el('span', { class: 'badge', text: g.type === 'event' ? 'Event' : 'Page' })),
        el('td', {}, el('code', { text: g.match })), el('td', { class: 'num' }, del)));
    });
  };
  draw();
  const name = el('input', { class: 'input', placeholder: 'e.g. Signup', maxlength: 100, disabled: disabled || null });
  const type = el('select', { class: 'input', disabled: disabled || null }, el('option', { value: 'event', text: 'Custom event' }), el('option', { value: 'page', text: 'Page visit' }));
  const match = el('input', { class: 'input', placeholder: 'signup', list: 'goal-suggest', disabled: disabled || null });
  const suggest = el('datalist', { id: 'goal-suggest' });
  const add = el('button', { type: 'button', class: 'btn', disabled: disabled || null }, icon('plus'), el('span', { text: 'Add goal' }));
  const loadSuggestions = async () => {
    try {
      const res = await ctx.rawApi('breakdown', { site: p.id, dim: type.value === 'event' ? 'event' : 'page', limit: 50, from: fmt.addDays(ctx.today, -89), to: ctx.today });
      suggest.replaceChildren(...res.rows.map((r) => el('option', { value: r.value })));
    } catch (e) { /* no suggestions */ }
  };
  type.addEventListener('change', () => { match.placeholder = type.value === 'event' ? 'signup' : '/thank-you*'; loadSuggestions(); });
  add.addEventListener('click', () => {
    if (!name.value.trim()) { name.focus(); return; }
    if (!match.value.trim()) { match.focus(); return; }
    list.push({ name: name.value.trim(), type: type.value, match: match.value.trim() });
    name.value = ''; match.value = '';
    draw();
    sb.dirty();
  });
  loadSuggestions();
  c.body.append(el('table', { class: 'data' }, el('thead', {}, el('tr', {}, el('th', { text: 'Goal' }), el('th', { text: 'Type' }), el('th', { text: 'Matches' }), el('th'))), tbody),
    el('div', { class: 'goal-add' }, field('Name', name), field('When a visit…', type), field('Event name or page path', match, 'Suggestions come from the last 90 days.'), add, suggest), sb.bar);
}

// ---- data collection ---------------------------------------------------------------------------------------------------
function collection(root, ctx, p, { disabled, save }) {
  const changes = { features: {} };
  const sb = saveBar(() => save(changes, 'Data collection updated'), disabled);
  const intro = card({ title: 'Data collection', span: 12 });
  intro.body.appendChild(el('p', { class: 'note', text: 'Switches apply to visitors’ browsers within minutes (the tracker refreshes its configuration). Turning a feature off stops new data; what was recorded stays.' }));
  root.appendChild(intro.root);
  for (const g of FEATURES) {
    const c = card({ title: g.group, span: 6 });
    for (const [key, label, help, opt] of g.items) {
      const row = el('div', { class: 'feature-row' });
      const sw = toggle(p.features[key], (on) => { changes.features[key] = on; sb.dirty(); }, { disabled, id: 'f-' + key });
      row.append(sw, el('label', { for: 'f-' + key, class: 'feature-text' }, el('b', { text: label }), el('span', { class: 'note', text: help })));
      if (opt) {
        const rate = el('input', { type: 'number', class: 'input tiny', min: 0, max: 100, step: 1, disabled: disabled || null,
          value: Math.round(((p.options[opt] || {}).sample_rate ?? 1) * 100), 'aria-label': label + ' sample rate (%)' });
        rate.addEventListener('input', () => { changes[opt] = Object.assign({}, changes[opt] || {}, { sample_rate: Math.min(1, Math.max(0, Number(rate.value) / 100)) }); sb.dirty(); });
        const rateBox = el('span', { class: 'rate' }, rate, el('span', { class: 'note', text: '% of visits' }));
        hint(rateBox, 'Share of visits recorded (decided per visit)');
        row.appendChild(rateBox);
      }
      c.body.appendChild(row);
    }
    root.appendChild(c.root);
  }
  const ex = card({ title: 'Pages not tracked', span: 12 });
  const paths = tagInput(p.exclude_paths, { placeholder: '/admin/*, /preview/*', disabled, onChange: (v) => { changes.exclude_paths = v; sb.dirty(); } });
  ex.body.append(field('Excluded paths', paths, 'Pages whose path matches (* for anything) send nothing.', { wide: true }), sb.bar);
  root.appendChild(ex.root);
}

// ---- privacy ------------------------------------------------------------------------------------------------------------
function privacy(root, ctx, p, { disabled, save }) {
  const changes = { privacy: {} };
  const sb = saveBar(() => save(changes, 'Privacy settings saved'), disabled);
  const c = card({ title: 'Privacy', span: 12, info: 'These apply to new data. To clean what is already stored, run jobs.py privacy-clean (docs/privacy.md).' });
  const ipMode = p.privacy.hash_ip ? 'hash' : p.privacy.truncate_ip ? 'truncate' : 'keep';
  const ip = segmented([{ id: 'keep', label: 'Keep' }, { id: 'truncate', label: 'Truncate' }, { id: 'hash', label: 'Hash' }], ipMode, (v) => {
    changes.privacy.truncate_ip = v === 'truncate';
    changes.privacy.hash_ip = v === 'hash';
    sb.dirty();
  }, { label: 'IP addresses' });
  if (disabled) ip.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  const sw = (key, label, help) => el('div', { class: 'feature-row' }, toggle(p.privacy[key], (on) => { changes.privacy[key] = on; sb.dirty(); }, { disabled, id: 'p-' + key }),
    el('label', { for: 'p-' + key, class: 'feature-text' }, el('b', { text: label }), el('span', { class: 'note', text: help })));
  const redactPaths = tagInput(p.redact_paths, { placeholder: '/reset-password/*', disabled, onChange: (v) => { changes.redact_paths = v; sb.dirty(); } });
  const redactParams = tagInput(p.redact_params, { disabled, onChange: (v) => { changes.redact_params = v; sb.dirty(); } });
  c.body.append(el('div', { class: 'form' },
    field('IP addresses', ip, 'Keep: full addresses (best geolocation and bot detection). Truncate: the last part dropped (/24, /48). Hash: a keyed hash (counts unique networks, reveals nothing).', { wide: true }),
    sw('honour_dnt', 'Honour Do Not Track', 'Browsers sending DNT: 1 are not recorded.'),
    sw('honour_gpc', 'Honour Global Privacy Control', 'Browsers sending Sec-GPC: 1 are not recorded.'),
    sw('drop_fingerprint', 'No device fingerprinting', 'Turns off every fingerprint feature.'),
    field('Paths with secrets', redactPaths, 'What * matches becomes “redacted” before leaving the browser (e.g. /reset-password/* or /invite/*/accept).', { wide: true }),
    field('URL parameters never recorded', redactParams, 'Values of these parameters (glob, any case) are removed from every URL and referrer.', { wide: true })), sb.bar);
  root.appendChild(c.root);
}

// ---- sharing ---------------------------------------------------------------------------------------------------------------
function sharing(root, ctx, p, { disabled, save }) {
  const changes = {};
  const sb = saveBar(() => save(changes, 'Sharing updated'), disabled);
  const c = card({ title: 'Public statistics page', span: 12, info: 'A read-only page with a few charts, for anyone with the link. Rows of fewer than 3 visitors are never shown.' });
  const pub = toggle(p.public, (on) => { changes.public = on; sb.dirty(); }, { disabled, label: 'Publish a statistics page for this property' });
  const metrics = el('div', { class: 'check-grid' });
  const chosen = new Set(p.public_metrics);
  for (const [k, label] of PUBLIC_METRICS) {
    const box = el('input', { type: 'checkbox', id: 'pm-' + k, checked: chosen.has(k) || null, disabled: disabled || null });
    box.addEventListener('change', () => { if (box.checked) chosen.add(k); else chosen.delete(k); changes.public_metrics = [...chosen]; sb.dirty(); });
    metrics.appendChild(el('label', { class: 'check-row', for: 'pm-' + k }, box, el('span', { text: label })));
  }
  const base = collectorBase(ctx.me && ctx.me.public_url);
  const token = typeof p.share_token === 'string' ? p.share_token : '';
  const url = base + 'share/' + encodeURIComponent(p.id) + '/' + (token ? '?t=' + encodeURIComponent(token) : '');
  const tokenRow = el('div', { class: 'copy-field' });
  if (token) {
    const revoke = el('button', { type: 'button', class: 'btn small ghost danger', text: 'Remove the token', disabled: disabled || null });
    revoke.addEventListener('click', () => save({ share_token: '' }, 'Anyone with the address can now open the page'));
    const regen = el('button', { type: 'button', class: 'btn small ghost', text: 'New token', disabled: disabled || null });
    regen.addEventListener('click', () => save({ share_token: 'generate' }, 'New token: the old link stops working'));
    tokenRow.append(el('code', { text: token.slice(0, 6) + '…' }), regen, revoke);
  } else {
    const gen = el('button', { type: 'button', class: 'btn small', text: 'Require a token', disabled: disabled || null });
    gen.addEventListener('click', () => save({ share_token: 'generate' }, 'Token created: only the link with it opens the page'));
    tokenRow.append(el('span', { class: 'note', text: 'No token: anyone who knows the address can open the page.' }), gen);
  }
  const link = el('div', { class: 'copy-field' }, el('code', { class: 'wrap', text: url }), copyButton(url, 'Copy link'),
    p.public ? el('a', { class: 'btn small ghost', href: url, target: '_blank', rel: 'noopener' }, icon('external'), el('span', { text: 'Open' })) : null);
  c.body.append(el('div', { class: 'form' }, pub, field('Shown on the page', metrics, null, { wide: true }),
    field('Access token', tokenRow, 'With a token, the link carries it (?t=…); the page and its data refuse requests without it.', { wide: true }),
    field('Link', link, p.public ? null : 'The page is off until published.', { wide: true })), sb.bar);
  root.appendChild(c.root);
}

// ---- delete ---------------------------------------------------------------------------------------------------------------
function danger(root, ctx, p, { disabled }) {
  const c = card({ title: 'Delete this property', span: 12 });
  const btn = el('button', { type: 'button', class: 'btn danger', disabled: disabled || null }, icon('trash'), el('span', { text: 'Delete property…' }));
  btn.addEventListener('click', () => confirmDelete(ctx, p));
  c.body.append(el('p', { text: 'The property disappears from the dashboard and its tracking code stops being accepted. You choose whether its data is kept (re-create a property with the same id to see it again) or deleted for good.' }), btn);
  root.appendChild(c.root);
}

export async function confirmDelete(ctx, p) {
  let deleteData = false;
  const choice = segmented([{ id: 'keep', label: 'Keep its data' }, { id: 'delete', label: 'Delete its data' }], 'keep', (v) => { deleteData = v === 'delete'; note.hidden = !deleteData; });
  const note = el('p', { class: 'notice warn', hidden: true, text: 'Every stored hit, visit, recording and note of this property is deleted by the next scheduled job run. Backups made before keep it until they rotate out. This cannot be undone.' });
  const ok = await confirm({ title: 'Delete “' + p.label + '”?', danger: true, confirmLabel: 'Delete property', confirmText: p.id,
    message: el('div', { class: 'stack' }, el('p', { text: 'Its tracking code stops being accepted (hits arriving later are stored flagged, out of reports).' }), choice, note) });
  if (!ok) return;
  try {
    const res = await send('DELETE', 'property', { confirm: p.id, delete_data: deleteData }, { site: p.id });
    toast(res.data === 'kept' ? 'Property deleted; its data is kept' : 'Property deleted; its data will be removed', { kind: 'success' });
    await refreshSites();
    go('properties', { site: null });
  } catch (e) {
    toast(e.message, { kind: 'error' });
  }
}

