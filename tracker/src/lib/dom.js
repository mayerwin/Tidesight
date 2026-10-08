// DOM helpers shared by plugins (bundled into each plugin at build time).

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

// Visible text of an element, collapsed and truncated.
export function text(el, max) {
  const t = (el && (el.innerText || el.textContent || el.value || el.alt || el.title || el.getAttribute('aria-label'))) || '';
  return String(t).replace(/\s+/g, ' ').trim().slice(0, max || 100) || undefined;
}

// The nearest ancestor (or self) matching a selector.
export function closest(el, sel) {
  return el && el.closest ? el.closest(sel) : null;
}

// Register a plugin with the core (works whether the core is already loaded or not).
export function register(name, fn) {
  const w = window;
  (w.tidesight = w.tidesight || function () { (w.tidesight.q = w.tidesight.q || []).push(arguments); })('_plugin', name, fn);
}
