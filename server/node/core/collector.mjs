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
 * malformed or unknown-site hits flagged instead of dropping them. Behaviour, status codes, headers and the
 * rows written match the Python collector; docs/protocol.md is the contract.
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

import { loads, dumpsCompact, dumps, isPyInt } from "./pyjson.mjs";
import { parseQs, pySlice, pyStrip, pyStrOf, pyIntOf, originOf, unquoteLatin1, utcDay } from "./pycompat.mjs";
import { anonymizeIp, anonymizeHeaders } from "./anonymize.mjs";
import { gunzipBounded, gzipCompress, isGzip } from "./gzip.mjs";
import { ChunkRecord, HitRecord, RATE_LIMITED, RequestRecord, StorageError } from "./records.mjs";
import { VERSION } from "./version.mjs";

export const GIF = Uint8Array.from(atob("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="), (c) => c.charCodeAt(0));

const ACCEPT_CH = "Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform, Sec-CH-UA-Platform-Version, Sec-CH-UA-Model, " +
  "Sec-CH-UA-Full-Version-List, Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Form-Factors, " +
  "Sec-CH-UA-WoW64, Viewport-Width, DPR, Device-Memory, RTT, Downlink, ECT, Sec-CH-Prefers-Color-Scheme, " +
  "Sec-CH-Prefers-Reduced-Motion";

const CHUNK_KINDS = new Set(["replay", "mouse", "snapshot"]);
const MAX_HEADER_VALUE = 8192;
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
    this.headers = new Map();
    const add = (name, value) => {
      const key = String(name).toLowerCase().replace(/_/g, "-");
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
  return new Response(nullBody || head ? null : bytes, { status, headers: h });
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

/** Read from a stream until `max` bytes or its end (the last chunk may overshoot `max`). */
async function readStream(stream, max) {
  const reader = stream.getReader();
  const parts = [];
  let total = 0;
  try {
    while (total < max) {
      const { value, done } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
      parts.push(chunk);
      total += chunk.byteLength;
    }
  } finally {
    reader.releaseLock();
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.byteLength;
  }
  return out;
}

/** int(CONTENT_LENGTH or 0) with Python's int() leniency; ValueError → 0. */
function contentLength(value) {
  if (!value) return 0;
  const s = pyStrip(value).replace(/(\d)_(?=\d)/g, "$1");
  return /^[+-]?\d+$/.test(s) ? Number(s) : 0;
}

/** collect.read_body: (body or null when too large, declared/observed size). The stream ends with the request. */
async function readBody(env, limit) {
  const length = contentLength(env.header("content-length"));
  if (env.body === null) return [EMPTY, 0];
  if (length > limit) return [null, length];
  if (length > 0) {
    const body = await readStream(env.body, length);
    const exact = body.length > length ? body.subarray(0, length) : body;
    return [exact, exact.length];
  }
  const body = await readStream(env.body, limit + 1);
  if (body.length > limit) return [null, Math.min(body.length, limit + 1)];
  return [body, body.length];
}

// ---------------------------------------------------------------------------
// Envelope → hits
// ---------------------------------------------------------------------------

/** Split an envelope into hit records with typed columns. Never drops an event (except fp under drop_fingerprint). */
export function buildHits(envelope, maxEvents, site = null) {
  const errors = [];
  if (!isPyInt(envelope.get("v"))) errors.push("bad_version");
  const events = envelope.get("events");
  if (!Array.isArray(events)) {
    errors.push("no_events");
    return [[], errors];
  }
  if (events.length > maxEvents) errors.push("too_many_events");
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
 * @param {(cfg, parts, env) => Promise<Uint8Array|null>} [options.readStatic]  tracker files, when available
 * @param {() => number} [options.now]           clock (ms)
 */
export function createCollector({ connector, getConfig, getStore, log = consoleLog, readStatic = null, now = Date.now }) {
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

  const identity = async (cfg, site, env, headers) => {
    const privacy = site ? site.privacy : cfg.privacy;
    let ip = env.remoteAddr || null;
    if (privacy.hash_ip || privacy.truncate_ip) {
      const doHash = Boolean(privacy.hash_ip);
      const doTruncate = Boolean(privacy.truncate_ip);
      ip = await anonymizeIp(ip, doHash, doTruncate, cfg.secret);
      headers = await anonymizeHeaders(headers, doHash, doTruncate, cfg.secret);
    }
    return [ip, headers];
  };

  const rateKey = (cfg, env) => {
    const keyName = cfg.server.rate_limit_key;
    if (keyName === "remote_addr") return env.remoteAddr || null;
    const value = env.header(String(keyName).toLowerCase().replace(/_/g, "-"));
    if (value) return pySlice(pyStrip(value.split(",")[0]), 64);
    return env.remoteAddr || null;
  };

  const write = async (cfg, kind, rec, env) => {
    const store = await getStore(cfg);
    const limit = cfg.server.rate_limit_per_minute;
    const key = rateKey(cfg, env);
    try {
      return await (kind === "chunk" ? store.writeChunk(rec, key, limit) : store.writeRequest(rec, key, limit));
    } catch (exc) {
      // Without the spool fallback a database or file-system error reaches here: still a 503, as in Python.
      const storage = exc && (exc.code === "ERR_SQLITE_ERROR" || typeof exc.errcode === "number" || exc.syscall);
      if (!storage) throw exc;
      throw new StorageError(`could not store the record: ${exc.message}`, { cause: exc });
    }
  };

  const count = async (cfg, siteId, name) => {
    try {
      const store = await getStore(cfg);
      await store.recordCounter(utcDay(now()), siteId, name, 1);
    } catch (exc) {
      log.error(`could not record counter ${name}`, exc);
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
          if (rules.cookies.some((re) => re.test(cname))) kept.set(cname, pySlice(cval, 512));
          else others += 1;
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

  const parseEnvelope = async (body, env, s, contentType) => {
    let encoding = "identity";
    let data = body;
    const contentEncoding = (env.header("content-encoding") || "").toLowerCase();
    if (isGzip(body) || contentEncoding.includes("gzip")) {
      encoding = "gzip";
      try {
        data = await gunzipBounded(body, s.max_decompressed_kb * 1024);
      } catch (exc) {
        return [null, `bad_gzip: ${exc.message}`, encoding];
      }
    } else if (contentEncoding && contentEncoding !== "identity") {
      const enc = pySlice(contentEncoding, 40);
      return [null, `unsupported_encoding: ${enc}`, enc];
    }
    if (!data.length) return [null, "empty_body", encoding];
    let text;
    try {
      text = UTF8_STRICT.decode(data);
    } catch {
      return [null, "bad_utf8", encoding];
    }
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    if (contentType && contentType.toLowerCase().includes("application/x-www-form-urlencoded")) {
      const form = parseQs(text, true);
      text = (form.get("d") || form.get("data") || [""])[0];
    }
    let obj;
    try {
      obj = loads(text, { parseConstant: rejectConstant });
    } catch (exc) {
      return [null, `bad_json: ${pySlice(String(exc.message), 120)}`, encoding];
    }
    if (!(obj instanceof Map)) return [null, "not_an_object", encoding];
    return [obj, null, encoding];
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
    const [body, size] = await readBody(env, s.max_payload_kb * 1024);

    const rec = new RequestRecord({
      received_at: received, endpoint: "collect", method: "POST", site_id: null, site_status: "invalid", ip: null,
      headers, cookies, query: query || null, content_type: contentType, body_bytes: size, server: serverInfo(env),
    });
    let status = 204;
    let envelope = null;
    let site = null;

    if (body === null) {
      rec.error = "too_large";
      rec.site_status = "too_large";
      rec.site_id = querySite();
      status = 413;
    } else {
      const [parsed, error, encoding] = await parseEnvelope(body, env, s, contentType);
      envelope = parsed;
      rec.body_encoding = encoding;
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
    const dropped = privacyDrop(cfg, site, env);
    if (dropped) {
      await count(cfg, rec.site_id, dropped);
      return respond(204, [...baseHeaders(cfg), ...cors(cfg, env, site)]);
    }
    [rec.ip, rec.headers] = await identity(cfg, site, env, rec.headers);

    if (envelope !== null) {
      const [hits, errors] = buildHits(envelope, s.max_events_per_request, site);
      rec.hits = hits;
      rec.proto_v = pyIntOf(envelope.get("v"));
      rec.envelope = new Map([...envelope].filter(([k]) => k !== "events"));
      if (errors.length) {
        rec.error = errors.join(",");
        if (errors.includes("no_events") || errors.includes("not_an_object")) status = 400;
      }
    }

    let outcome;
    try {
      outcome = await write(cfg, "request", rec, env);
    } catch (exc) {
      if (!(exc instanceof StorageError)) throw exc;
      log.error("storage unavailable", exc);
      return respond(503, [...baseHeaders(cfg), ...cors(cfg, env, site), ["Retry-After", "60"]]);
    }
    const out = [...baseHeaders(cfg), ...cors(cfg, env, site)];
    if (outcome === RATE_LIMITED) return respond(429, [...out, ["Retry-After", "60"]]);
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
    const qs = new Map([...parseQs(env.query, true)].map(([k, v]) => [k, v[0]]));
    const siteId = pyStrOf(qs.get("site"), 100);
    const [status, site] = siteStatus(cfg, siteId, env, true);
    const dropped = privacyDrop(cfg, site, env);
    const corsHeaders = cors(cfg, env, site);
    if (dropped) {
      await count(cfg, siteId, dropped);
      return respond(204, [...baseHeaders(cfg), ...corsHeaders]);
    }
    const [body, size] = await readBody(env, s.max_chunk_kb * 1024);
    if (body === null) {
      await count(cfg, siteId, "chunk_too_large");
      return respond(413, [...baseHeaders(cfg), ...corsHeaders]);
    }
    const k = qs.get("k");
    const kind = CHUNK_KINDS.has(k) ? k : "_invalid";
    let error = kind !== "_invalid" ? null : "unknown_kind";
    if (!body.length) error = "empty_body";
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
    let outcome;
    try {
      outcome = await write(cfg, "chunk", rec, env);
    } catch (exc) {
      if (!(exc instanceof StorageError)) throw exc;
      log.error("storage unavailable", exc);
      return respond(503, [...baseHeaders(cfg), ...corsHeaders, ["Retry-After", "60"]]);
    }
    if (outcome === RATE_LIMITED) return respond(429, [...baseHeaders(cfg), ...corsHeaders, ["Retry-After", "60"]]);
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
    const url = first.get("u") || first.get("url") || env.header("referer");
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
    try {
      await write(cfg, "request", rec, env);
    } catch (exc) {
      if (!(exc instanceof StorageError)) throw exc;
      log.error("storage unavailable (pixel)", exc);
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
      payload = "(window.tidesight=window.tidesight||function(){(tidesight.q=tidesight.q||[])" +
        `.push(arguments)})("_config",${pub.body});\n`;
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
      log.error("configuration unavailable", exc);
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
      log.error(`unhandled error on ${method} ${path}`, exc);
      if (path === "/p.gif") return respond(200, gifHeaders(), GIF);
      return respond(500, [["Content-Type", "text/plain"]], "error\n");
    }
    return notFound();
  }

  return { fetch };
}
