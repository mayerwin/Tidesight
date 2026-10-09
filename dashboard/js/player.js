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
