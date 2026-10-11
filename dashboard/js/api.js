// API client. The base is <dashboard>/api/ for the private dashboard and /share/<site>/api/ for public pages.
const share = location.pathname.match(/^(.*\/share\/([^/]+))\/?/);
export const PUBLIC = !!share;
export const PUBLIC_SITE = share ? decodeURIComponent(share[2]) : null;
const base = share ? share[1] + '/api/' : new URL('api/', location.href.split('#')[0].replace(/[^/]*$/, '')).pathname;
const shareToken = new URLSearchParams(location.search).get('t');

// The in-browser live demo (site/demo) answers the API itself, from a worker: it defines tidesightDemo before the
// dashboard loads. Read once, at load, and only as a plain object with a fetch function (never an element that
// page content might name so).
const demo = (() => {
  const d = globalThis.tidesightDemo;
  return d && Object.getPrototypeOf(d) === Object.prototype && typeof d.fetch === 'function' ? d : null;
})();
export const IN_BROWSER = !!demo;
export const transport = (url, init) => (demo ? demo.fetch(url, init) : fetch(url, init));

export class ApiError extends Error {
  constructor(status, message, body) { super(message); this.status = status; this.body = body || null; }
}

// Fired when the API says the session ended (401): the shell shows the sign-in form.
export const events = new EventTarget();

function query(params) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  return qs.toString();
}

async function request(method, endpoint, params, body) {
  const headers = { Accept: 'application/json', 'X-Tidesight': '1' };
  if (shareToken) headers['X-Share-Token'] = shareToken; // a header: kept out of server logs
  const init = { method, credentials: 'same-origin', headers };
  if (body !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  const qs = query(params);
  const res = await transport(base + endpoint + (qs ? '?' + qs : ''), init);
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) {
    const err = new ApiError(res.status, (data && data.error) || res.statusText || 'Request failed', data);
    if (res.status === 401 && !['login', 'setup', 'password_link'].includes(endpoint)) events.dispatchEvent(new CustomEvent('signedout'));
    throw err;
  }
  return data;
}

export const api = (endpoint, params = {}) => request('GET', endpoint, params);
// A file as the request's body (Plausible's export): the server keeps it and answers JSON.
export async function upload(endpoint, file, params = {}) {
  const qs = query(params);
  const res = await transport(base + endpoint + (qs ? '?' + qs : ''), { method: 'POST', credentials: 'same-origin',
    headers: { Accept: 'application/json', 'X-Tidesight': '1', 'Content-Type': file.type || 'application/zip' }, body: file });
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || res.statusText || 'Upload failed', data);
  return data;
}
export const send = (method, endpoint, body = {}, params = {}) => request(method, endpoint, params, body);

// A URL to download (report files, raw exports): plain navigation carries the session cookie.
export function fileUrl(endpoint, params) {
  return base + endpoint + '?' + query(params);
}
export function exportUrl(params) { return fileUrl('export', params); }

// Where the tracker is served from: [server] public_url, else this dashboard's own host and folder.
export function collectorBase(publicUrl) {
  if (publicUrl) return publicUrl.replace(/\/+$/, '') + '/';
  const path = location.pathname.replace(/\/dashboard\/.*$/, '/').replace(/\/share\/.*$/, '/');
  return location.origin + path;
}
