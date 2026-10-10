// Properties: every site Tidesight records, whether data is arriving, and adding a new one (a three-step wizard:
// website, data collection, tracking code with a live check for the first hit).
import { send } from '../api.js';
import { card, el, icon, sparkline } from '../charts.js';
import { hint } from '../dom.js';
import * as fmt from '../fmt.js';
import { refreshSites } from '../main.js';
import { go, hrefFor, state } from '../state.js';
import { dialog, field, menu, toast } from '../ui.js';
import { confirmDelete, snippetBlock } from './property.js';

export async function render(root, ctx) {
  const res = await ctx.rawApi('properties');
  const admin = res.can_edit;
  const head = card({ title: 'Properties', span: 12, className: 'props-head' });
  const search = el('input', { class: 'input search', type: 'search', placeholder: 'Find a property…', 'aria-label': 'Find a property' });
  const add = el('button', { type: 'button', class: 'btn primary', disabled: !admin || null }, icon('plus'), el('span', { text: 'Add property' }));
  if (!admin) hint(add, 'Only admins can add properties (an admin can change your role on the Users page)');
  add.addEventListener('click', () => addWizard(ctx, res));
  head.body.appendChild(el('div', { class: 'props-bar' },
    el('p', { class: 'note', text: fmt.plural(res.properties.length, 'property', 'properties') + '. A property is a website (one or more domains) with its own reports, goals and settings.' }),
    el('span', { class: 'spacer' }), res.properties.length > 6 ? el('div', { class: 'search-box' }, icon('search'), search) : null, add));
  root.appendChild(head.root);

  for (const p of res.pending_deletions || []) {
    root.appendChild(el('div', { class: 'notice warn span-12' }, icon('trash'),
      el('span', { text: 'The data of the deleted property “' + p.site_id + '” is being deleted: the next scheduled job run removes it (requested ' + fmt.ago(p.requested_at) + (p.requested_by ? ' by ' + p.requested_by : '') + ').' })));
  }
  for (const u of res.unknown || []) {
    const b = el('button', { type: 'button', class: 'btn small', disabled: !admin || !u.valid_id || null, text: 'Create this property' });
    b.addEventListener('click', () => addWizard(ctx, res, { id: u.id }));
    root.appendChild(el('div', { class: 'notice span-12' }, icon('info'),
      el('span', {}, 'Data is arriving for a property id that does not exist: ', el('code', { text: u.id }),
        ' (' + fmt.plural(u.requests, 'request') + ', last ' + fmt.ago(u.last_seen) + '). It is stored but kept out of reports until the property exists.'), b));
  }

  const grid = el('div', { class: 'props-grid span-12' });
  root.appendChild(grid);
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    grid.replaceChildren();
    const list = res.properties.filter((p) => !q || [p.label, p.id, ...(p.domains || [])].some((t) => String(t).toLowerCase().includes(q)));
    for (const p of list) grid.appendChild(propertyCard(ctx, p, admin));
    if (!list.length) grid.appendChild(el('div', { class: 'empty', text: res.properties.length ? 'No property matches.' : 'No property yet: add your first one.' }));
  };
  search.addEventListener('input', draw);
  draw();
  if (ctx.params.get('add') && admin) { ctx.patch({ add: null }); addWizard(ctx, res); }
}

export function statusOf(p) {
  if (!p.last_hit) return { cls: 'idle', text: 'Waiting for data', hint: 'No hit received yet: is the tracking code on the site?' };
  const age = Date.now() - p.last_hit;
  if (age < 48 * 3600000) return { cls: 'ok', text: 'Receiving data', hint: 'Last hit ' + fmt.ago(p.last_hit) };
  return { cls: 'warn', text: 'No data for ' + Math.round(age / 86400000) + ' days', hint: 'Last hit ' + fmt.when(p.last_hit) + ': check the tracking code' };
}

function propertyCard(ctx, p, admin) {
  const st = statusOf(p);
  const c = el('article', { class: 'prop-card' });
  const status = el('span', { class: 'status-pill ' + st.cls }, el('i'), el('span', { text: st.text }));
  hint(status, st.hint);
  const more = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'More actions' }, icon('dots'));
  more.addEventListener('click', () => menu(more, [
    { label: 'Open reports', icon: 'chart', onClick: () => go('overview', { site: p.id, filters: null, goal: null }) },
    { label: 'Settings', icon: 'gear', onClick: () => go('property', { site: p.id }) },
    { label: 'Tracking code', icon: 'copy', onClick: () => go('property', { site: p.id, tab: 'tracking' }) },
    p.managed && admin ? 'sep' : null,
    p.managed && admin ? { label: 'Delete…', icon: 'trash', danger: true, onClick: () => confirmDelete(ctx, p) } : null,
  ], { align: 'end' }));
  const configBadge = p.managed ? null : el('span', { class: 'badge', text: 'Config file' });
  if (configBadge) hint(configBadge, 'Defined in tidesight.toml: change it there');
  c.append(
    el('div', { class: 'prop-top' },
      el('span', { class: 'site-mark', text: (p.label || p.id).replace(/^www\./, '').slice(0, 1).toUpperCase() }),
      el('div', { class: 'prop-title' }, el('a', { href: hrefFor('overview', { site: p.id, filters: null, goal: null }), text: p.label, title: p.label }),
        el('span', { class: 'prop-meta' }, el('code', { text: p.id }), el('span', { text: p.timezone }), configBadge,
          p.public ? el('span', { class: 'badge', text: 'Public' }) : null)),
      more),
    el('div', { class: 'prop-domains' }, ...(p.domains.length ? p.domains : ['any domain']).slice(0, 5).map((d) => el('span', { class: 'tag', text: d })),
      p.domains.length > 5 ? el('span', { class: 'note', text: '+' + (p.domains.length - 5) }) : null, el('span', { class: 'spacer' }), status),
    el('div', { class: 'prop-spark' }, sparkline(p.visits_series || [], { width: 320, height: 40 })),
    el('div', { class: 'prop-stats' },
      el('span', {}, el('b', { text: fmt.compact(p.visits_30d) }), ' visits in 30 days'),
      el('span', {}, el('b', { text: fmt.compact(p.hits_today) }), ' hits today (UTC)')),
    el('div', { class: 'prop-actions' },
      el('a', { class: 'btn small', href: hrefFor('overview', { site: p.id, filters: null, goal: null }) }, icon('chart'), el('span', { text: 'Reports' })),
      el('a', { class: 'btn small ghost', href: hrefFor('property', { site: p.id }) }, icon('gear'), el('span', { text: 'Settings' }))));
  return c;
}

// ---- the add wizard ---------------------------------------------------------------------------------------
const ZONES = (() => {
  try { if (Intl.supportedValuesOf) return Intl.supportedValuesOf('timeZone'); } catch (e) { /* older browsers */ }
  return ['UTC', 'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'Asia/Tokyo', 'Australia/Sydney'];
})();
export function zoneSelect(value) {
  const sel = el('select', { class: 'input' });
  const zones = ZONES.includes('UTC') ? ZONES : ['UTC'].concat(ZONES);
  for (const z of zones) sel.appendChild(el('option', { value: z, text: z.replace(/_/g, ' '), selected: z === value || null }));
  if (value && !zones.includes(value)) sel.appendChild(el('option', { value, text: value, selected: true }));
  return sel;
}
export function cleanDomain(text) {
  let d = String(text || '').trim().toLowerCase();
  d = d.replace(/^[a-z]+:\/\//, '').replace(/[/?#].*$/, '').replace(/\.$/, '');
  return d;
}
export const DOMAIN_RE = /^(\*\.)?([a-z0-9¡-￿]([a-z0-9¡-￿-]{0,61}[a-z0-9¡-￿])?)(\.[a-z0-9¡-￿]([a-z0-9¡-￿-]{0,61}[a-z0-9¡-￿])?)*(:\d{1,5})?$|^\[[0-9a-f:.]+\](:\d{1,5})?$/;
// A host with a dot (example.com), localhost, or an IP: what a property may list.
export function validDomain(d) {
  const host = d.replace(/^https?:\/\//, '');
  if (!DOMAIN_RE.test(host)) return false;
  const name = host.replace(/:\d+$/, '');
  if (name.startsWith('*.')) return /\./.test(name.slice(2)); // never *.com
  return host.startsWith('[') || /\./.test(name) || name === 'localhost';
}
export function slug(text) {
  return (String(text).toLowerCase().replace(/^www\./, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 63) || 'site');
}

const PRESETS = [
  { id: 'standard', title: 'Standard', badge: 'Recommended', points: ['Pageviews, sources, campaigns, locations, devices', 'Engagement, scroll depth, clicks, forms, site search',
    'Web Vitals and JavaScript errors', 'Bot detection; no session recordings'] },
  { id: 'privacy', title: 'Privacy-first', points: ['Everything in Standard', 'IP addresses truncated before storage', 'No device fingerprinting',
    'Honours Do Not Track and Global Privacy Control'] },
  { id: 'complete', title: 'Everything', points: ['Everything in Standard', 'Session replays (10% of visits, inputs masked)', 'Mouse movement heatmaps',
    'Copy and paste events, long tasks'] },
];

function addWizard(ctx, listing, { id: presetId } = {}) {
  let step = 1;
  let created = null;
  let poll = null;
  const tz = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (e) { return 'UTC'; } })();
  const domain = el('input', { class: 'input wide', placeholder: 'example.com', autocomplete: 'off', spellcheck: 'false', autofocus: true,
    'aria-describedby': 'domain-help' });
  const www = el('input', { type: 'checkbox', id: 'w-www', checked: true });
  const subs = el('input', { type: 'checkbox', id: 'w-subs' });
  const wwwRow = el('label', { class: 'check-row', for: 'w-www' }, www, el('span', { class: 'www-text', text: 'Also www.' }));
  const subsRow = el('label', { class: 'check-row', for: 'w-subs' }, subs, el('span', { class: 'subs-text', text: 'And every subdomain' }));
  const name = el('input', { class: 'input wide', placeholder: 'Defaults to the domain', maxlength: 100 });
  const zone = zoneSelect(ZONES.includes(tz) ? tz : 'UTC');
  const idIn = el('input', { class: 'input mono', placeholder: 'auto', pattern: '[a-z0-9][a-z0-9-]{0,62}', value: presetId || '' });
  let idTouched = !!presetId;
  idIn.addEventListener('input', () => { idTouched = true; });
  const err = el('div', { class: 'field-error', hidden: true });
  let preset = 'standard';
  const presetBox = el('div', { class: 'preset-grid', role: 'radiogroup', 'aria-label': 'Data collection' });
  const drawPresets = () => {
    presetBox.replaceChildren();
    for (const p of PRESETS) {
      const b = el('button', { type: 'button', class: 'preset', role: 'radio', 'aria-checked': String(p.id === preset) },
        el('div', { class: 'preset-head' }, el('b', { text: p.title }), p.badge ? el('span', { class: 'badge accent', text: p.badge }) : null),
        el('ul', {}, ...p.points.map((t) => el('li', { text: t }))));
      b.addEventListener('click', () => { preset = p.id; drawPresets(); });
      presetBox.appendChild(b);
    }
  };
  drawPresets();
  const updateHints = () => {
    const d = cleanDomain(domain.value);
    const base = d.replace(/^www\./, '').replace(/^\*\./, '');
    wwwRow.hidden = !d || d.startsWith('www.') || d.startsWith('*.') || /^[\d.]+(:\d+)?$/.test(d) || d.startsWith('localhost');
    wwwRow.querySelector('.www-text').textContent = 'Also www.' + base;
    subsRow.querySelector('.subs-text').textContent = 'And every subdomain (*.' + base + ')';
    subsRow.hidden = !d || d.startsWith('*.') || /^[\d.]+(:\d+)?$/.test(d) || d.startsWith('localhost');
    name.placeholder = base || 'Defaults to the domain';
    if (!idTouched) idIn.placeholder = base ? slug(base) : 'auto';
  };
  domain.addEventListener('input', () => { err.hidden = true; updateHints(); });
  updateHints();

  const body = el('div', { class: 'wizard' });
  const steps = el('ol', { class: 'wizard-steps' });
  const back = el('button', { type: 'button', class: 'btn', text: 'Back' });
  const next = el('button', { type: 'button', class: 'btn primary', text: 'Continue' });
  const d = dialog({ title: 'Add a property', width: 720, body: el('div', {}, steps, body), actions: [back, next],
    onClose: () => {
      clearInterval(poll);
      if (created) refreshSites().then(() => { if (location.hash.startsWith('#/properties')) ctx.go('properties', {}, true); });
    } });

  const origins = () => {
    const dm = cleanDomain(domain.value);
    const base = dm.replace(/^www\./, '');
    const out = [dm];
    if (!wwwRow.hidden && www.checked) out.push('www.' + base);
    if (!subsRow.hidden && subs.checked) out.push('*.' + base);
    return out;
  };
  const drawStep = () => {
    steps.replaceChildren(...['Website', 'Data collection', 'Tracking code'].map((t, i) => el('li', { class: i + 1 === step ? 'on' : i + 1 < step ? 'done' : '' },
      el('span', { class: 'n', text: String(i + 1) }), el('span', { text: t }))));
    body.replaceChildren();
    back.hidden = step === 1 || step === 3;
    if (step === 1) {
      body.append(
        field('Website address', el('div', {}, domain, el('div', { class: 'checks' }, wwwRow, subsRow)),
          'The domain whose pages will send visits (a subdomain like blog.example.com works too). Only these domains are accepted.', { wide: true }),
        el('div', { class: 'row-2' }, field('Name (optional)', name, 'How this property is listed; the domain otherwise.'),
          field('Time zone', zone, 'Days in reports start at midnight in this zone.')),
        el('details', { class: 'advanced' }, el('summary', { text: 'Advanced' }),
          field('Property id', idIn, 'Used in the tracking code (data-site). Lower-case letters, digits and dashes.')),
        err);
      next.textContent = 'Continue';
      setTimeout(() => domain.focus(), 0);
    } else if (step === 2) {
      body.append(el('p', { class: 'note', text: 'What to record. Every switch can be changed later in the settings.' }), presetBox, err);
      next.textContent = 'Create property';
    } else {
      const status = el('div', { class: 'wait-box' });
      const setWaiting = (p) => {
        status.replaceChildren();
        if (p && p.last_hit) {
          status.className = 'wait-box ok';
          status.append(icon('check'), el('div', {}, el('b', { text: 'Data is arriving.' }), el('div', { class: 'note', text: 'First hit received ' + fmt.ago(p.last_hit) + '. Reports fill in as the enrichment job runs (every few minutes).' })));
          clearInterval(poll);
        } else {
          status.className = 'wait-box';
          status.append(el('span', { class: 'pulse' }), el('div', {}, el('b', { text: 'Waiting for the first visit…' }),
            el('div', { class: 'note', text: 'Open a page of ' + (created.domains[0] || 'your site') + ' with the code installed; this turns green within seconds.' })));
        }
      };
      setWaiting(created);
      body.append(el('p', {}, 'Paste this tag into the ', el('code', { text: '<head>' }), ' of every page of ', el('b', { text: created.label }), ':'),
        snippetBlock(created, state.me && state.me.public_url), status);
      poll = setInterval(async () => {
        try { const p = await ctx.rawApi('property', { site: created.id }); setWaiting(p); } catch (e) { /* keep waiting */ }
      }, 4000);
      next.textContent = 'Open its reports';
    }
  };
  back.addEventListener('click', () => { step -= 1; drawStep(); });
  next.addEventListener('click', async () => {
    if (step === 1) {
      const dm = cleanDomain(domain.value);
      if (!dm || !validDomain(dm)) { err.textContent = 'Enter a domain like example.com or blog.example.com.'; err.hidden = false; domain.focus(); return; }
      if (idIn.value && !/^[a-z0-9][a-z0-9-]{0,62}$/.test(idIn.value)) { err.textContent = 'The property id has lower-case letters, digits and dashes.'; err.hidden = false; return; }
      step = 2;
      drawStep();
    } else if (step === 2) {
      next.disabled = true;
      try {
        created = await send('POST', 'properties', { domains: origins(), name: name.value.trim() || undefined, timezone: zone.value,
          preset, id: idIn.value.trim() || undefined });
        toast('Property created', { kind: 'success' });
        await refreshSites();
        step = 3;
        drawStep();
      } catch (e) {
        err.textContent = e.message;
        err.hidden = false;
        if (e.status === 409 || /domain|id/i.test(e.message)) { step = 1; drawStep(); err.textContent = e.message; err.hidden = false; }
      } finally {
        next.disabled = false;
      }
    } else {
      clearInterval(poll);
      const id = created.id;
      created = null;
      d.close();
      go('overview', { site: id, filters: null, goal: null });
    }
  });
  drawStep();
  return listing;
}

