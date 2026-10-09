// DOM helpers shared by every module. Visitors' data reaches the page only through textContent and attributes
// (never innerHTML); icons are static markup from this file.
const SVGNS = 'http://www.w3.org/2000/svg';

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k === 'dataset' && typeof v === 'object') Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
  return node;
}

export function s(tag, attrs = {}) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, v);
  return node;
}

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const P = (d, extra = '') => '<path d="' + d + '" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"' + extra + '/>';
export const icons = {
  table: P('M3 5h18v14H3zM3 10h18M3 15h18M9 5v14'),
  chart: P('M4 19V5M4 19h16M7 15l4-5 3 3 5-7'),
  close: P('M6 6l12 12M18 6L6 18'),
  download: P('M12 4v11m0 0l-4-4m4 4l4-4M5 19h14'),
  filter: P('M4 5h16l-6 7.5V19l-4 1.5v-8z'),
  plus: P('M12 5v14M5 12h14'),
  minus: P('M5 12h14'),
  check: P('M5 12.5l4.5 4.5L19 7.5'),
  down: P('M6 9l6 6 6-6'),
  up: P('M6 15l6-6 6 6'),
  left: P('M15 6l-6 6 6 6'),
  right: P('M9 6l6 6-6 6'),
  calendar: P('M4 6h16v14H4zM4 10h16M8 3v4M16 3v4'),
  search: P('M10.5 17a6.5 6.5 0 110-13 6.5 6.5 0 010 13zM15.5 15.5L21 21'),
  copy: P('M8 8h11v12H8zM5 16V4h11'),
  external: P('M14 4h6v6M20 4l-9 9M18 14v5H5V6h5'),
  gear: P('M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.6 1.6 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.6 1.6 0 00-1.8-.3 1.6 1.6 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.6 1.6 0 00-1-1.5 1.6 1.6 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.6 1.6 0 00.3-1.8 1.6 1.6 0 00-1.5-1H3a2 2 0 110-4h.1a1.6 1.6 0 001.5-1 1.6 1.6 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.6 1.6 0 001.8.3H9a1.6 1.6 0 001-1.5V3a2 2 0 114 0v.1a1.6 1.6 0 001 1.5 1.6 1.6 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.6 1.6 0 00-.3 1.8V9a1.6 1.6 0 001.5 1H21a2 2 0 110 4h-.1a1.6 1.6 0 00-1.5 1z'),
  trash: P('M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3'),
  dots: '<circle cx="5" cy="12" r="1.6" fill="currentColor"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><circle cx="19" cy="12" r="1.6" fill="currentColor"/>',
  segment: P('M4 6h16M7 12h10M10 18h4'),
  bot: P('M7 9h10v9H7zM12 9V5M10 13h.01M14 13h.01M4 13v2M20 13v2M12 4.5h.01'),
  user: P('M12 12a4 4 0 100-8 4 4 0 000 8zM4 21a8 8 0 0116 0'),
  logout: P('M15 4h4v16h-4M10 8l-4 4 4 4M6 12h11'),
  theme: P('M12 3a9 9 0 109 9 7 7 0 01-9-9z'),
  keyboard: P('M3 6h18v12H3zM7 10h.01M11 10h.01M15 10h.01M7 14h10'),
  info: P('M12 21a9 9 0 100-18 9 9 0 000 18zM12 11v6M12 7.5v.5'),
  alert: P('M12 3l10 18H2zM12 10v5M12 18v.5'),
  link: P('M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1'),
  arrow: P('M5 12h14M13 6l6 6-6 6'),
  columns: P('M4 5h16v14H4zM9.5 5v14M14.5 5v14'),
  compare: P('M7 4v16M17 4v16M3 8h8M13 16h8'),
  note: P('M5 4h14v12l-5 4H5zM14 20v-4h5'),
  eye: P('M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 100-6 3 3 0 000 6z'),
  heat: P('M12 3c4 5 6 8 6 11a6 6 0 01-12 0c0-3 2-6 6-11z'),
  play: P('M7 5l12 7-12 7z'),
  globe: P('M12 21a9 9 0 100-18 9 9 0 000 18zM3 12h18M12 3c2.5 2.7 3.5 5.7 3.5 9s-1 6.3-3.5 9c-2.5-2.7-3.5-5.7-3.5-9s1-6.3 3.5-9z'),
  layers: P('M12 3l9 5-9 5-9-5zM3 13l9 5 9-5'),
  pulse: P('M3 12h4l3-7 4 14 3-7h4'),
  target: P('M12 21a9 9 0 100-18 9 9 0 000 18zM12 16a4 4 0 100-8 4 4 0 000 8zM12 12h.01'),
  undo: P('M9 14L4 9l5-5M4 9h10a6 6 0 010 12h-3'),
  grip: '<circle cx="9" cy="6" r="1.4" fill="currentColor"/><circle cx="15" cy="6" r="1.4" fill="currentColor"/><circle cx="9" cy="12" r="1.4" fill="currentColor"/><circle cx="15" cy="12" r="1.4" fill="currentColor"/><circle cx="9" cy="18" r="1.4" fill="currentColor"/><circle cx="15" cy="18" r="1.4" fill="currentColor"/>',
};
export function icon(name, cls) {
  const svgEl = s('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true', class: cls || null, focusable: 'false' });
  svgEl.innerHTML = icons[name] || ''; // static markup from this file, never user data
  return svgEl;
}

// ---- tooltip -----------------------------------------------------------------------
let tipEl = null;
export const tip = {
  show(x, y, title, rows, foot) {
    if (!tipEl) { tipEl = el('div', { class: 'tooltip', role: 'tooltip' }); document.body.appendChild(tipEl); }
    tipEl.replaceChildren();
    if (title) tipEl.appendChild(el('div', { class: 'tt-title', text: title }));
    for (const r of rows || []) {
      const row = el('div', { class: 'tt-row' + (r.muted ? ' muted' : '') });
      if (r.color) row.appendChild(el('i', { class: 'key' + (r.dashed ? ' dashed' : ''), style: { background: r.dashed ? 'none' : r.color, borderColor: r.color } }));
      row.appendChild(el('b', { text: r.value }));
      if (r.label) row.appendChild(el('span', { text: r.label }));
      if (r.delta) row.appendChild(el('em', { class: 'delta ' + r.delta.cls, text: r.delta.text }));
      tipEl.appendChild(row);
    }
    if (foot) tipEl.appendChild(el('div', { class: 'tt-foot', text: foot }));
    tipEl.hidden = false;
    const w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    const left = Math.max(8, Math.min(x, window.innerWidth - w - 24)), top = Math.max(8, Math.min(y, window.innerHeight - h - 24));
    tipEl.style.left = left + 'px';
    tipEl.style.top = top + 'px';
  },
  hide() { if (tipEl) tipEl.hidden = true; },
};

// A short explanation on hover/focus for any element (title attributes are slow and invisible on touch).
export function hint(node, text) {
  if (!text) return node;
  // An element without a name of its own (an icon) is named by the hint; others keep their visible name.
  if (!node.getAttribute('aria-label') && !node.textContent.trim()) node.setAttribute('aria-label', text);
  else node.setAttribute('aria-description', text);
  const show = () => { const r = node.getBoundingClientRect(); tip.show(r.left, r.bottom + 4, null, [], text); };
  node.addEventListener('pointerenter', show);
  node.addEventListener('pointerleave', () => tip.hide());
  node.addEventListener('focus', show);
  node.addEventListener('blur', () => tip.hide());
  return node;
}
