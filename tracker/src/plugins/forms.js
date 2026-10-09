// Form analytics (feature forms). Never reads a value except to tell whether a field is filled (1/0).
// `form` events: a = start (first field focused in a form), blur (per field: time in field, filled), submit,
// invalid (HTML5 validation, first failing validity flag), reset, abandon (page left or SPA route after a start,
// without a submit). Hidden and button-like fields are skipped; anything inside .ts-no-track is ignored.
// Fields outside a <form> belong to the closest [data-ts-form] container, else to the pseudo form "_page".
import { register, selector, listen, closest } from '../lib/dom.js';

const FLAGS = ['valueMissing', 'typeMismatch', 'patternMismatch', 'tooLong', 'tooShort', 'rangeUnderflow',
  'rangeOverflow', 'stepMismatch', 'badInput', 'customError'];

register('forms', (api) => {
  if (!api.enabled('forms')) return;
  const d = document, now = Date.now, attr = (el, a) => el.getAttribute(a);
  let forms = {}, cur = null, inv = 0;

  const ignored = (el) => !!(closest(el, '.ts-no-track') || (el.form && closest(el.form, '.ts-no-track')));
  const field = (el) => (el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) &&
    !/^(hidden|submit|button|reset|image)$/.test(el.type) && !ignored(el) ? el : null);
  // getAttribute, not form.id/.name/.action: a field named "id" or "action" would shadow those properties.
  const formId = (f) => {
    let path;
    try { path = attr(f, 'action') && new URL(attr(f, 'action'), location.href).pathname; } catch (e) { /* bad URL */ }
    if (path && api.scrub) path = api.scrub(path); // redact_paths, ;jsessionid=…
    return attr(f, 'id') || attr(f, 'name') || path || selector(f);
  };
  const fid = (el) => {
    if (el.form) return formId(el.form);
    const g = closest(el, '[data-ts-form]');
    return g ? attr(g, 'data-ts-form') || attr(g, 'id') || selector(g) : '_page';
  };
  const name = (el) => attr(el, 'name') || el.id || selector(el);
  const filled = (el) => {
    const t = el.type;
    if (t === 'checkbox' || t === 'radio') return el.checked ? 1 : 0;
    if (t === 'file') return el.files && el.files.length ? 1 : 0;
    if (t === 'select-multiple') return el.selectedIndex >= 0 ? 1 : 0;
    return el.value ? 1 : 0;
  };
  const state = (f, fld) => { // starts the form on first contact
    let s = forms[f];
    if (!s) { s = forms[f] = { t: now(), k: 0, seen: {} }; api.send('form', { a: 'start', f, fld }); }
    return s;
  };
  const touch = (s, fld) => { if (!s.seen[fld]) { s.seen[fld] = 1; s.k++; } s.last = fld; };
  const blur = () => {
    const c = cur;
    cur = null;
    const s = forms[c.f], fld = name(c.el);
    if (!s) return;
    touch(s, fld);
    api.send('form', { a: 'blur', f: c.f, fld, ft: c.el.type, ms: now() - c.t, filled: filled(c.el) });
  };

  listen(api, d, 'focusin', (e) => {
    const el = field(e.target);
    if (!el || (cur && cur.el === el)) return;
    if (cur) blur();
    const f = fid(el);
    state(f, name(el));
    cur = { el, f, t: now() };
  });
  listen(api, d, 'focusout', (e) => { if (cur && cur.el === e.target) blur(); });
  // Safari does not focus checkboxes and radios on click: a change still counts as touching the field.
  listen(api, d, 'change', (e) => {
    const el = field(e.target);
    if (el && !(cur && cur.el === el)) { const fld = name(el); touch(state(fid(el), fld), fld); }
  });
  listen(api, d, 'submit', (e) => {
    const form = e.target;
    if (!form || form.tagName !== 'FORM' || ignored(form)) return;
    const f = formId(form);
    if (cur && cur.f === f) blur(); // the field still focused when Enter was pressed
    const s = forms[f] || (forms[f] = { t: now(), k: 0, seen: {} });
    s.done = 1;
    api.send('form', { a: 'submit', f, n: s.k, ms: now() - s.t });
  });
  listen(api, d, 'invalid', (e) => { // does not bubble: capture
    const el = field(e.target);
    if (!el || inv++ >= 50) return;
    const v = el.validity || {};
    api.send('form', { a: 'invalid', f: fid(el), fld: name(el), ft: el.type, why: FLAGS.filter((k) => v[k])[0] });
  });
  listen(api, d, 'reset', (e) => {
    const form = e.target;
    if (form && form.tagName === 'FORM' && !ignored(form)) api.send('form', { a: 'reset', f: formId(form) });
  });

  const abandon = () => {
    if (cur) blur();
    Object.keys(forms).forEach((f) => {
      const s = forms[f];
      if (!s.done && !s.gone) { s.gone = 1; api.send('form', { a: 'abandon', f, n: s.k, last: s.last }); }
    });
  };
  api.on('leave', abandon);
  api.on('route', abandon);
  api.on('pageview', () => { forms = {}; cur = null; inv = 0; });
});
