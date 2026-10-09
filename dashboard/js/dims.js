// What can be reported and filtered: dimensions (grouped, labelled, how their values read) and metrics (how they
// format, which way is good). The API's own lists are in server/python/tidesight/query.py.
import * as fmt from './fmt.js';

export const DIM_GROUPS = [
  { label: 'Acquisition', dims: [
    ['channel', 'Channel'], ['source', 'Source'], ['medium', 'Medium'], ['campaign', 'Campaign'], ['term', 'Term'],
    ['content', 'Content'], ['referrer', 'Referrer'], ['category', 'Source category']] },
  { label: 'Content', dims: [['page', 'Page'], ['entry', 'Entry page'], ['exit', 'Exit page'], ['title', 'Page title']] },
  { label: 'Location', dims: [['country', 'Country'], ['region', 'Region'], ['city', 'City'], ['language', 'Language'],
    ['timezone', 'Time zone'], ['asn', 'Network']] },
  { label: 'Technology', dims: [['device', 'Device'], ['browser', 'Browser'], ['browser_version', 'Browser version'],
    ['os', 'Operating system'], ['os_version', 'OS version'], ['screen', 'Screen size'], ['brand', 'Device brand'],
    ['model', 'Device model']] },
  { label: 'Behaviour', dims: [['event', 'Event'], ['goal', 'Goal'], ['new', 'New or returning'], ['hour', 'Hour of day'],
    ['weekday', 'Day of week']] },
  { label: 'People', dims: [['visitor', 'Visitor'], ['uid', 'User id'], ['bot', 'Bot name']] },
];
export const DIM_LABEL = Object.fromEntries(DIM_GROUPS.flatMap((g) => g.dims));
export const PAGE_DIMS = new Set(['page', 'title']);
export const EVENT_DIMS = new Set(['event']);
export const REPORT_DIMS = DIM_GROUPS.flatMap((g) => g.dims.map(([d]) => d)).filter((d) => d !== 'goal');
// Dimensions whose values are free text worth searching with "contains" (others are short lists).
export const TEXT_DIMS = new Set(['page', 'entry', 'exit', 'title', 'referrer', 'source', 'campaign', 'term', 'content',
  'city', 'region', 'event', 'asn', 'model', 'uid', 'visitor', 'browser_version', 'os_version']);

export const OPS = [
  { id: 'is', label: 'is', short: 'is' },
  { id: 'is_not', label: 'is not', short: 'is not' },
  { id: 'contains', label: 'contains', short: 'contains' },
  { id: 'not_contains', label: 'does not contain', short: 'excludes' },
  { id: 'starts_with', label: 'starts with', short: 'starts with' },
];
export const OP_LABEL = Object.fromEntries(OPS.map((o) => [o.id, o.label]));
export const EXACT_ONLY = new Set(['hour', 'new', 'goal', 'weekday']);
// Day names, Monday first; the API numbers days from Sunday (0) to Saturday (6).
const dayName = new Intl.DateTimeFormat(undefined, { weekday: 'long' });
export const WEEKDAYS = [1, 2, 3, 4, 5, 6, 0].map((d) => ({ value: String(d), label: dayName.format(new Date(2024, 0, 7 + d)) }));

const DEVICE = { desktop: 'Desktop', mobile: 'Mobile', tablet: 'Tablet', tv: 'TV', console: 'Console', wearable: 'Wearable',
  car: 'Car', bot: 'Bot', unknown: 'Unknown' };
const CHANNEL_HINT = { Direct: 'Typed or bookmarked, or the referrer was hidden' };

export function scopeOf(dim, dim2) {
  if (PAGE_DIMS.has(dim) || PAGE_DIMS.has(dim2)) return 'page';
  if (EVENT_DIMS.has(dim) || EVENT_DIMS.has(dim2)) return 'event';
  return 'session';
}

// How a value of a dimension reads. `goals` maps goal ids to names (for goal filters).
export function labelFor(dim, value, row, goals) {
  if (value === null || value === undefined || value === '(none)') {
    if (dim === 'referrer' || dim === 'source') return 'Direct / none';
    if (dim === 'campaign' || dim === 'medium' || dim === 'term' || dim === 'content') return '(not set)';
    return '(unknown)';
  }
  switch (dim) {
    case 'country': return fmt.country(value);
    case 'device': return DEVICE[value] || value;
    case 'hour': return String(value).padStart(2, '0') + ':00';
    case 'weekday': { const d = WEEKDAYS.find((w) => w.value === String(value)); return d ? d.label : String(value); }
    case 'new': return String(value) === '1' ? 'New visitor' : 'Returning visitor';
    case 'language': { const name = fmt.language(value); return name && name !== value ? name + ' (' + value + ')' : value; }
    case 'goal': return (goals && goals[value]) || value;
    default: return String(value);
  }
}
export function hintFor(dim, value) {
  if (dim === 'channel') return CHANNEL_HINT[value] || '';
  return '';
}
export function prefixFor(dim, row) {
  if (dim === 'country') return fmt.flag(row.value);
  if (dim === 'city' && row.country) return fmt.flag(row.country);
  return '';
}
export function isPath(dim) { return dim === 'page' || dim === 'entry' || dim === 'exit'; }

// Metrics: key -> label, format, whether up is good, and whether it adds up across rows (a share of the total).
const rate = (v) => fmt.pct(v, 1);
export const METRICS = {
  visitors: { label: 'Visitors', short: 'Visitors', format: fmt.num, compact: fmt.compact, additive: true,
    help: 'Unique visitors (people, as far as Tidesight can link their visits)' },
  sessions: { label: 'Visits', short: 'Visits', format: fmt.num, compact: fmt.compact, additive: true, help: 'Sessions' },
  pageviews: { label: 'Pageviews', short: 'Views', format: fmt.num, compact: fmt.compact, additive: true },
  views_per_session: { label: 'Views per visit', short: 'Views/visit', format: (v) => fmt.dec(v, 2), compact: (v) => fmt.dec(v, 1) },
  bounce_rate: { label: 'Bounce rate', short: 'Bounce', format: rate, compact: (v) => fmt.pct(v), up: false,
    help: 'Visits with one page and under 10 seconds of engagement' },
  avg_duration: { label: 'Visit duration', short: 'Duration', format: fmt.duration, compact: fmt.duration },
  avg_engaged: { label: 'Engaged time', short: 'Engaged', format: fmt.duration, compact: fmt.duration,
    help: 'Average time the page was visible and in use' },
  new_visitors: { label: 'New visitors', short: 'New', format: fmt.num, compact: fmt.compact, additive: true },
  avg_time: { label: 'Time on page', short: 'Time', format: fmt.duration, compact: fmt.duration },
  avg_scroll: { label: 'Scroll depth', short: 'Scroll', format: (v) => (v === null || v === undefined ? '—' : v + '%'),
    compact: (v) => (v === null || v === undefined ? '—' : v + '%') },
  entries: { label: 'Entries', short: 'Entries', format: fmt.num, compact: fmt.compact, additive: true,
    help: 'Visits that started on this page' },
  exits: { label: 'Exits', short: 'Exits', format: fmt.num, compact: fmt.compact, additive: true },
  exit_rate: { label: 'Exit rate', short: 'Exit rate', format: rate, compact: (v) => fmt.pct(v), up: false },
  count: { label: 'Events', short: 'Events', format: fmt.num, compact: fmt.compact, additive: true },
  total_value: { label: 'Total value', short: 'Value', format: (v) => (v ? fmt.dec(v, 2) : '—'), compact: fmt.compact, additive: true },
  avg_value: { label: 'Average value', short: 'Avg value', format: (v) => (v ? fmt.dec(v, 2) : '—'), compact: (v) => fmt.dec(v, 1) },
  conversions: { label: 'Conversions', short: 'Conv.', format: fmt.num, compact: fmt.compact, additive: true,
    help: 'Visits that completed the goal' },
  conversion_rate: { label: 'Conversion rate', short: 'Conv. rate', format: rate, compact: (v) => fmt.pct(v) },
  events: { label: 'Events', short: 'Events', format: fmt.num, compact: fmt.compact, additive: true },
};
export function metric(key) { return METRICS[key] || { label: key, short: key, format: fmt.num, compact: fmt.compact }; }
export const SCOPE_METRICS = {
  session: ['visitors', 'sessions', 'pageviews', 'views_per_session', 'bounce_rate', 'avg_duration', 'avg_engaged', 'new_visitors'],
  page: ['pageviews', 'visitors', 'sessions', 'avg_time', 'avg_engaged', 'avg_scroll', 'entries', 'exits', 'exit_rate', 'bounce_rate'],
  event: ['count', 'visitors', 'sessions', 'total_value', 'avg_value'],
};
export const DEFAULT_COLUMNS = {
  session: ['visitors', 'sessions', 'pageviews', 'bounce_rate', 'avg_duration'],
  page: ['pageviews', 'visitors', 'avg_engaged', 'avg_scroll', 'entries', 'exit_rate'],
  event: ['count', 'visitors', 'sessions', 'total_value'],
};
export const DEFAULT_SORT = { session: 'visitors', page: 'pageviews', event: 'count' };
