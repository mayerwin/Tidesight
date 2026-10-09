/**
 * The Tidesight collector, runtime-neutral: a port of server/python/tidesight/collect.py written against the
 * Web Fetch API (Request → Response) and an abstract store (see records.mjs). The Node connector and the
 * Cloudflare Worker both run this file.
 *
 *   POST /collect      batch of events (JSON envelope, optionally gzip)
 *   POST /chunk        heavy data chunk (replay, mouse movement), stored unparsed
 *   GET  /p.gif        pixel hit
 *   GET  /config.js    site configuration for the tracker (also /config.json)
 *   GET  /health       liveness and storage check
 *   GET  /ts.js, /ts/* tracker files, when the platform provides them (readStatic)
 *
 * The request path validates, bounds and appends. It never enriches, never calls the network, and stores
 * malformed or unknown-site hits flagged instead of dropping them, within the allowances of docs/configuration.md
 * ("Limits and abuse protection"), which the store checks atomically with the write. Behaviour, status codes,
 * headers and the rows written match the Python collector; docs/protocol.md is the contract.
 *
 * The request is seen the way a WSGI application sees it: PATH_INFO is the percent-decoded path, QUERY_STRING
 * the raw query, header names are lower-cased with "_" read as "-", REMOTE_ADDR is the peer address supplied by
 * the platform adapter. Adapters may pass `extras` to keep raw details the Fetch API normalises away:
 *   remoteAddr   REMOTE_ADDR (Node: the socket peer; Worker: CF-Connecting-IP)
 *   rawTarget    the request target as received ("/collect?site=x"), instead of request.url
 *   rawHeaders   [name, value, name, value, ...] in arrival order, instead of request.headers
 *   method       the request method, when the Request had to be built with another one
 *   scheme       wsgi.url_scheme ("https" when a TLS proxy is in front)
 *   server       extra keys for the stored `server` object (server_protocol, server_port, ...)
 *   basePath     a mount prefix to strip (SCRIPT_NAME)
 */

import { loads, dumpsCompact, dumps, isPyInt, PyFloat } from "./pyjson.mjs";
import {
  parseQs, pyLen, pyRstrip, pySlice, pyStrip, pyStrOf, pyIntOf, originOf, unquoteLatin1, utcDay,
} from "./pycompat.mjs";
import {
  anonymizeIp, anonymizeHeaders, hashIp, inNetworks, parseIPv4, parseIPv6Scoped, v4String, v6String,
} from "./anonymize.mjs";
import { ErrorLog } from "./errorlog.mjs";
import { gunzipBounded, gzipCompress, isGzip } from "./gzip.mjs";
import { redactUrl } from "./redact.mjs";
import {
  ChunkRecord, HitRecord, RATE_LIMITED, RequestRecord, StorageError, TOO_LARGE, budget, isFlagged, makeLimits,
} from "./records.mjs";
import { VERSION } from "./version.mjs";

export const GIF = Uint8Array.from(atob("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="), (c) => c.charCodeAt(0));

/** The bytes of a Response's body, attached so that a platform adapter can send them without a copy. */
export const RAW_BODY = Symbol.for("tidesight.rawBody");

const ACCEPT_CH = "Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform, Sec-CH-UA-Platform-Version, Sec-CH-UA-Model, " +
  "Sec-CH-UA-Full-Version-List, Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Form-Factors, " +
  "Sec-CH-UA-WoW64, Viewport-Width, DPR, Device-Memory, RTT, Downlink, ECT, Sec-CH-Prefers-Color-Scheme, " +
  "Sec-CH-Prefers-Reduced-Motion";

const CHUNK_KINDS = new Set(["replay", "mouse", "snapshot"]);
// The feature a site must have on to accept a chunk kind (others are stored flagged, "feature_off").
const CHUNK_FEATURES = { replay: "replay", mouse: "mouse_moves", snapshot: "dom_snapshots" };
const DISK_CHECK_MS = 10000;
const MAX_HEADER_VALUE = 8192;
const MAX_KEPT_COOKIES = 20;
const HIT_COST = 256; // bytes charged per stored hit (a row costs about that much, however small its JSON)
const MAX_JSON_DEPTH = 64; // objects and arrays nested deeper than this: an unreadable body (bad_json)
const MAX_DECLARED_LENGTH = 2 ** 53 - 1; // larger Content-Length values are recorded as this
const BODY_BLOCK = 64 * 1024; // a body buffer starts at this size and doubles as bytes arrive
const BUSY_RETRY_AFTER = "10"; // seconds, when the in-flight body memory budget is used up
const REDACTED = "[redacted]";
const ALWAYS_SECRET = new Set(["authorization", "proxy-authorization"]);

const ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
// Python's \s (str.isspace) inside the class.
const ORIGIN_RE = /^(?:null|[A-Za-z][A-Za-z0-9+.-]*:\/\/[^\t\n\x0b\x0c\r\x1c-\x20\x85\xa0\u{1680}\u{2000}-\u{200a}\u{2028}\u{2029}\u{202f}\u{205f}\u{3000}/?#,]+)$/u;
const COOKIE_DOMAIN_RE = /^\.?[A-Za-z0-9.-]+$/;
const STATIC_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const STATIC_TYPES = {
  ".js": "application/javascript; charset=utf-8", ".map": "application/json; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8",
};

const UTF8_STRICT = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
const UTF8 = new TextEncoder();
const EMPTY = new Uint8Array(0);

function rejectConstant(name) {
  throw new Error(`invalid JSON constant ${name}`);
}

/**
 * collect._finite_float: a JSON number with a fraction or exponent as a float; one too large for a float (1e999)
 * makes the body unreadable, as NaN does (it would be stored as Infinity, which is not JSON).
 */
function finiteFloat(text) {
  const value = Number(text);
  if (!Number.isFinite(value)) throw new Error("number out of range");
  return new PyFloat(value);
}

// collect._V4_PORT_RE and _V6_PORT_RE (Python's \d is any Unicode decimal digit).
const V4_PORT_RE = /^(\p{Nd}{1,3}(?:\.\p{Nd}{1,3}){3}):\p{Nd}{1,5}$/u;
const V6_PORT_RE = /^\[([0-9A-Fa-f:.]+)\](?::\p{Nd}{1,5})?$/u;

/**
 * collect.client_key: the rate-limit identity of a client. An IPv4 address as itself, an IPv4-mapped IPv6 address
 * as its IPv4 address, any other IPv6 address as its network for ipv6Prefix (clamped to 16..128: one host can use
 * a whole /64), written as Python's ipaddress writes it ("2001:db8:1:2::/64"); a port ("1.2.3.4:5678",
 * "[2001:db8::1]:443") is ignored. Anything that is not an address (a custom key header) is kept as its first
 * 64 characters.
 */
export function clientKey(value, ipv6Prefix = 64) {
  if (!value) return null;
  const addr = addressOf(value);
  if (addr === null) return pySlice(value, 64);
  if (addr.v4 !== undefined) return v4String(addr.v4);
  const v6 = addr.v6;
  if ((v6 >> 32n) === 0xffffn) return v4String(Number(v6 & 0xffffffffn)); // ipv4_mapped
  const n = Math.trunc(Number(ipv6Prefix));
  const prefix = Number.isFinite(n) ? Math.max(16, Math.min(128, n)) : 64;
  const hostBits = BigInt(128 - prefix);
  return `${v6String((v6 >> hostBits) << hostBits)}/${prefix}`;
}

/**
 * collect._address: the IP address in a value, ignoring a port ("1.2.3.4:5678", "[2001:db8::1]:443") —
 * {v4: number} or {v6: BigInt} — or null when it is not one.
 */
export function addressOf(value) {
  const v = pyStrip(value);
  const m = V6_PORT_RE.exec(v) || V4_PORT_RE.exec(v);
  const s = m ? m[1] : v;
  const v4 = parseIPv4(s); // ipaddress.ip_address: IPv4 first, then IPv6 (scope allowed)
  if (v4 !== null) return { v4 };
  const v6 = parseIPv6Scoped(s);
  return v6 === null ? null : { v6 };
}

/**
 * collect._rate_key: the client's identity for the allowances. The connecting address (REMOTE_ADDR), or the
 * address the rate_limit_key header gives, believed only when the connection comes from a listed proxy
 * (rate_limit_proxies) and the value is an IP address: anyone else could invent a new identity per request, or
 * borrow a victim's. The header's last value is the one the nearest proxy appended (earlier ones are the client's
 * say). `fields` are the raw header fields, [lower-case name, value] in arrival order: only fields named exactly
 * like the configured header count, and an alias of it with "_" for "-" (which WSGI servers merge into the same
 * variable, but a proxy does not replace) makes the header untrustworthy: the remote address is used instead.
 */
export function clientKeyFor(cfg, remoteAddr, fields) {
  const remote = remoteAddr || null;
  const keyName = String(cfg.server.rate_limit_key);
  let value = null;
  if (keyName !== "remote_addr" && remote && inNetworks(remote, cfg.proxyNetworks())) {
    const want = keyName.toLowerCase();
    const alias = want.replace(/_/g, "-");
    let last = null;
    let aliased = false;
    for (const [name, v] of fields) {
      if (name === want) last = v;
      else if (name.replace(/_/g, "-") === alias) aliased = true;
    }
    // WSGI keeps Content-Type and Content-Length out of the HTTP_* variables.
    if (last && !aliased && alias !== "content-type" && alias !== "content-length") {
      const candidate = pySlice(pyStrip(last.slice(last.lastIndexOf(",") + 1)), 64);
      if (addressOf(candidate) !== null) value = candidate;
    }
  }
  return clientKey(value || remote, cfg.server.rate_limit_ipv6_prefix);
}

/** collect._deeper_than: whether objects and arrays nest more than `limit` levels (the top level is 1). */
function deeperThan(obj, limit) {
  const stack = [[obj, 1]];
  while (stack.length) {
    const [value, depth] = stack.pop();
    if (value instanceof Map || Array.isArray(value)) {
      if (depth > limit) return true;
      for (const v of value instanceof Map ? value.values() : value) stack.push([v, depth + 1]);
    }
  }
  return false;
}

/** collect._kb: kilobytes, rounded up (a request of 1 byte uses 1 KB of a budget). */
export function kb(n) {
  return Math.max(1, Math.ceil(Number(n) / 1024));
}

/**
 * collect.header_bytes: what a request's stored headers and kept cookies weigh (names and values, in characters
 * as Python counts them), charged to its client. headers: Map name → value; cookies: Map {kept, others} or null.
 */
export function headerBytes(headers, cookies) {
  let n = 0;
  for (const [name, value] of headers) n += pyLen(name) + pyLen(String(value));
  const kept = cookies ? cookies.get("kept") : null;
  if (kept) for (const [name, value] of kept) n += pyLen(name) + pyLen(String(value));
  return n;
}

const consoleLog = {
  info: (msg) => console.error(msg),
  warning: (msg) => console.error(msg),
  error: (msg, exc) => console.error(msg, exc ?? ""),
};

// ---------------------------------------------------------------------------
// Request view (what a WSGI environ would hold)
// ---------------------------------------------------------------------------

class Environ {
  constructor(request, extras) {
    this.method = String(extras.method || request.method || "GET").toUpperCase();
    let target = extras.rawTarget;
    let url = null;
    if (target == null) {
      url = new URL(request.url);
      target = url.pathname + url.search;
    }
    const q = target.indexOf("?");
    const rawPath = q < 0 ? target : target.slice(0, q);
    this.query = q < 0 ? "" : target.slice(q + 1);
    let path = unquoteLatin1(rawPath);
    this.scriptName = "";
    const base = extras.basePath;
    if (base && (path === base || path.startsWith(base + "/"))) {
      this.scriptName = base;
      path = path.slice(base.length);
    }
    this.path = path;
    this.headers = new Map(); // as WSGI merges them: lower case, "_" read as "-", repeated fields joined
    this.fields = []; // the raw fields, [lower-case name, value], for the client key header
    const add = (name, value) => {
      const raw = String(name).toLowerCase();
      this.fields.push([raw, String(value)]);
      const key = raw.replace(/_/g, "-");
      const prev = this.headers.get(key);
      this.headers.set(key, prev === undefined ? String(value) : prev + (key === "cookie" ? "; " : ", ") + value);
    };
    if (extras.rawHeaders) {
      for (let i = 0; i + 1 < extras.rawHeaders.length; i += 2) add(extras.rawHeaders[i], extras.rawHeaders[i + 1]);
    } else {
      for (const [name, value] of request.headers) add(name, value);
    }
    this.remoteAddr = extras.remoteAddr || null;
    if (extras.scheme) this.scheme = extras.scheme;
    else this.scheme = (url || new URL(request.url)).protocol.replace(/:$/, "");
    this.server = extras.server || {};
    this.body = request.body || null;
    this.bodyReserved = 0; // bytes of the in-flight body budget this request holds
    this.clientId = null; // the hashed allowance identity, once computed
  }

  header(name) {
    return this.headers.get(name);
  }
}

// ---------------------------------------------------------------------------
// Responses
// ---------------------------------------------------------------------------

function respond(status, headers, body = null, head = false) {
  const h = new Headers();
  for (const [name, value] of headers) h.append(name, value);
  const bytes = body === null ? EMPTY : typeof body === "string" ? UTF8.encode(body) : body;
  const nullBody = status === 204 || status === 304;
  if (!nullBody) h.set("Content-Length", String(bytes.length));
  const res = new Response(nullBody || head ? null : bytes, { status, headers: h });
  if (!nullBody && !head && bytes.length) {
    try {
      res[RAW_BODY] = bytes; // e.g. a cached tracker file, sent by reference rather than copied per response
    } catch {
      // a platform whose Response objects are not extensible: the body is read the standard way
    }
  }
  return res;
}

function gifHeaders() {
  return [["Content-Type", "image/gif"], ["Cache-Control", "no-store, max-age=0"],
    ["X-Robots-Tag", "noindex, nofollow"], ["Access-Control-Allow-Origin", "*"]];
}

function notFound() {
  return respond(404, [["Content-Type", "text/plain"]], "not found\n");
}

function methodNotAllowed(allow) {
  return respond(405, [["Allow", allow], ["Content-Type", "text/plain"]], "method not allowed\n");
}

// ---------------------------------------------------------------------------
// Body reading
// ---------------------------------------------------------------------------

/** The in-flight body memory budget is used up: answer 503 without reading (more of) the body. */
class BodyBudgetError extends Error {}

/**
 * max(0, min(2^53 - 1, int(CONTENT_LENGTH or 0))) with Python's int() leniency; ValueError → 0. A larger declared
 * length is recorded as 2^53 - 1.
 */
function contentLength(value) {
  if (!value) return 0;
  const s = pyStrip(value).replace(/(\d)_(?=\d)/g, "$1");
  if (!/^[+-]?\d+$/.test(s)) return 0;
  return Math.max(0, Math.min(MAX_DECLARED_LENGTH, Number(s)));
}

/**
 * Read at most `want` bytes into one buffer that grows as they arrive (from 64 KiB, doubling, up to `want`): the
 * memory a body holds follows what was actually received, whatever it declares, and each piece is copied in at
 * once (no per-segment objects). Every growth is charged to `take(n)` first; a refusal throws BodyBudgetError.
 */
async function readInto(env, want, take) {
  const reader = env.body.getReader();
  let buf = EMPTY;
  let n = 0;
  try {
    while (n < want) {
      const { value, done } = await reader.read();
      if (done) break;
      let chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
      if (chunk.length > want - n) chunk = chunk.subarray(0, want - n);
      if (n + chunk.length > buf.length) {
        const size = Math.min(want, Math.max(BODY_BLOCK, buf.length * 2, n + chunk.length));
        if (!take(size - buf.length)) throw new BodyBudgetError();
        env.bodyReserved += size - buf.length;
        const grown = new Uint8Array(size);
        grown.set(buf.subarray(0, n));
        buf = grown;
      }
      buf.set(chunk, n);
      n += chunk.length;
    }
  } finally {
    reader.releaseLock();
  }
  return buf.subarray(0, n);
}

/**
 * collect.read_body: (body or null when too large, declared/observed size). The stream ends with the request. A
 * declared length is read exactly (a client that stops early leaves a shorter body, as a WSGI server would hand
 * it over); without one (chunked), up to limit + 1 bytes.
 */
async function readBody(env, limit, take) {
  const length = contentLength(env.header("content-length"));
  if (env.body === null) return [EMPTY, 0];
  if (length > limit) return [null, length];
  if (length > 0) {
    const body = await readInto(env, length, take);
    return [body, body.length];
  }
  const body = await readInto(env, limit + 1, take);
  if (body.length > limit) return [null, Math.min(body.length, limit + 1)];
  return [body, body.length];
}

// ---------------------------------------------------------------------------
// Envelope → hits
// ---------------------------------------------------------------------------

/**
 * Split an envelope into hit records with typed columns. At most `maxEvents` become hits: one request must not
 * turn into an unbounded number of rows. The caller keeps the raw body of a longer batch, flagged
 * "too_many_events:<count>", so nothing is lost. Only fp events under drop_fingerprint are dropped.
 */
export function buildHits(envelope, maxEvents, site = null) {
  const errors = [];
  if (!isPyInt(envelope.get("v"))) errors.push("bad_version");
  let events = envelope.get("events");
  if (!Array.isArray(events)) {
    errors.push("no_events");
    return [[], errors];
  }
  if (events.length > maxEvents) {
    errors.push(`too_many_events:${events.length}`);
    events = events.slice(0, maxEvents); // events[:max_events], negative limits included
  }
  const dropFp = Boolean(site && site.privacy.drop_fingerprint);
  const vid = pyStrOf(envelope.get("vid"), 64);
  const sid = pyStrOf(envelope.get("sid"), 64);
  const uid = pyStrOf(envelope.get("uid"), 200);
  const hits = [];
  let bad = 0;
  for (let seq = 0; seq < events.length; seq++) {
    const ev = events[seq];
    if (!(ev instanceof Map)) {
      bad += 1;
      hits.push(new HitRecord({ seq, data: dumpsCompact(ev), type: "_invalid", visitor_id: vid, session_id: sid, user_id: uid }));
      continue;
    }
    let etype = pyStrOf(ev.get("t"), 32);
    if (etype === null) {
      bad += 1;
      etype = "_invalid";
    }
    if (dropFp && etype === "fp") continue;
    const n = ev.get("n");
    hits.push(new HitRecord({
      seq, data: dumpsCompact(ev), type: etype,
      name: typeof n === "string" && n ? pySlice(n, 200) : null, // only a string names an event (n is a count elsewhere)
      client_ts: pyIntOf(ev.get("ts")), event_id: pyStrOf(ev.get("id"), 64),
      visitor_id: pyStrOf(ev.get("vid"), 64) || vid, session_id: pyStrOf(ev.get("sid"), 64) || sid,
      page_id: pyStrOf(ev.get("pid"), 64), user_id: pyStrOf(ev.get("uid"), 200) || uid,
      url: pyStrOf(ev.get("url"), 2048),
    }));
  }
  if (bad) errors.push(`bad_events:${bad}`);
  return [hits, errors];
}

function parseCookieHeader(value) {
  const out = [];
  for (const part of value.split(";")) {
    const eq = part.indexOf("=");
    if (eq >= 0) out.push([pyStrip(part.slice(0, eq)), pyStrip(part.slice(eq + 1))]);
    else if (pyStrip(part)) out.push([pyStrip(part), ""]);
  }
  return out;
}

// ---------------------------------------------------------------------------
// The collector
// ---------------------------------------------------------------------------

/**
 * @param {object} options
 * @param {string} options.connector            stored as server.connector ("node", "worker", ...)
 * @param {() => any} options.getConfig          the current Config (may return a promise)
 * @param {(cfg) => any} options.getStore        the store for this config (may return a promise)
 * @param {object} [options.log]                 {info, warning, error(msg, exc)}
 * @param {ErrorLog} [options.errors]            the process's error-log throttle (shared with the store)
 * @param {(cfg, parts, env) => Promise<Uint8Array|null>} [options.readStatic]  tracker files, when available
 * @param {(dataDir: string) => number|null} [options.diskFree]  free bytes on the file system holding the data
 *                                               directory (null: unknown); without it min_free_disk_mb is ignored
 * @param {number} [options.maxBodyMemory]       bytes all request bodies being read may hold together; beyond,
 *                                               /collect and /chunk answer 503 (default: no limit)
 * @param {() => number} [options.now]           clock (ms)
 */
export function createCollector({
  connector, getConfig, getStore, log = consoleLog, errors = null, readStatic = null, diskFree = null,
  maxBodyMemory = Infinity, now = Date.now,
}) {
  const disk = { checked: null, free: null, warned: -Infinity };
  // Request-path exceptions: full stacks for the first 5 of each minute, then only a count (collect.ERRORS).
  const errorLog = errors || new ErrorLog(log);
  const bodies = { used: 0 };
  const take = (n) => {
    if (bodies.used + n > maxBodyMemory) return false;
    bodies.used += n;
    return true;
  };

  const baseHeaders = (cfg) => {
    const headers = [["Cache-Control", "no-store"], ["X-Robots-Tag", "noindex, nofollow"]];
    if (cfg.server.accept_ch) headers.push(["Accept-CH", ACCEPT_CH]);
    return headers;
  };

  const cors = (cfg, env, site) => {
    const origin = env.header("origin");
    if (!origin || !ORIGIN_RE.test(origin)) return [];
    if (cfg.server.cors === "allowed" && (site === null || !site.originAllowed(origin))) return [["Vary", "Origin"]];
    return [["Access-Control-Allow-Origin", origin], ["Access-Control-Allow-Credentials", "true"], ["Vary", "Origin"]];
  };

  /** 503 while the in-flight body budget is used up: the connection closes, the rest of the body is not read. */
  const busy = (cfg, env, site) => respond(503, [...baseHeaders(cfg), ...cors(cfg, env, site),
    ["Retry-After", BUSY_RETRY_AFTER], ["Connection", "close"]]);

  const siteStatus = (cfg, siteId, env, requireOrigin) => {
    if (!siteId) return ["missing_site", null];
    const site = cfg.site(siteId);
    if (site === null) return ["unknown_site", null];
    const origin = env.header("origin") || originOf(env.header("referer"));
    if (origin) return [site.originAllowed(origin) ? "ok" : "origin_mismatch", site];
    if (site.origins.length && requireOrigin) return ["no_origin", site];
    return ["ok", site];
  };

  const privacyDrop = (cfg, site, env) => {
    const privacy = site ? site.privacy : cfg.privacy;
    if (privacy.honour_dnt && pyStrip(env.header("dnt") || "") === "1") return "dropped_dnt";
    if (privacy.honour_gpc && pyStrip(env.header("sec-gpc") || "") === "1") return "dropped_gpc";
    return null;
  };

  /**
   * collect._identity: what is kept about the requester, the address (anonymized in privacy mode) and the headers.
   * A Referer can hold the full page URL (beacons, the pixel): its secrets are redacted with the site's
   * redact_params and redact_paths (redact.mjs), as the tracker does for the URLs it sends.
   */
  const identity = async (cfg, site, env, headers) => {
    const privacy = site ? site.privacy : cfg.privacy;
    let ip = env.remoteAddr || null;
    if (privacy.hash_ip || privacy.truncate_ip) {
      const doHash = Boolean(privacy.hash_ip);
      const doTruncate = Boolean(privacy.truncate_ip);
      ip = await anonymizeIp(ip, doHash, doTruncate, cfg.secret);
      headers = await anonymizeHeaders(headers, doHash, doTruncate, cfg.secret);
    }
    if (headers.has("referer")) {
      const { params, paths } = cfg.redactRules(site);
      headers.set("referer", redactUrl(headers.get("referer"), params, paths));
    }
    return [ip, headers];
  };

  /**
   * collect._client_id: the client in allowance keys, its key (clientKeyFor) hashed with the secret as hash_ip
   * does, so that the rate_limits table (and its backups) never holds an address. Once per request.
   */
  const clientIdOf = async (cfg, env) => {
    if (env.clientId === null) {
      env.clientId = await hashIp(clientKeyFor(cfg, env.remoteAddr, env.fields) || "-", cfg.secret);
    }
    return env.clientId;
  };

  /**
   * collect._limits: what this request may consume (docs/configuration.md, "Limits and abuse protection"). Per
   * client and minute: requests and kilobytes (separate allowances for /chunk); requests that would only be stored
   * flagged have their own, smaller allowance, per client and for all clients together; a configured site has a
   * daily quota of stored hits.
   */
  const limitsFor = async (cfg, env, site, chunk, size, flagged, hits = 0) => {
    const s = cfg.server;
    const client = await clientIdOf(cfg, env);
    const counter = chunk ? "chunk_rate_limited" : "rate_limited";
    // Every key carries a prefix, so no client value (a header can say anything) can alias another row.
    const budgets = [
      budget("r:" + client, 1, chunk ? s.chunk_rate_limit_per_minute : s.rate_limit_per_minute, counter),
      budget("kb:" + client, kb(size), chunk ? s.chunk_kb_per_minute : s.rate_limit_kb_per_minute, counter),
    ];
    if (flagged) {
      budgets.push(budget("f:" + client, 1, s.flagged_per_minute, "flagged_limited"));
      budgets.push(budget("*f", 1, s.flagged_global_per_minute, "flagged_limited"));
    }
    const counted = site !== null && !flagged && !chunk;
    return makeLimits({
      budgets, site: site !== null ? site.id : "", quota: counted ? site.settings.max_hits_per_day : 0,
      hits: counted ? hits : 0,
    });
  };

  /** collect._disk_low: less free space than min_free_disk_mb where the data lives (checked every 10 s). */
  const diskLow = (cfg) => {
    const need = Math.trunc(Number(cfg.server.min_free_disk_mb) || 0);
    if (need <= 0 || !diskFree) return false;
    const t = performance.now();
    if (disk.checked === null || t - disk.checked >= DISK_CHECK_MS) {
      disk.checked = t;
      try {
        disk.free = diskFree(cfg.dataDir);
      } catch {
        disk.free = null;
      }
    }
    const low = disk.free !== null && disk.free !== undefined && disk.free < need * 1024 * 1024;
    if (low && t - disk.warned > 60000) {
      disk.warned = t;
      log.warning(`less than ${need} MB free in ${cfg.dataDir}: refusing writes (503)`);
    }
    return low;
  };

  /**
   * collect._refused_early: a client already over its request or kilobyte allowance this minute is refused before
   * its body is decompressed, parsed or compressed (the costly part of a request): charged (r: 1, then kb: the
   * body received) and counted as any refusal, under the site given if it is configured.
   */
  const refusedEarly = async (cfg, env, site, chunk, size) => {
    const s = cfg.server;
    const client = await clientIdOf(cfg, env);
    const caps = [["r:" + client, chunk ? s.chunk_rate_limit_per_minute : s.rate_limit_per_minute],
      ["kb:" + client, chunk ? s.chunk_kb_per_minute : s.rate_limit_kb_per_minute]].filter(([, cap]) => cap > 0);
    if (!caps.length || diskLow(cfg)) return false; // low disk: the normal path answers 503
    const stream = chunk ? "chunks" : "events";
    const t = now();
    try {
      const store = await getStore(cfg);
      const used = await store.usage(stream, caps.map(([key]) => key), t);
      if (!caps.some(([key, cap]) => (used[key] || 0) >= cap)) return false;
      const limits = await limitsFor(cfg, env, site, chunk, size, false);
      await store.refuse(stream, makeLimits({ budgets: limits.budgets, site: limits.site }), t);
    } catch (exc) { // never refuse because the check itself failed
      errorLog.exception("could not check allowances", exc);
      return false;
    }
    return true;
  };

  const write = async (cfg, kind, rec, limits) => {
    const store = await getStore(cfg);
    try {
      return await (kind === "chunk" ? store.writeChunk(rec, limits) : store.writeRequest(rec, limits));
    } catch (exc) {
      // Without the spool fallback a database or file-system error reaches here: still a 503, as in Python.
      const storage = exc && (exc.code === "ERR_SQLITE_ERROR" || typeof exc.errcode === "number" || exc.syscall);
      if (!storage) throw exc;
      throw new StorageError(`could not store the record: ${exc.message}`, { cause: exc });
    }
  };

  /**
   * collect._count: count a request that stores nothing else (a DNT/GPC drop, an oversize chunk), under a configured
   * site only (never an id from the request), and only within the client's request allowance (r:<client>, in
   * events.db): a flood of them must not become a flood of writes.
   */
  const count = async (cfg, site, name, env) => {
    const siteId = site !== null ? site.id : "";
    try {
      const limits = makeLimits({
        budgets: [budget("r:" + (await clientIdOf(cfg, env)), 1, cfg.server.rate_limit_per_minute, "")], site: siteId,
      });
      const store = await getStore(cfg);
      await store.recordCounter(utcDay(now()), siteId, name, 1, limits);
    } catch (exc) {
      errorLog.exception(`could not record counter ${name}`, exc);
    }
  };

  const extractHeaders = (cfg, env) => {
    const rules = cfg.headerRules;
    const headers = new Map();
    let cookies = null;
    for (const [name, value] of env.headers) {
      if ((name === "content-type" || name === "content-length") && !value) continue;
      if (rules.drop.has(name)) continue;
      if (name === "cookie") {
        const kept = new Map();
        let others = 0;
        for (const [cname, cval] of parseCookieHeader(value)) {
          // The dashboard's sign-in cookie (ts_login) is never kept, whatever cookie_names says.
          if (cname !== "ts_login" && kept.size < MAX_KEPT_COOKIES && rules.cookies.some((re) => re.test(cname))) {
            // A comma is not a cookie character: past it is another Cookie header the server merged in.
            kept.set(cname, pySlice(pyRstrip(cval.split(",", 1)[0]), 512));
          } else {
            others += 1; // beyond 20 kept cookies, matching ones are only counted
          }
        }
        cookies = new Map([["kept", kept], ["others", others]]);
        continue;
      }
      if (ALWAYS_SECRET.has(name) || rules.secret.some((re) => re.test(name))) {
        headers.set(name, REDACTED);
        continue;
      }
      headers.set(name, pySlice(value, MAX_HEADER_VALUE));
    }
    return [headers, cookies];
  };

  const serverInfo = (env) => {
    const info = { connector, version: VERSION, scheme: env.scheme, path: pySlice(env.scriptName + env.path, 512) };
    for (const [key, value] of Object.entries(env.server)) {
      if (value === null || value === undefined || value === "" || value === false) continue;
      info[key] = typeof value === "object" ? value : pySlice(String(value), 200);
    }
    return info;
  };

  // -- /collect --------------------------------------------------------

  const handlePreflight = (cfg, env) => respond(204, [...cors(cfg, env, null),
    ["Access-Control-Allow-Methods", "POST, GET, OPTIONS"],
    ["Access-Control-Allow-Headers", "Content-Type, Content-Encoding"],
    ["Access-Control-Max-Age", "86400"], ["Cache-Control", "no-store"]]);

  /** Decompress, decode and parse. Returns [envelope, error, encoding, decoded bytes]. */
  const parseEnvelope = async (body, env, s, contentType) => {
    let encoding = "identity";
    let data = body;
    const contentEncoding = (env.header("content-encoding") || "").toLowerCase();
    if (isGzip(body) || contentEncoding.includes("gzip")) {
      encoding = "gzip";
      try {
        data = await gunzipBounded(body, s.max_decompressed_kb * 1024);
      } catch (exc) {
        return [null, `bad_gzip: ${exc.message}`, encoding, body];
      }
    } else if (contentEncoding && contentEncoding !== "identity") {
      const enc = pySlice(contentEncoding, 40);
      return [null, `unsupported_encoding: ${enc}`, enc, body];
    }
    if (!data.length) return [null, "empty_body", encoding, data];
    let text;
    try {
      text = UTF8_STRICT.decode(data);
    } catch {
      return [null, "bad_utf8", encoding, data];
    }
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    if (contentType && contentType.toLowerCase().includes("application/x-www-form-urlencoded")) {
      const form = parseQs(text, true);
      text = (form.get("d") || form.get("data") || [""])[0];
    }
    let obj;
    try {
      obj = loads(text, { parseConstant: rejectConstant, parseFloat: finiteFloat });
    } catch (exc) {
      return [null, `bad_json: ${pySlice(String(exc.message), 120)}`, encoding, data];
    }
    if (!(obj instanceof Map)) return [null, "not_an_object", encoding, data];
    if (deeperThan(obj, MAX_JSON_DEPTH)) { // nothing the tracker sends nests deeper than a few levels
      return [null, `bad_json: nested deeper than ${MAX_JSON_DEPTH} levels`, encoding, data];
    }
    return [obj, null, encoding, data];
  };

  const serverCookie = (site, envelope, env) => {
    const domain = site.settings.server_cookie_domain;
    const vid = envelope.get("vid");
    if (!domain || typeof vid !== "string" || !ID_RE.test(vid)) return null;
    const vt = pyIntOf(envelope.get("vt"));
    const value = vt !== null && vt > 0 ? `${vid}.${vt}` : vid;
    if (!COOKIE_DOMAIN_RE.test(domain)) return null;
    const https = String(env.server.https || "").toLowerCase();
    const secure = env.scheme === "https" || https === "on" || https === "1";
    const maxAge = BigInt(Math.trunc(Number(site.settings.cookie_days ?? 400))) * 86400n;
    const parts = [`ts_svid=${value}`, `Domain=${domain}`, "Path=/", `Max-Age=${maxAge}`, "SameSite=Lax"];
    if (secure) parts.push("Secure");
    return parts.join("; ");
  };

  const handleCollect = async (cfg, env) => {
    const received = now();
    const s = cfg.server;
    const query = env.query;
    const qs = parseQs(query, true);
    const querySite = () => pyStrOf((qs.get("site") || [null])[0], 100);
    const contentType = env.header("content-type") || null;
    const [headers, cookies] = extractHeaders(cfg, env);
    let body;
    let size;
    try {
      [body, size] = await readBody(env, s.max_payload_kb * 1024, take);
    } catch (exc) {
      if (exc instanceof BodyBudgetError) return busy(cfg, env, cfg.site(querySite()));
      throw exc;
    }
    if (body !== null) {
      const qSite = cfg.site(querySite());
      if (await refusedEarly(cfg, env, qSite, false, size + pyLen(query))) {
        return respond(429, [...baseHeaders(cfg), ...cors(cfg, env, qSite), ["Retry-After", "60"]]);
      }
    }

    const rec = new RequestRecord({
      received_at: received, endpoint: "collect", method: "POST", site_id: null, site_status: "invalid", ip: null,
      headers, cookies, query: query || null, content_type: contentType, body_bytes: size, server: serverInfo(env),
    });
    let status = 204;
    let envelope = null;
    let site = null;
    let storedSize = size; // what this request would store, for the per-minute KB budget

    if (body === null) {
      rec.error = "too_large";
      rec.site_status = "too_large";
      rec.site_id = querySite();
      status = 413;
      storedSize = s.max_payload_kb * 1024 + 1; // what was read, not what the request claims
    } else {
      const [parsed, error, encoding, data] = await parseEnvelope(body, env, s, contentType);
      envelope = parsed;
      rec.body_encoding = encoding;
      storedSize = Math.max(size, data.length);
      if (error) {
        rec.error = error;
        rec.body = body;
        status = 400;
      } else if (s.store_raw_body) {
        rec.body = body;
      }
    }

    if (envelope !== null) {
      const siteId = pyStrOf(envelope.get("site"), 100) || querySite();
      rec.site_id = siteId;
      [rec.site_status, site] = siteStatus(cfg, siteId, env, true);
    } else {
      rec.site_id = rec.site_id || querySite();
      site = cfg.site(rec.site_id);
    }
    const corsHeaders = cors(cfg, env, site);
    const dropped = privacyDrop(cfg, site, env);
    if (dropped) {
      await count(cfg, site, dropped, env);
      return respond(204, [...baseHeaders(cfg), ...corsHeaders]);
    }
    if (diskLow(cfg)) return respond(503, [...baseHeaders(cfg), ...corsHeaders, ["Retry-After", "300"]]);
    [rec.ip, rec.headers] = await identity(cfg, site, env, rec.headers);

    if (envelope !== null) {
      const [hits, errors] = buildHits(envelope, s.max_events_per_request, site);
      rec.hits = hits;
      rec.proto_v = pyIntOf(envelope.get("v"));
      rec.envelope = new Map([...envelope].filter(([k]) => k !== "events"));
      if (errors.length) {
        rec.error = errors.join(",");
        const tooMany = errors.some((e) => e.startsWith("too_many_events"));
        if (tooMany || errors.includes("no_events") || errors.includes("not_an_object")) status = 400;
        if (tooMany) rec.body = body; // the events not stored as hits stay in the raw body
      }
    }

    const flagged = isFlagged(rec); // a batch with a few invalid events still has good hits: normal allowances
    // What the request stores: its body, its hits as serialized (re-serialized numbers can be longer than sent:
    // 9e15 becomes 9000000000000000.0), at least HIT_COST a hit, and the query and headers.
    let storedHits = 0;
    for (const h of rec.hits) if (h.data) storedHits += pyLen(h.data);
    const charged = Math.max(storedSize, HIT_COST * rec.hits.length, storedHits) + pyLen(query) +
      headerBytes(rec.headers, rec.cookies);
    const limits = await limitsFor(cfg, env, site, false, charged, flagged, rec.hits.length);
    let outcome;
    try {
      outcome = await write(cfg, "request", rec, limits);
    } catch (exc) {
      if (!(exc instanceof StorageError)) throw exc;
      errorLog.exception("storage unavailable", exc);
      return respond(503, [...baseHeaders(cfg), ...corsHeaders, ["Retry-After", "60"]]);
    }
    const out = [...baseHeaders(cfg), ...corsHeaders];
    if (outcome === RATE_LIMITED && status === 204) return respond(429, [...out, ["Retry-After", "60"]]);
    if (outcome === RATE_LIMITED) return respond(status, out); // 400 and 413 stand: retrying cannot help
    if (outcome === TOO_LARGE) return respond(413, out); // a row the database cannot hold: metadata stored
    if (status === 204 && site !== null && rec.site_status === "ok" && envelope !== null) {
      const cookie = serverCookie(site, envelope, env);
      if (cookie) out.push(["Set-Cookie", cookie]);
    }
    return respond(status, out);
  };

  // -- /chunk ----------------------------------------------------------

  const handleChunk = async (cfg, env) => {
    const received = now();
    const s = cfg.server;
    const query = env.query;
    const qs = new Map([...parseQs(query, true)].map(([k, v]) => [k, v[0]]));
    const siteId = pyStrOf(qs.get("site"), 100);
    let [status, site] = siteStatus(cfg, siteId, env, true);
    const dropped = privacyDrop(cfg, site, env);
    const corsHeaders = cors(cfg, env, site);
    if (dropped) {
      await count(cfg, site, dropped, env);
      return respond(204, [...baseHeaders(cfg), ...corsHeaders]);
    }
    let body;
    let size;
    try {
      [body, size] = await readBody(env, s.max_chunk_kb * 1024, take);
    } catch (exc) {
      if (exc instanceof BodyBudgetError) return busy(cfg, env, site);
      throw exc;
    }
    if (body === null) {
      await count(cfg, site, "chunk_too_large", env);
      return respond(413, [...baseHeaders(cfg), ...corsHeaders]);
    }
    if (diskLow(cfg)) return respond(503, [...baseHeaders(cfg), ...corsHeaders, ["Retry-After", "300"]]);
    if (await refusedEarly(cfg, env, site, true, size + pyLen(query))) {
      return respond(429, [...baseHeaders(cfg), ...corsHeaders, ["Retry-After", "60"]]);
    }
    const k = qs.get("k");
    const kind = CHUNK_KINDS.has(k) ? k : "_invalid";
    let error = kind !== "_invalid" ? null : "unknown_kind";
    if (!body.length) error = "empty_body";
    if (site !== null && status === "ok" && Object.hasOwn(CHUNK_FEATURES, kind) && !site.features[CHUNK_FEATURES[kind]]) {
      status = "feature_off"; // the site does not record this kind: stored flagged, within its allowance
    }
    const sentGzip = isGzip(body);
    const payload = sentGzip ? body : await gzipCompress(body);
    let [headers] = extractHeaders(cfg, env);
    let ip;
    [ip, headers] = await identity(cfg, site, env, headers);
    const meta = new Map(qs);
    meta.set("bytes", size);
    meta.set("sent_encoding", sentGzip ? "gzip" : "identity");
    for (const h of ["user-agent", "origin", "referer"]) if (headers.has(h)) meta.set(h, headers.get(h));
    const rec = new ChunkRecord({
      received_at: received, kind, site_id: siteId, site_status: status, payload, encoding: "gzip", meta,
      visitor_id: pyStrOf(qs.get("vid"), 64), session_id: pyStrOf(qs.get("sid"), 64),
      page_id: pyStrOf(qs.get("pid"), 64), seq: pyIntOf(qs.get("seq")), client_ts: pyIntOf(qs.get("ts")),
      url: pyStrOf(qs.get("url"), 2048), ip, error,
    });
    // Charged: the body, the query string and the headers kept in meta (as stored, from the query string if not).
    const kept = new Map(["user-agent", "origin", "referer"].filter((h) => meta.has(h)).map((h) => [h, meta.get(h)]));
    const charged = size + pyLen(query) + headerBytes(kept, null);
    const limits = await limitsFor(cfg, env, site, true, charged, status !== "ok" || error !== null);
    let outcome;
    try {
      outcome = await write(cfg, "chunk", rec, limits);
    } catch (exc) {
      if (!(exc instanceof StorageError)) throw exc;
      errorLog.exception("storage unavailable", exc);
      return respond(503, [...baseHeaders(cfg), ...corsHeaders, ["Retry-After", "60"]]);
    }
    if (outcome === RATE_LIMITED && !error) {
      return respond(429, [...baseHeaders(cfg), ...corsHeaders, ["Retry-After", "60"]]);
    }
    if (outcome === TOO_LARGE) return respond(413, [...baseHeaders(cfg), ...corsHeaders]);
    return respond(error ? 400 : 204, [...baseHeaders(cfg), ...corsHeaders]);
  };

  // -- /p.gif ----------------------------------------------------------

  const handlePixel = async (cfg, env) => {
    const received = now();
    const method = env.method;
    const query = env.query;
    const data = new Map();
    const first = new Map();
    for (const [key, values] of parseQs(query, true)) {
      data.set(key, values.length === 1 ? values[0] : values);
      first.set(key, values[0]);
    }
    const siteId = pyStrOf(first.get("site"), 100);
    const [status, site] = siteStatus(cfg, siteId, env, false);
    if (privacyDrop(cfg, site, env)) return respond(200, gifHeaders(), GIF, method === "HEAD");
    let [headers, cookies] = extractHeaders(cfg, env);
    let ip;
    [ip, headers] = await identity(cfg, site, env, headers);
    const url = first.get("u") || first.get("url") || headers.get("referer"); // the stored (redacted) Referer
    const hit = new HitRecord({
      seq: 0, data: dumpsCompact(data), type: pyStrOf(first.get("t"), 32) || "pageview",
      name: pyStrOf(first.get("n"), 200), event_id: pyStrOf(first.get("id"), 64),
      visitor_id: pyStrOf(first.get("vid"), 64), session_id: pyStrOf(first.get("sid"), 64),
      user_id: pyStrOf(first.get("uid"), 200), url: pyStrOf(url, 2048), client_ts: pyIntOf(first.get("ts")),
    });
    const rec = new RequestRecord({
      received_at: received, endpoint: "pixel", method, site_id: siteId, site_status: status, ip, headers, cookies,
      query: query || null, server: serverInfo(env), hits: [hit],
    });
    if (!diskLow(cfg)) { // the pixel answers its GIF whatever happens to the hit
      const charged = Math.max(pyLen(query), HIT_COST) + headerBytes(headers, cookies);
      try {
        const limits = await limitsFor(cfg, env, site, false, charged, isFlagged(rec), 1);
        await write(cfg, "request", rec, limits);
      } catch (exc) {
        if (!(exc instanceof StorageError)) throw exc;
        errorLog.exception("storage unavailable (pixel)", exc);
      }
    }
    return respond(200, gifHeaders(), GIF, method === "HEAD");
  };

  // -- /config.js ------------------------------------------------------

  const handleConfig = async (cfg, env, asJson) => {
    const method = env.method;
    if (method !== "GET" && method !== "HEAD") return methodNotAllowed("GET, HEAD");
    const qs = parseQs(env.query, false);
    const siteId = pyStrOf((qs.get("site") || [""])[0], 100) || "";
    const pub = await cfg.publicSiteConfig(siteId);
    const etag = `"${pub.cv}"`;
    const headers = [["Cache-Control", "public, max-age=300"], ["ETag", etag], ["Access-Control-Allow-Origin", "*"],
      ["X-Content-Type-Options", "nosniff"], ["X-Robots-Tag", "noindex, nofollow"]];
    if (env.header("if-none-match") === etag) return respond(304, headers);
    let payload;
    if (asJson) {
      headers.push(["Content-Type", "application/json; charset=utf-8"]);
      payload = pub.body;
    } else {
      headers.push(["Content-Type", "application/javascript; charset=utf-8"]);
      // An element with id "tidesight" makes window.tidesight an object: only a function is the tracker's queue.
      payload = '(typeof window.tidesight=="function"?window.tidesight:window.tidesight=function(){' +
        `(tidesight.q=tidesight.q||[]).push(arguments)})("_config",${pub.body});\n`;
    }
    return respond(200, headers, payload, method === "HEAD");
  };

  // -- /health ---------------------------------------------------------

  const handleHealth = async (cfg) => {
    const store = await getStore(cfg);
    const ok = Boolean(await store.healthy());
    const body = new Map([["status", ok ? "ok" : "error"], ["version", VERSION],
      ["storage", store.label || cfg.server.storage_mode], ["time", now()]]);
    return respond(ok ? 200 : 503, [["Content-Type", "application/json"], ["Cache-Control", "no-store"]], dumps(body));
  };

  // -- static tracker files --------------------------------------------

  const handleStatic = async (cfg, env, path) => {
    if (path === "/robots.txt") return respond(200, [["Content-Type", "text/plain"]], "User-agent: *\nDisallow: /\n");
    const parts = path.split("/").filter(Boolean);
    if (!parts.length || parts.length > 2 || !parts.every((p) => STATIC_NAME_RE.test(p))) return notFound();
    if (parts.length === 2 && parts[0] !== "ts") return notFound();
    const name = parts[parts.length - 1];
    const dot = name.lastIndexOf(".");
    const ctype = dot > 0 ? STATIC_TYPES[name.slice(dot).toLowerCase()] : undefined;
    if (!ctype || !readStatic) return notFound();
    const content = await readStatic(cfg, parts, env);
    if (!content) return notFound();
    const versioned = env.query.includes("v=");
    const headers = [["Content-Type", ctype], ["Access-Control-Allow-Origin", "*"], ["X-Content-Type-Options", "nosniff"],
      ["Cache-Control", versioned ? "public, max-age=31536000, immutable" : "public, max-age=3600"]];
    return respond(200, headers, content, env.method === "HEAD");
  };

  // -- routing ---------------------------------------------------------

  /** Handle one request. `extras` carries what the Fetch API cannot (see the module comment). */
  async function fetch(request, extras = {}) {
    let cfg;
    try {
      cfg = await getConfig();
    } catch (exc) {
      errorLog.exception("configuration unavailable", exc);
      return respond(503, [["Content-Type", "text/plain"]], "configuration error\n");
    }
    let env;
    let path = "/";
    let method = "GET";
    try {
      env = new Environ(request, extras);
      path = env.path || "/";
      method = env.method;
      if (path === "/collect") {
        if (method === "POST") return await handleCollect(cfg, env);
        if (method === "OPTIONS") return handlePreflight(cfg, env);
        return methodNotAllowed("POST, OPTIONS");
      }
      if (path === "/chunk") {
        if (method === "POST") return await handleChunk(cfg, env);
        if (method === "OPTIONS") return handlePreflight(cfg, env);
        return methodNotAllowed("POST, OPTIONS");
      }
      if (path === "/p.gif") return await handlePixel(cfg, env);
      if (path === "/config.js" || path === "/config.json") return await handleConfig(cfg, env, path.endsWith(".json"));
      if (path === "/health") return await handleHealth(cfg, env);
      if (path === "/" && (method === "GET" || method === "HEAD")) {
        return respond(200, [["Content-Type", "text/plain; charset=utf-8"]], "Tidesight\n", method === "HEAD");
      }
      if ((method === "GET" || method === "HEAD") && (path.startsWith("/ts") || path === "/robots.txt")) {
        return await handleStatic(cfg, env, path);
      }
    } catch (exc) {
      errorLog.exception(`unhandled error on ${method} ${path}`, exc);
      if (path === "/p.gif") return respond(200, gifHeaders(), GIF);
      return respond(500, [["Content-Type", "text/plain"]], "error\n");
    } finally {
      if (env && env.bodyReserved) {
        bodies.used -= env.bodyReserved; // the body is no longer held by this request
        env.bodyReserved = 0;
      }
    }
    return notFound();
  }

  return { fetch, bodyMemory: () => bodies.used };
}
