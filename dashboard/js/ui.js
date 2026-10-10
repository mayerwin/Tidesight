// UI building blocks: popovers, menus, dialogs, toasts, copy and download, form controls.
import { IN_BROWSER, transport } from './api.js';
import { el, icon, tip } from './dom.js';

// ---- popovers (one open at a time; a dialog may hold its own) -----------------------------------------
let current = null;
export function closePopover() {
  if (current) { const c = current; current = null; c.remove(); c._onClose && c._onClose(); }
}
document.addEventListener('pointerdown', (e) => {
  if (!current) return;
  if (current.contains(e.target) || (current._anchor && current._anchor.contains(e.target))) return;
  if (e.target.closest && e.target.closest('.dialog, .toast')) return;
  closePopover();
}, true);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && current) { const a = current._anchor; closePopover(); a && a.focus && a.focus(); e.stopPropagation(); }
}, true);
window.addEventListener('resize', () => closePopover());
// Anchors in the sticky report bar stay put while the page scrolls: their panel follows.
window.addEventListener('scroll', () => { if (current && current.reposition) current.reposition(); }, { passive: true });

// Open `content` under `anchor` (aligned to its left, or right with align: 'end'). Returns the panel.
export function popover(anchor, content, { align = 'start', className = '', onClose, width } = {}) {
  if (current && current._anchor === anchor) { closePopover(); return null; }
  closePopover();
  tip.hide();
  const panel = el('div', { class: 'popover ' + className, role: 'dialog' }, content);
  if (width) panel.style.width = width + 'px';
  panel._anchor = anchor;
  panel._onClose = () => { anchor.setAttribute && anchor.setAttribute('aria-expanded', 'false'); onClose && onClose(); };
  document.body.appendChild(panel);
  anchor.setAttribute && anchor.setAttribute('aria-expanded', 'true');
  place(panel, anchor, align);
  panel.reposition = () => place(panel, anchor, align);
  current = panel;
  const first = panel.querySelector('[autofocus], input, select, button, [tabindex="0"]');
  if (first) setTimeout(() => first.focus(), 0);
  return panel;
}
export function place(panel, anchor, align = 'start') {
  const r = anchor.getBoundingClientRect();
  const w = panel.offsetWidth, h = panel.offsetHeight, vw = window.innerWidth, vh = window.innerHeight;
  let left = align === 'end' ? r.right - w : r.left;
  left = Math.max(8, Math.min(left, vw - w - 8));
  let top = r.bottom + 6;
  if (top + h > vh - 8 && r.top - h - 6 > 8) top = r.top - h - 6;
  panel.style.left = left + window.scrollX + 'px';
  panel.style.top = Math.max(8, top) + window.scrollY + 'px';
}

// A menu of actions: items [{label, hint?, icon?, onClick, checked?, danger?, disabled?} | 'sep' | {heading}]
export function menu(anchor, items, opts = {}) {
  const list = el('div', { class: 'menu-list', role: 'menu' });
  const buttons = [];
  for (const it of items) {
    if (!it) continue;
    if (it === 'sep') { list.appendChild(el('div', { class: 'menu-sep', role: 'separator' })); continue; }
    if (it.heading) { list.appendChild(el('div', { class: 'menu-heading', text: it.heading })); continue; }
    const b = el('button', { type: 'button', role: it.checked !== undefined ? 'menuitemradio' : 'menuitem', class: 'menu-item' + (it.danger ? ' danger' : ''),
      'aria-checked': it.checked !== undefined ? String(!!it.checked) : null, disabled: it.disabled || null });
    b.appendChild(el('span', { class: 'mi-icon' }, it.checked ? icon('check') : it.icon ? icon(it.icon) : null));
    const text = el('span', { class: 'mi-text' }, el('span', { class: 'mi-label', text: it.label }));
    if (it.hint) text.appendChild(el('span', { class: 'mi-hint', text: it.hint }));
    b.appendChild(text);
    if (it.kbd) b.appendChild(el('kbd', { text: it.kbd }));
    b.addEventListener('click', () => { closePopover(); it.onClick && it.onClick(); });
    buttons.push(b);
    list.appendChild(b);
  }
  list.addEventListener('keydown', (e) => {
    const i = buttons.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { (buttons[i + 1] || buttons[0]).focus(); e.preventDefault(); }
    if (e.key === 'ArrowUp') { (buttons[i - 1] || buttons[buttons.length - 1]).focus(); e.preventDefault(); }
  });
  return popover(anchor, list, Object.assign({ className: 'menu-pop' }, opts));
}

// ---- dialogs --------------------------------------------------------------------------------------------
const openDialogs = [];
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && openDialogs.length && !current) { openDialogs[openDialogs.length - 1].close(); e.preventDefault(); }
});
// Navigating away closes what is open (the browser's Back button included).
export function closeDialogs() {
  while (openDialogs.length) openDialogs[openDialogs.length - 1].close();
}
export function dialog({ title, body, actions = [], width = 560, className = '', onClose, subtitle } = {}) {
  closePopover();
  const back = el('div', { class: 'dialog-back' });
  const box = el('div', { class: 'dialog ' + className, role: 'dialog', 'aria-modal': 'true', 'aria-label': title || 'Dialog',
    style: { maxWidth: width + 'px' } });
  const head = el('div', { class: 'dialog-head' }, el('div', {}, el('h2', { text: title || '' }),
    subtitle ? el('p', { class: 'dialog-sub', text: subtitle }) : null));
  const x = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Close' }, icon('close'));
  head.appendChild(x);
  const content = el('div', { class: 'dialog-body' }, body);
  const foot = el('div', { class: 'dialog-foot' }, ...actions);
  box.append(head, content);
  if (actions.length) box.appendChild(foot);
  back.appendChild(box);
  document.body.appendChild(back);
  document.body.classList.add('modal-open');
  const previous = document.activeElement;
  const handle = {
    el: box, body: content, foot,
    close() {
      back.remove();
      const i = openDialogs.indexOf(handle);
      if (i >= 0) openDialogs.splice(i, 1);
      if (!openDialogs.length) document.body.classList.remove('modal-open');
      onClose && onClose();
      if (previous && previous.focus) previous.focus();
    },
  };
  x.addEventListener('click', () => handle.close());
  back.addEventListener('pointerdown', (e) => { if (e.target === back) handle.close(); });
  box.addEventListener('keydown', (e) => { // keep the focus inside
    if (e.key !== 'Tab') return;
    const f = [...box.querySelectorAll('button, [href], input, select, textarea, [tabindex="0"]')].filter((n) => !n.disabled && n.offsetParent);
    if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) { f[f.length - 1].focus(); e.preventDefault(); }
    else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { f[0].focus(); e.preventDefault(); }
  });
  openDialogs.push(handle);
  setTimeout(() => { const f = box.querySelector('[autofocus], input:not([type=checkbox]), select, textarea') || x; f.focus(); }, 0);
  return handle;
}

// Ask before something destructive; `confirmText` must be typed when given.
export function confirm({ title, message, confirmLabel = 'Confirm', danger = false, confirmText, extra } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const input = confirmText ? el('input', { class: 'input wide mono', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Type ' + confirmText + ' to confirm' }) : null;
    const ok = el('button', { type: 'button', class: 'btn ' + (danger ? 'danger' : 'primary'), text: confirmLabel, disabled: confirmText ? true : null });
    const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
    const body = el('div', { class: 'stack' }, typeof message === 'string' ? el('p', { text: message }) : message, extra || null,
      input ? el('label', { class: 'field' }, el('span', {}, 'Type ', el('code', { text: confirmText }), ' to confirm'), input) : null);
    const d = dialog({ title, body, actions: [cancel, ok], width: 480, onClose: () => { if (!done) resolve(false); } });
    if (input) input.addEventListener('input', () => { ok.disabled = input.value.trim() !== confirmText; });
    cancel.addEventListener('click', () => d.close());
    ok.addEventListener('click', () => { done = true; d.close(); resolve(true); });
  });
}

// ---- toasts -----------------------------------------------------------------------------------------------
let toastBox = null;
export function toast(message, { kind = 'info', timeout = 3500, action } = {}) {
  if (!toastBox) { toastBox = el('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.appendChild(toastBox); }
  const t = el('div', { class: 'toast ' + kind }, icon(kind === 'error' ? 'alert' : kind === 'success' ? 'check' : 'info'), el('span', { text: message }));
  if (action) {
    const b = el('button', { type: 'button', class: 'link-btn', text: action.label });
    b.addEventListener('click', () => { action.onClick(); t.remove(); });
    t.appendChild(b);
  }
  toastBox.appendChild(t);
  setTimeout(() => { t.classList.add('leaving'); setTimeout(() => t.remove(), 300); }, timeout);
}

// ---- clipboard & files --------------------------------------------------------------------------------------
export async function copyText(text, label = 'Copied to the clipboard') {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = el('textarea', { style: { position: 'fixed', top: '-1000px', opacity: '0' } });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
    ta.remove();
    if (!ok) { toast('Copying is blocked by the browser: select the text and copy it yourself.', { kind: 'error' }); return false; }
  }
  if (label) toast(label, { kind: 'success', timeout: 2000 });
  return true;
}
export function download(filename, content, type = 'text/plain;charset=utf-8') {
  const blob = content instanceof Blob ? content : new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: filename, style: { display: 'none' } });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
}
// Navigate to a server-made file (the response is an attachment: the page stays).
export async function downloadUrl(url) {
  let name = '';
  if (IN_BROWSER) { // the in-browser demo answers downloads too: save its answer from memory
    try {
      const res = await transport(url, { headers: { 'X-Tidesight': '1' } });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
      name = (/filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '') || [])[1] || '';
      url = URL.createObjectURL(await res.blob());
    } catch (e) {
      toast(e.message, { kind: 'error' });
      return;
    }
  }
  const a = el('a', { href: url, download: name, style: { display: 'none' } });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { a.remove(); if (name) URL.revokeObjectURL(url); }, 1000);
}

// Rows of plain values as CSV/TSV/JSON (exports of tables already on screen). Formulas are neutralised.
const FORMULA = /^[=+\-@\t\r]/;
function cell(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'string' && FORMULA.test(v)) return "'" + v;
  return String(v);
}
export function toDelimited(headers, rows, sep) {
  const quote = (v) => {
    const s = cell(v);
    if (sep === '\t') return s.replace(/[\t\r\n]+/g, ' ');
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  return [headers.map(quote).join(sep)].concat(rows.map((r) => r.map(quote).join(sep))).join('\r\n') + '\r\n';
}
export function exportRows(name, headers, rows, format) {
  const stamp = name.replace(/[^A-Za-z0-9_.-]+/g, '-').replace(/^-+|-+$/g, '') || 'tidesight';
  if (format === 'json') {
    download(stamp + '.json', JSON.stringify(rows.map((r) => Object.fromEntries(headers.map((h, i) => [h, r[i]]))), null, 1), 'application/json');
  } else if (format === 'copy') {
    copyText(toDelimited(headers, rows, '\t'), 'Copied ' + rows.length + ' rows: paste them into a spreadsheet');
  } else {
    const sep = format === 'tsv' ? '\t' : ',';
    download(stamp + '.' + (format === 'tsv' ? 'tsv' : 'csv'), '﻿' + toDelimited(headers, rows, sep),
      format === 'tsv' ? 'text/tab-separated-values;charset=utf-8' : 'text/csv;charset=utf-8');
  }
}

// ---- controls ---------------------------------------------------------------------------------------------------
export function segmented(options, value, onChange, { label, small = false } = {}) {
  const box = el('div', { class: 'segmented' + (small ? ' small' : ''), role: 'radiogroup', 'aria-label': label || null });
  for (const o of options) {
    const b = el('button', { type: 'button', role: 'radio', 'aria-checked': String(o.id === value), text: o.label,
      disabled: o.disabled || null, title: o.title || null });
    b.addEventListener('click', () => {
      if (o.id === value) return;
      value = o.id;
      box.querySelectorAll('button').forEach((x) => x.setAttribute('aria-checked', String(x === b)));
      onChange(o.id);
    });
    box.appendChild(b);
  }
  return box;
}

export function toggle(checked, onChange, { label, id, disabled } = {}) {
  const input = el('input', { type: 'checkbox', role: 'switch', class: 'switch', id: id || null, disabled: disabled || null });
  input.checked = !!checked;
  input.addEventListener('change', () => onChange(input.checked));
  if (!label) return input;
  return el('label', { class: 'switch-row' }, input, el('span', { text: label }));
}

export function field(label, control, help, { wide = false } = {}) {
  return el('label', { class: 'field' + (wide ? ' wide' : '') }, el('span', { class: 'field-label', text: label }), control,
    help ? el('span', { class: 'field-help', text: help }) : null);
}

export function spinner() { return el('span', { class: 'spinner', 'aria-hidden': 'true' }); }

export function debounce(fn, ms = 250) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// A list of short texts edited as chips (domains, paths): Enter or comma adds, Backspace removes the last.
export function tagInput(values, { placeholder = '', validate, normalize, onChange, disabled } = {}) {
  let items = [...values];
  const wrap = el('div', { class: 'tag-input' + (disabled ? ' disabled' : '') });
  const input = el('input', { class: 'tag-entry', placeholder, disabled: disabled || null, spellcheck: 'false', autocomplete: 'off' });
  const error = el('div', { class: 'field-error', hidden: true });
  const draw = () => {
    wrap.querySelectorAll('.tag').forEach((n) => n.remove());
    items.forEach((v, i) => {
      const x = el('button', { type: 'button', 'aria-label': 'Remove ' + v, disabled: disabled || null }, icon('close'));
      x.addEventListener('click', () => { items.splice(i, 1); draw(); onChange && onChange(items.slice()); });
      wrap.insertBefore(el('span', { class: 'tag' }, el('span', { text: v }), x), input);
    });
  };
  const add = () => {
    const raw = input.value.split(/[\s,]+/).map((v) => v.trim()).filter(Boolean);
    if (!raw.length) return true;
    for (const r of raw) {
      const v = normalize ? normalize(r) : r;
      const problem = validate ? validate(v) : null;
      if (problem) { error.textContent = problem; error.hidden = false; return false; }
      if (!items.includes(v)) items.push(v);
    }
    error.hidden = true;
    input.value = '';
    draw();
    onChange && onChange(items.slice());
    return true;
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(); }
    else if (e.key === 'Backspace' && !input.value && items.length) { items.pop(); draw(); onChange && onChange(items.slice()); }
  });
  input.addEventListener('blur', () => { if (input.value.trim()) add(); });
  wrap.addEventListener('click', (e) => { if (e.target === wrap) input.focus(); });
  wrap.appendChild(input);
  draw();
  const box = el('div', { class: 'tag-field' }, wrap, error);
  box.values = () => { if (input.value.trim()) add(); return items.slice(); };
  box.set = (v) => { items = [...v]; draw(); };
  return box;
}
