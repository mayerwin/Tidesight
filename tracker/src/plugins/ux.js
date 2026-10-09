// Window and interaction signals: resize/orientation (resize), visibility and tab focus (visibility),
// copy/cut/paste (copy_paste) and text selection (text_selection). Never sends copied, pasted or selected text,
// only its length.
import { register, selector, listen, noTrack } from '../lib/dom.js';

register('ux', (api) => {
  const w = window, d = document, on = api.enabled;

  if (on('resize')) {
    let last = '', timer = 0;
    const orient = () => { const o = w.screen && w.screen.orientation; return o && o.type; };
    const size = () => [w.innerWidth, w.innerHeight, orient()];
    last = size().join();
    const check = () => {
      const s = size(), k = s.join();
      if (k !== last) { last = k; api.send('resize', { vw: s[0], vh: s[1], or: s[2] }); }
    };
    const later = () => { clearTimeout(timer); timer = setTimeout(api.safe(check), 500); };
    listen(api, w, 'resize', later, { passive: true });
    listen(api, w, 'orientationchange', later, { passive: true });
  }

  if (on('visibility')) {
    let n = 0;
    const vis = (s) => { if (n++ < 50) api.send('vis', { s }); };
    api.on('hide', () => vis('hidden')); // before the core's flush, so it travels in the same beacon
    api.on('pageview', () => { n = 0; });
    listen(api, d, 'visibilitychange', () => { if (d.visibilityState === 'visible') vis('visible'); });
    // Not capturing: element focus events do not bubble, so only the window's own blur/focus arrive here.
    listen(api, w, 'blur', () => vis('blur'), { passive: true });
    listen(api, w, 'focus', () => vis('focus'), { passive: true });
  }

  const elOf = (n) => (n && n.nodeType !== 1 ? n.parentElement : n);

  if (on('copy_paste')) {
    const clip = (e) => {
      const t = elOf(e.target);
      if (!t || noTrack(t)) return;
      let len;
      try {
        if (e.type === 'paste') len = ((e.clipboardData && e.clipboardData.getData('text')) || '').length;
        else if (/^(INPUT|TEXTAREA)$/.test(t.tagName)) len = t.selectionEnd - t.selectionStart;
        else len = String(w.getSelection()).length;
      } catch (x) { /* field type without selection */ }
      api.send(e.type, { sel: selector(t), len });
    };
    ['copy', 'cut', 'paste'].forEach((t) => listen(api, d, t, clip));
  }

  if (on('text_selection')) {
    let timer = 0, last = '';
    const check = () => {
      const s = w.getSelection(), len = s && !s.isCollapsed && s.rangeCount ? String(s).length : 0;
      if (len < 3) return;
      const el = elOf(s.getRangeAt(0).commonAncestorContainer);
      if (!el || noTrack(el)) return;
      const sel = selector(el), k = sel + ':' + len;
      if (k !== last) { last = k; api.send('select', { sel, len }); }
    };
    listen(api, d, 'selectionchange', () => { clearTimeout(timer); timer = setTimeout(api.safe(check), 1000); });
  }
});
