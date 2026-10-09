// The report state lives in the URL hash (every view is bookmarkable and shareable):
// #/<view>?site=…&period=30d|custom&from=…&to=…&compare=previous|year|custom&cfrom=…&cto=…&filters=[…]&bots=…&goal=…
import { api, PUBLIC, PUBLIC_SITE } from './api.js';
import * as fmt from './fmt.js';

export const GLOBAL_KEYS = ['site', 'period', 'from', 'to', 'compare', 'cfrom', 'cto', 'filters', 'bots', 'goal'];

export const PRESETS = [
  { id: 'today', label: 'Today', key: 't' }, { id: 'yesterday', label: 'Yesterday', key: 'y' },
  { id: '7d', label: 'Last 7 days', key: 'w' }, { id: '28d', label: 'Last 28 days' }, { id: '30d', label: 'Last 30 days', key: 'm' },
  { id: '90d', label: 'Last 90 days' }, { id: 'wtd', label: 'This week' }, { id: 'lastweek', label: 'Last week' },
  { id: 'mtd', label: 'This month' }, { id: 'lastmonth', label: 'Last month' }, { id: 'qtd', label: 'This quarter' },
  { id: 'ytd', label: 'This year' }, { id: '12m', label: 'Last 12 months' }, { id: 'all', label: 'All time', key: 'a' },
];
export const COMPARE_MODES = [
  { id: 'previous', label: 'Previous period' }, { id: 'year', label: 'Same period last year' }, { id: 'custom', label: 'Custom period' },
];

export const state = { sites: [], version: '', me: null, freshness: null, segments: null };

// Today in the property's time zone (its days are what reports count).
export function todayIn(tz) {
  try {
    return new Intl.DateTimeFormat('en-CA', { timeZone: tz || undefined, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  } catch (e) {
    return fmt.isoDay(new Date());
  }
}

export function presetRange(id, today, firstDay) {
  const t = fmt.parseDay(today);
  const iso = (d) => fmt.isoDay(d);
  const monday = (d) => { const x = new Date(d); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; };
  switch (id) {
    case 'today': return [today, today];
    case 'yesterday': { const y = fmt.addDays(today, -1); return [y, y]; }
    case '7d': return [fmt.addDays(today, -6), today];
    case '28d': return [fmt.addDays(today, -27), today];
    case '90d': return [fmt.addDays(today, -89), today];
    case 'wtd': return [iso(monday(t)), today];
    case 'lastweek': { const m = monday(t); m.setDate(m.getDate() - 7); const e = new Date(m); e.setDate(e.getDate() + 6); return [iso(m), iso(e)]; }
    case 'mtd': return [iso(new Date(t.getFullYear(), t.getMonth(), 1)), today];
    case 'lastmonth': return [iso(new Date(t.getFullYear(), t.getMonth() - 1, 1)), iso(new Date(t.getFullYear(), t.getMonth(), 0))];
    case 'qtd': return [iso(new Date(t.getFullYear(), Math.floor(t.getMonth() / 3) * 3, 1)), today];
    case 'ytd': return [iso(new Date(t.getFullYear(), 0, 1)), today];
    case '12m': return [fmt.addDays(today, -364), today];
    case 'all': return [firstDay && firstDay < today ? firstDay : fmt.addDays(today, -364), today];
    default: return [fmt.addDays(today, -29), today];
  }
}

// The same-length period before or after (whole months move by months).
export function shiftRange(from, to, dir) {
  const a = fmt.parseDay(from), b = fmt.parseDay(to);
  const lastOfMonth = new Date(b.getFullYear(), b.getMonth() + 1, 0).getDate() === b.getDate();
  if (a.getDate() === 1 && lastOfMonth) {
    const months = (b.getFullYear() - a.getFullYear()) * 12 + b.getMonth() - a.getMonth() + 1;
    const na = new Date(a.getFullYear(), a.getMonth() + dir * months, 1);
    const nb = new Date(na.getFullYear(), na.getMonth() + months, 0);
    return [fmt.isoDay(na), fmt.isoDay(nb)];
  }
  const n = fmt.daysBetween(from, to) + 1;
  return [fmt.addDays(from, dir * n), fmt.addDays(to, dir * n)];
}

export function comparisonRange(mode, from, to, cfrom, cto) {
  if (mode === 'previous') { const n = fmt.daysBetween(from, to) + 1; return [fmt.addDays(from, -n), fmt.addDays(from, -1)]; }
  if (mode === 'year') {
    const back = (d) => { const x = fmt.parseDay(d); const m = x.getMonth(); x.setFullYear(x.getFullYear() - 1); if (x.getMonth() !== m) x.setDate(0); return fmt.isoDay(x); };
    return [back(from), back(to)];
  }
  if (mode === 'custom' && cfrom && cto) return cfrom <= cto ? [cfrom, cto] : [cto, cfrom];
  return null;
}

// ---- the hash ---------------------------------------------------------------------------------
export function route() {
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?');
  return { view: path || 'overview', params: new URLSearchParams(query || '') };
}

let renderHook = () => {};
export function onReplace(fn) { renderHook = fn; }

export function go(view, changes = {}, replace = false) {
  const { view: current, params } = route();
  if (view !== current) {  // view-specific settings do not follow to another view
    for (const k of [...params.keys()]) if (!GLOBAL_KEYS.includes(k)) params.delete(k);
  }
  for (const [k, v] of Object.entries(changes)) {
    if (v === null || v === undefined || v === '') params.delete(k); else params.set(k, v);
  }
  const hash = '#/' + view + (params.toString() ? '?' + params.toString() : '');
  if (replace) { history.replaceState(null, '', hash); renderHook(); } else if (location.hash !== hash) location.hash = hash; else renderHook();
}
// Change parameters of the current view in the URL without rendering it again (a table that already shows them).
export function patchParams(changes) {
  const { view, params } = route();
  for (const [k, v] of Object.entries(changes)) {
    if (v === null || v === undefined || v === '') params.delete(k); else params.set(k, v);
  }
  history.replaceState(null, '', '#/' + view + (params.toString() ? '?' + params.toString() : ''));
}
export function hrefFor(view, changes = {}) {
  const { params } = route();
  for (const k of [...params.keys()]) if (!GLOBAL_KEYS.includes(k)) params.delete(k);
  for (const [k, v] of Object.entries(changes)) {
    if (v === null || v === undefined || v === '') params.delete(k); else params.set(k, v);
  }
  return '#/' + view + (params.toString() ? '?' + params.toString() : '');
}

export function decodeFilters(params) {
  const out = [];
  const raw = params.get('filters');
  if (raw) {
    try {
      for (const f of JSON.parse(raw)) {
        if (Array.isArray(f) && f.length === 3 && Array.isArray(f[2])) out.push({ dim: String(f[0]), op: String(f[1]), values: f[2].map(String) });
      }
    } catch (e) { /* a broken link: no filters */ }
  }
  for (const [k, v] of params) if (k.startsWith('f.') && v) out.push({ dim: k.slice(2), op: 'is', values: [v] });  // older links
  return out;
}
export function encodeFilters(filters) {
  return filters.length ? JSON.stringify(filters.map((f) => [f.dim, f.op, f.values])) : null;
}
export function sameFilters(a, b) {
  return JSON.stringify(a.map((f) => [f.dim, f.op, [...f.values].sort()]).sort()) === JSON.stringify(b.map((f) => [f.dim, f.op, [...f.values].sort()]).sort());
}

// Everything a view needs: the property, the period, the comparison, filters, and API helpers.
export function context() {
  const { view, params } = route();
  let siteId = PUBLIC ? PUBLIC_SITE : params.get('site') || storage.get('ts_dash_site');
  if (!PUBLIC && !state.sites.some((x) => x.id === siteId)) siteId = state.sites[0] && state.sites[0].id;
  const siteInfo = state.sites.find((x) => x.id === siteId);
  const today = todayIn(siteInfo && siteInfo.timezone);
  let period = params.get('period') || '30d';
  let [from, to] = presetRange(period, today, siteInfo && siteInfo.first_day);
  if (params.get('from') && params.get('to') && /^\d{4}-\d{2}-\d{2}$/.test(params.get('from')) && /^\d{4}-\d{2}-\d{2}$/.test(params.get('to'))) {
    period = 'custom';
    [from, to] = params.get('from') <= params.get('to') ? [params.get('from'), params.get('to')] : [params.get('to'), params.get('from')];
  } else if (!PRESETS.some((p) => p.id === period)) {
    period = '30d';
  }
  const compareMode = PUBLIC ? null : params.get('compare');
  const cmp = compareMode ? comparisonRange(compareMode, from, to, params.get('cfrom'), params.get('cto')) : null;
  const filters = PUBLIC ? [] : decodeFilters(params);
  const bots = ['include', 'only'].includes(params.get('bots')) ? params.get('bots') : params.get('bots') === '1' ? 'include' : 'exclude';
  const goals = (siteInfo && siteInfo.goals) || [];
  const goal = goals.some((g) => g.id === params.get('goal')) ? params.get('goal') : null;
  const common = { site: siteId, from, to };
  if (filters.length) common.filters = JSON.stringify(filters.map((f) => ({ dim: f.dim, op: f.op, values: f.values })));
  if (bots !== 'exclude') common.bots = bots;
  const withCompare = cmp ? { compare: compareMode, ...(compareMode === 'custom' ? { compare_from: cmp[0], compare_to: cmp[1] } : {}) } : {};
  const ctx = {
    view, params, site: siteId, siteInfo, sites: state.sites, me: state.me, today,
    from, to, period, compare: cmp ? { mode: compareMode, from: cmp[0], to: cmp[1] } : null,
    filters, bots, goal, goals, goalNames: Object.fromEntries(goals.map((g) => [g.id, g.name])), public: PUBLIC,
    // API parameters of this state (comparison included unless asked not to).
    query: (extra = {}, { compare = true } = {}) => ({ ...common, ...(compare ? withCompare : {}), ...extra }),
    api: (endpoint, extra = {}, opts = {}) => api(endpoint, ctx.query(extra, opts)),
    rawApi: api,
    go,
    patch: patchParams,
    setFilters: (list, extra = {}) => go(view, { filters: encodeFilters(list), offset: null, ...extra }),
    // A filter on a dimension already filtered the same way replaces it; exclusions add up.
    addFilter: (f) => {
      const same = filters.find((x) => x.dim === f.dim && x.op === f.op);
      if (same && f.op === 'is_not') {
        ctx.setFilters(filters.map((x) => (x === same ? { ...x, values: [...new Set([...x.values, ...f.values])] } : x)));
      } else {
        ctx.setFilters([...filters.filter((x) => x !== same), f]);
      }
    },
    setFilter: (dim, value, op = 'is') => ctx.addFilter({ dim, op, values: [String(value)] }),
    removeFilter: (i) => ctx.setFilters(filters.filter((_, k) => k !== i)),
    fmt,
  };
  return ctx;
}

// localStorage can be missing or throw (private windows, blocked storage): never let that break the page.
export const storage = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { if (v === null || v === undefined) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  json(k, fallback) { try { const v = JSON.parse(localStorage.getItem(k)); return v === null ? fallback : v; } catch (e) { return fallback; } },
};
