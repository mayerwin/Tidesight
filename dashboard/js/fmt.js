// Formatting helpers: compact numbers, percentages, durations, dates.
const nf = new Intl.NumberFormat(undefined);
export function num(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return nf.format(Math.round(v));
}
export function compact(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const a = Math.abs(v);
  if (a < 1000) return nf.format(Math.round(v * 10) / 10);
  if (a < 1e6) return (v / 1e3).toFixed(a < 1e4 ? 1 : 0).replace(/\.0$/, '') + 'K';
  if (a < 1e9) return (v / 1e6).toFixed(a < 1e7 ? 1 : 0).replace(/\.0$/, '') + 'M';
  return (v / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
}
export function pct(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return (v * 100).toFixed(digits) + '%';
}
export function duration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return m + 'm ' + String(r).padStart(2, '0') + 's';
  const h = Math.floor(m / 60);
  return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
}
export function ms(v) {
  if (v === null || v === undefined) return '—';
  return v >= 1000 ? (v / 1000).toFixed(2) + ' s' : Math.round(v) + ' ms';
}
const dayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const dayFullFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
const dtFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
export function day(iso, full = false) {
  const d = new Date(iso + 'T12:00:00');
  return Number.isNaN(d.getTime()) ? String(iso) : (full ? dayFullFmt : dayFmt).format(d);
}
export function when(msTs) { return msTs ? dtFmt.format(new Date(msTs)) : '—'; }
export function ago(msTs) {
  const s = Math.round((Date.now() - msTs) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' d ago';
}
export function isoDay(d) {
  const y = d.getFullYear(), m = String(d.getMonth() + 1).padStart(2, '0'), dd = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + dd;
}
export function addDays(iso, n) {
  const d = new Date(iso + 'T12:00:00');
  d.setDate(d.getDate() + n);
  return isoDay(d);
}
// Delta between current and previous, with whether "up" is good for this metric.
export function delta(cur, prev, upIsGood = true) {
  if (cur === null || cur === undefined || !prev) return null;
  const d = (cur - prev) / prev;
  if (!Number.isFinite(d)) return null;
  const dir = Math.abs(d) < 0.005 ? 'flat' : d > 0 ? 'up' : 'down';
  const good = dir === 'flat' ? 'flat' : (dir === 'up') === upIsGood ? 'good' : 'bad';
  return { text: (d > 0 ? '+' : d < 0 ? '−' : '±') + Math.abs(d * 100).toFixed(Math.abs(d) < 0.1 ? 1 : 0) + '%', cls: good, dir };
}
export function flag(cc) {
  if (!cc || cc.length !== 2 || !/^[A-Z]{2}$/.test(cc)) return '';
  return String.fromCodePoint(...[...cc].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}
const regionNames = typeof Intl.DisplayNames === 'function' ? new Intl.DisplayNames(undefined, { type: 'region' }) : null;
export function country(cc) {
  if (!cc || cc === '(none)') return 'Unknown';
  try { return (regionNames && regionNames.of(cc)) || cc; } catch (e) { return cc; }
}
