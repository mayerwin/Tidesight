// Helpers for plugins that send heavy data to POST /chunk (mouse movement, replay, DOM snapshots).

// True when the current path matches the site's exclude_paths (same glob rules as the core). The core already
// drops events on excluded pages; chunks bypass the event queue, so the plugins that send them check this.
let exPath, exVal;
export function excluded(api) {
  const p = location.pathname, g = api.cfg().exclude_paths || [];
  if (p !== exPath) {
    exPath = p;
    exVal = g.some((x) => new RegExp('^' + String(x).replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*') + '$').test(p));
  }
  return exVal;
}

// Sends the chunks of one kind. Bodies are gzip-compressed first (asynchronous). A chunk still being compressed
// when the page changes (SPA route: the page id is about to change) or is hidden or left is sent uncompressed
// at once by drain(), so it keeps the right page id and is not lost to the unload. `beacon` is a flag, or a
// function deciding per body (a byte budget: browsers allow ~64 KB of beacons in flight per page).
export function chunker(api, kind) {
  const pending = [];
  return {
    send(seq, t0, body, extra) {
      const p = [seq, t0, body, extra];
      pending.push(p);
      api.gz(body).then(api.safe((b) => {
        const i = pending.indexOf(p);
        if (i >= 0) { pending.splice(i, 1); api.chunk(kind, seq, t0, b, 0, extra); }
      }));
    },
    drain(beacon) {
      pending.splice(0).forEach((p) => api.chunk(kind, p[0], p[1], p[2], beacon && beacon.call ? beacon(p[2]) : beacon, p[3]));
    }
  };
}
