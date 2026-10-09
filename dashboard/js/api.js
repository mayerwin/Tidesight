// API client. The base is /dashboard/api/ for the private dashboard and /share/<site>/api/ for public pages.
const share = location.pathname.match(/^(.*\/share\/([^/]+))\/?/);
export const PUBLIC = !!share;
export const PUBLIC_SITE = share ? decodeURIComponent(share[2]) : null;
const base = share ? share[1] + '/api/' : new URL('api/', location.href.split('#')[0].replace(/[^/]*$/, '')).pathname;
const shareToken = new URLSearchParams(location.search).get('t');

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(endpoint, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  const headers = { Accept: 'application/json' };
  if (shareToken) headers['X-Share-Token'] = shareToken; // a header: kept out of server logs
  const res = await fetch(base + endpoint + '?' + qs.toString(), { credentials: 'same-origin', headers });
  let body = null;
  try { body = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new ApiError(res.status, (body && body.error) || res.statusText);
  return body;
}

export function exportUrl(params) {
  const qs = new URLSearchParams(params);
  return base + 'export?' + qs.toString();
}
