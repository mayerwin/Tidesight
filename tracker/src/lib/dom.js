// DOM helpers shared by plugins (bundled into each plugin at build time; unused helpers are tree-shaken).

// A short, fairly stable CSS selector: stops at the nearest id, skips generated class names.
export function selector(el) {
  const out = [];
  for (let i = 0; el && el.nodeType === 1 && i < 5; i++, el = el.parentElement) {
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) { out.unshift('#' + el.id); break; }
    let s = el.tagName.toLowerCase();
    const cls = (typeof el.className === 'string' ? el.className : '').split(/\s+/)
      .filter((c) => /^[A-Za-z_-][\w-]*$/.test(c) && !/\d{3}|^(css|sc|jsx)-/.test(c)).slice(0, 2);
    if (cls.length) s += '.' + cls.join('.');
    const p = el.parentElement;
    if (p && el !== document.body) {
      const same = [].filter.call(p.children, (c) => c.tagName === el.tagName);
      if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
    }
    out.unshift(s);
    if (el === document.body) break;
  }
  return out.join('>');
}

// The nearest ancestor (or self) matching a selector.
export function closest(el, sel) {
  return el && el.closest ? el.closest(sel) : null;
}

// Elements with the ts-no-track class (or inside one) are ignored by the behaviour plugins.
export function noTrack(el) {
  return !!closest(el, '.ts-no-track');
}

// Visible text of an element, collapsed and truncated. Never what a visitor typed: no value of a text field,
// textarea or select, nothing from editable or masked (.ts-mask, .ts-no-track) elements. With `own`, only the
// element's own text nodes (cheap on large containers).
export function text(el, max, own) {
  if (!el || el.nodeType !== 1 || el.isContentEditable || closest(el, '.ts-mask,.ts-no-track')) return;
  const tag = el.tagName;
  let t = '';
  if (tag === 'INPUT') { if (/^(submit|button|reset)$/i.test(el.type)) t = el.value; } // author-defined labels only
  else if (tag !== 'TEXTAREA' && tag !== 'SELECT') {
    t = own ? [].map.call(el.childNodes, (n) => (n.nodeType === 3 ? n.nodeValue : '')).join('') : el.textContent;
  }
  t = /\S/.test(t || '') ? t : el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title') || '';
  return String(t).replace(/\s+/g, ' ').trim().slice(0, max || 100).replace(/[\ud800-\udbff]$/, '') || undefined; // never half an emoji
}

// addEventListener with the handler wrapped by api.safe, so nothing can throw into the host page.
// Capture and passive unless options are given.
export function listen(api, target, type, fn, opts) {
  try { target.addEventListener(type, api.safe(fn), opts || { capture: true, passive: true }); } catch (e) { /* old browser */ }
}

// Run fn when the browser is idle (at the latest after `ms`).
export function idle(fn, ms) {
  const w = window;
  if (w.requestIdleCallback) w.requestIdleCallback(fn, { timeout: ms || 2000 }); else setTimeout(fn, 1);
}

// Document size [width, height] in CSS pixels.
export function pageSize() {
  const d = document, e = d.documentElement, b = d.body || e;
  return [Math.max(e.scrollWidth, b.scrollWidth), Math.max(e.scrollHeight, b.scrollHeight)];
}

// Register a plugin with the core (works whether the core is already loaded or not).
export function register(name, fn) {
  const w = window;
  (w.tidesight = w.tidesight || function () { (w.tidesight.q = w.tidesight.q || []).push(arguments); })('_plugin', name, fn);
}
