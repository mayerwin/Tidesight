// Formatting helpers: numbers, percentages, durations, dates and ranges.
const nf = new Intl.NumberFormat(undefined);
const nf1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const none = (v) => v === null || v === undefined || Number.isNaN(v);

export function num(v) {
  if (none(v)) return '—';
  return nf.format(Math.round(v));
}
export function dec(v, digits = 2) {
  if (none(v)) return '—';
  return digits === 2 ? nf2.format(v) : nf1.format(v);
}
export function compact(v) {
  if (none(v)) return '—';
  const a = Math.abs(v);
  if (a < 1000) return nf1.format(Math.round(v * 10) / 10);
  if (a < 1e6) return (v / 1e3).toFixed(a < 1e4 ? 1 : 0).replace(/\.0$/, '') + 'K';
  if (a < 1e9) return (v / 1e6).toFixed(a < 1e7 ? 1 : 0).replace(/\.0$/, '') + 'M';
  return (v / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
}
export function pct(v, digits = 0) {
  if (none(v)) return '—';
  const d = digits === 0 && Math.abs(v) < 0.1 && v !== 0 ? 1 : digits;
  return (v * 100).toFixed(d) + '%';
}
export function duration(ms) {
  if (none(ms)) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return m + 'm ' + String(r).padStart(2, '0') + 's';
  const h = Math.floor(m / 60);
  return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
}
export function ms(v) {
  if (none(v)) return '—';
  return v >= 1000 ? (v / 1000).toFixed(2) + ' s' : Math.round(v) + ' ms';
}
const dayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const dayYearFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
const dayFullFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
const monthFmt = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' });
const monthShortFmt = new Intl.DateTimeFormat(undefined, { month: 'short', year: '2-digit' });
const dtFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
export function parseDay(iso) { return new Date(iso + 'T12:00:00'); }
export function day(iso, full = false) {
  const d = parseDay(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : (full ? dayFullFmt : dayFmt).format(d);
}
export function dayYear(iso) {
  const d = parseDay(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : dayYearFmt.format(d);
}
export function month(iso, short = false) {
  const d = parseDay(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : (short ? monthShortFmt : monthFmt).format(d);
}
export function when(msTs) { return msTs ? dtFmt.format(new Date(msTs)) : '—'; }
export function time(msTs) { return msTs ? timeFmt.format(new Date(msTs)) : '—'; }
export function ago(msTs) {
  if (!msTs) return 'never';
  const s = Math.round((Date.now() - msTs) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' h ago';
  if (s < 86400 * 60) return Math.round(s / 86400) + ' d ago';
  return dayYear(isoDay(new Date(msTs)));
}
export function isoDay(d) {
  const y = d.getFullYear(), m = String(d.getMonth() + 1).padStart(2, '0'), dd = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + dd;
}
export function addDays(iso, n) {
  const d = parseDay(iso);
  d.setDate(d.getDate() + n);
  return isoDay(d);
}
export function daysBetween(a, b) { return Math.round((parseDay(b) - parseDay(a)) / 86400000); }
// "Sep 1 – 30, 2026", "Sep 28 – Oct 4", "Mar 3, 2025 – Feb 2, 2026"
export function range(from, to) {
  if (!from || !to) return '';
  if (from === to) return dayYear(from);
  const a = parseDay(from), b = parseDay(to), thisYear = new Date().getFullYear();
  if (a.getFullYear() !== b.getFullYear()) return dayYear(from) + ' – ' + dayYear(to);
  const tail = b.getFullYear() !== thisYear ? ', ' + b.getFullYear() : '';
  if (a.getMonth() === b.getMonth()) return dayFmt.format(a) + ' – ' + b.getDate() + tail;
  return dayFmt.format(a) + ' – ' + dayFmt.format(b) + tail;
}
// Change between current and previous, with whether "up" is good for this metric.
export function delta(cur, prev, upIsGood = true) {
  if (none(cur) || none(prev) || prev === 0) {
    if (!none(cur) && cur > 0 && prev === 0) return { text: 'new', cls: upIsGood ? 'good' : 'bad', dir: 'up', value: null };
    return null;
  }
  const d = (cur - prev) / Math.abs(prev);
  if (!Number.isFinite(d)) return null;
  const dir = Math.abs(d) < 0.005 ? 'flat' : d > 0 ? 'up' : 'down';
  const good = dir === 'flat' ? 'flat' : (dir === 'up') === upIsGood ? 'good' : 'bad';
  const size = Math.abs(d * 100);
  const text = (d > 0 ? '+' : d < 0 ? '−' : '±') + (size >= 1000 ? compact(size) : size.toFixed(size < 10 ? 1 : 0)) + '%';
  return { text, cls: good, dir, value: d };
}
// Windows has no flag emoji (it shows two letters): no flags there.
const NO_FLAGS = typeof navigator !== 'undefined' && /Windows/.test(navigator.userAgent || '');
export function flag(cc) {
  if (NO_FLAGS || !cc || cc.length !== 2 || !/^[A-Z]{2}$/.test(cc)) return '';
  return String.fromCodePoint(...[...cc].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}
const regionNames = typeof Intl.DisplayNames === 'function' ? new Intl.DisplayNames(undefined, { type: 'region' }) : null;
const languageNames = typeof Intl.DisplayNames === 'function' ? new Intl.DisplayNames(undefined, { type: 'language' }) : null;
export function country(cc) {
  if (!cc || cc === '(none)') return 'Unknown';
  try { return (regionNames && regionNames.of(cc)) || cc; } catch (e) { return cc; }
}
export function language(code) {
  if (!code || code === '(none)') return 'Unknown';
  try { return (languageNames && languageNames.of(code)) || code; } catch (e) { return code; }
}
export function plural(n, one, many) { return num(n) + ' ' + (n === 1 ? one : many || one + 's'); }
