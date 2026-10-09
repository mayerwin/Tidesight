// Lazily loads the vendored rrweb player (session replay, DOM snapshots).
let loading = null;

export function loadPlayer() {
  if (loading) return loading;
  loading = new Promise((resolve, reject) => {
    const base = new URL('../vendor/rrweb-player/', import.meta.url);
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = new URL('rrweb-player.css', base).href;
    document.head.appendChild(css);
    const script = document.createElement('script');
    script.src = new URL('rrweb-player.js', base).href;
    script.onload = () => {
      const mod = window.rrwebPlayer;
      const Player = mod && (mod.default || mod);
      if (typeof Player === 'function') resolve(Player); else reject(new Error('rrweb player failed to load'));
    };
    script.onerror = () => reject(new Error('rrweb player failed to load'));
    document.head.appendChild(script);
  });
  return loading;
}

// Resource hints make a browser contact a host before any Content Security Policy applies (DNS lookups,
// preconnections). A recording is anyone's data: its <link> elements lose those rel values before the player
// rebuilds the page (in full snapshots, added nodes and attribute changes).
const HINT = /\b(?:dns-prefetch|preconnect|prefetch|preload|prerender|modulepreload)\b/i;
const HINTS = /\b(?:dns-prefetch|preconnect|prefetch|preload|prerender|modulepreload)\b/gi;
export function sanitizeEvents(events) {
  const clean = (attrs) => {
    if (attrs && typeof attrs.rel === 'string' && HINT.test(attrs.rel)) attrs.rel = attrs.rel.replace(HINTS, ' ').trim();
  };
  const walk = (root) => {
    const stack = [root];
    while (stack.length) {
      const n = stack.pop();
      if (!n || typeof n !== 'object') continue;
      clean(n.attributes);
      if (Array.isArray(n.childNodes)) for (let i = 0; i < n.childNodes.length; i++) stack.push(n.childNodes[i]);
    }
  };
  for (const e of Array.isArray(events) ? events : []) {
    const d = e && e.data;
    if (!d) continue;
    if (e.type === 2) walk(d.node);
    else if (e.type === 3 && d.source === 0) {
      (d.adds || []).forEach((a) => a && walk(a.node));
      (d.attributes || []).forEach((a) => a && clean(a.attributes));
    }
  }
  return events;
}
