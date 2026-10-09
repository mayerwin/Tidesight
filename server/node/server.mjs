#!/usr/bin/env node
/**
 * Tidesight Node connector: the ingest endpoints (docs/protocol.md) on Node's http module, storing raw hits in
 * SQLite through node:sqlite. Same rows, same schema and same responses as the Python collector.
 *
 *   node server.mjs --port 8080 --config tidesight.json [--host 127.0.0.1] [--data DIR]
 *                   [--scheme http|https] [--base-path /prefix]
 *
 *   --config     the resolved configuration (python -m tidesight.jobs --config tidesight.toml config-json
 *                --out tidesight.json); default $TIDESIGHT_CONFIG, else ./tidesight.json. Reloaded when it changes.
 *   --host       interface to listen on (default $HOST or 127.0.0.1: put a reverse proxy in front)
 *   --port       default $PORT or 8080
 *   --data       data directory, overriding server.data_dir from the configuration
 *   --scheme     the scheme browsers use to reach the collector; "https" when a TLS-terminating proxy is in
 *                front (marks the ts_svid cookie Secure). Default: from server.public_url, else http
 *   --base-path  a mount prefix the proxy does not strip (e.g. /stats)
 *   --max-connections  open connections at most (default 1000; further ones are closed at once)
 *
 * REMOTE_ADDR is the socket peer. Forwarding headers (X-Forwarded-For, Forwarded, ...) are stored raw with
 * every other header and resolved at enrichment ([enrich] ip_header / trusted_proxies); they are never trusted
 * at ingest (rate_limit_key is believed only from rate_limit_proxies).
 *
 * A public endpoint must hold out against slow and greedy clients by itself: requests time out (headers 15 s,
 * whole request 30 s, a silent socket 30 s), a connection may have at most 4 pipelined requests in flight, a
 * client at most 32 requests that can carry a body, and the request bodies being read at most 48 MiB together (one
 * buffer each, grown as bytes arrive). Beyond those, 503 with Retry-After or a closed connection. Tracker files are
 * served from memory. A reverse proxy in front (nginx, buffering request bodies) is still recommended.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import zlib from "node:zlib";

import { setSyncHmac } from "./core/anonymize.mjs";
import { clientKeyFor, createCollector } from "./core/collector.mjs";
import { parseConfig } from "./core/config.mjs";
import { ErrorLog } from "./core/errorlog.mjs";
import { setNativeZlib } from "./core/gzip.mjs";
import { VERSION } from "./core/version.mjs";
import { remoteAddress, sendError, sendResponse, toFetchRequest } from "./http-adapter.mjs";
import { createLogger } from "./log.mjs";
import { openStore } from "./storage.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const SERVER_SOFTWARE = `Tidesight-Node/${VERSION} Node.js/${process.version}`;

const HEADERS_TIMEOUT_MS = 15000; // to receive a request's headers
const REQUEST_TIMEOUT_MS = 30000; // to receive a whole request
const SOCKET_TIMEOUT_MS = 30000; // a socket with no traffic either way (a client that never reads its responses)
const KEEP_ALIVE_MS = 65000; // idle keep-alive: longer than the usual proxy idle timeouts
const MAX_PIPELINED = 4; // requests in flight on one connection; beyond, the connection is closed
const MAX_CLIENT_REQUESTS = 32; // requests in flight from one client (its allowance identity); beyond, 503
const MAX_BODY_MEMORY = 48 * 1024 * 1024; // request bodies being read, all together; beyond, 503
const BUSY_RETRY_AFTER = "10";
const STATIC_CHECK_MS = 2000; // a cached tracker file is checked against the disk at most this often
const MAX_STATIC_BYTES = 8 * 1024 * 1024;
const INFLIGHT = Symbol("tidesight.inflight");

function usage(code, message = "") {
  const text = (message ? message + "\n\n" : "") +
    "usage: node server.mjs --port 8080 --config tidesight.json [--host 127.0.0.1] [--data DIR]\n" +
    "                       [--scheme http|https] [--base-path /prefix] [--max-connections 1000]\n";
  (code ? process.stderr : process.stdout).write(text);
  process.exit(code);
}

function parseArgs(argv) {
  const opts = {
    port: process.env.PORT || "8080", host: process.env.HOST || "127.0.0.1",
    config: process.env.TIDESIGHT_CONFIG || "", data: "", scheme: "", basePath: "", maxConnections: "1000",
  };
  const names = { "--port": "port", "--host": "host", "--config": "config", "--data": "data", "--scheme": "scheme",
    "--base-path": "basePath", "--max-connections": "maxConnections" };
  for (let i = 0; i < argv.length; i++) {
    let flag = argv[i];
    let value = null;
    if (flag === "-h" || flag === "--help") usage(0);
    const eq = flag.indexOf("=");
    if (flag.startsWith("--") && eq > 0) {
      value = flag.slice(eq + 1);
      flag = flag.slice(0, eq);
    }
    if (!(flag in names)) usage(2, `unknown option ${flag}`);
    if (value === null) {
      if (i + 1 >= argv.length) usage(2, `${flag} needs a value`);
      value = argv[++i];
    }
    opts[names[flag]] = value;
  }
  if (!/^\d+$/.test(String(opts.port))) usage(2, `invalid port ${opts.port}`);
  if (!/^[1-9]\d*$/.test(String(opts.maxConnections))) usage(2, `invalid --max-connections ${opts.maxConnections}`);
  if (opts.scheme && !["http", "https"].includes(opts.scheme)) usage(2, "--scheme must be http or https");
  if (opts.basePath) opts.basePath = "/" + opts.basePath.replace(/^\/+|\/+$/g, "");
  if (opts.basePath === "/") opts.basePath = "";
  opts.config = opts.config || "tidesight.json";
  if (/\.toml$/i.test(opts.config)) {
    usage(2, `${opts.config} is a TOML file; the Node connector reads the resolved JSON:\n` +
      `  python -m tidesight.jobs --config ${opts.config} config-json --out tidesight.json`);
  }
  return opts;
}

/** config._load_or_create_secret: the deployment secret persisted in <data_dir>/.secret. */
function loadOrCreateSecret(dataDir, log) {
  const file = path.join(dataDir, ".secret");
  try {
    const value = fs.readFileSync(file, "ascii").trim();
    if (value) return value;
  } catch {
    // create it
  }
  const value = crypto.randomBytes(32).toString("hex");
  try {
    fs.mkdirSync(dataDir, { recursive: true });
    fs.writeFileSync(file, value, { encoding: "ascii", mode: 0o600, flag: "wx" });
  } catch (exc) {
    if (exc.code === "EEXIST") return fs.readFileSync(file, "ascii").trim();
    log.warning(`could not persist the generated secret in ${dataDir}: ${exc.message}`);
  }
  return value;
}

/** Holds the current configuration and reloads it when the file changes (checked every few seconds). */
class ConfigHolder {
  constructor(file, dataOverride, log) {
    this.file = path.resolve(file);
    this.dataOverride = dataOverride ? path.resolve(dataOverride) : "";
    this.log = log;
    this.config = this.load();
    this.mtime = this.stat();
    this.checked = Date.now();
  }

  stat() {
    try {
      return fs.statSync(this.file).mtimeMs;
    } catch {
      return null;
    }
  }

  load() {
    const cfg = parseConfig(fs.readFileSync(this.file, "utf8"), { dataDir: this.dataOverride || undefined });
    let dir = cfg.server.data_dir || "data";
    if (dir === "~" || dir.startsWith("~/") || dir.startsWith("~\\")) dir = path.join(os.homedir(), dir.slice(1));
    cfg.server.data_dir = path.resolve(path.dirname(this.file), dir); // relative to the config file, as in Python
    if (!cfg.secret) cfg.secret = loadOrCreateSecret(cfg.server.data_dir, this.log);
    return cfg;
  }

  get() {
    const now = Date.now();
    if (now - this.checked < 5000) return this.config;
    this.checked = now;
    const mtime = this.stat();
    if (mtime !== null && mtime !== this.mtime) {
      try {
        this.config = this.load();
        this.log.info(`config reloaded from ${this.file}`);
      } catch (exc) {
        this.log.error(`config reload failed, keeping the previous one: ${exc.message}`);
      }
      this.mtime = mtime;
    }
    return this.config;
  }
}

/** The store for the current storage settings (re-created when they change, as Collector.store does). */
class StoreHolder {
  constructor(log, errors) {
    this.log = log;
    this.errors = errors;
    this.store = null;
    this.key = null;
  }

  get(cfg) {
    const s = cfg.server;
    const key = JSON.stringify([s.data_dir, s.storage_mode, s.journal_mode, s.synchronous, s.busy_timeout_ms,
      s.spool_fallback]);
    if (this.store === null || key !== this.key) {
      if (this.store) this.store.close();
      this.store = openStore(s.data_dir, s.storage_mode, s.journal_mode, s.synchronous, s.busy_timeout_ms,
        s.spool_fallback, this.log, this.errors);
      this.key = key;
    }
    return this.store;
  }

  close() {
    if (this.store) this.store.close();
    this.store = null;
  }
}

/**
 * Free bytes for this process on the file system holding the data directory, or its nearest existing ancestor
 * (shutil.disk_usage(path).free in Python); null when it cannot be read.
 */
function diskFree(dataDir) {
  let dir = path.resolve(dataDir || ".");
  while (!fs.existsSync(dir) && path.dirname(dir) !== dir) dir = path.dirname(dir);
  try {
    const st = fs.statfsSync(dir);
    return Number(st.bavail) * Number(st.bsize);
  } catch {
    return null;
  }
}

const staticCache = new Map(); // file → {checked, mtimeMs, size, data}

/**
 * A tracker file, from memory: read once, then re-checked against the disk (size and mtime) at most every 2 s.
 * Every response shares the one buffer, and no file descriptor is opened per request.
 */
async function readStatic(cfg, parts) {
  const root = cfg.server.static_dir || path.join(REPO_ROOT, "tracker", "build");
  const file = path.join(root, ...parts);
  const now = Date.now();
  const cached = staticCache.get(file);
  if (cached && now - cached.checked < STATIC_CHECK_MS) return cached.data;
  try {
    const st = await fs.promises.stat(file);
    if (!st.isFile() || st.size > MAX_STATIC_BYTES) {
      staticCache.delete(file);
      return null;
    }
    if (cached && cached.mtimeMs === st.mtimeMs && cached.size === st.size) {
      cached.checked = now;
      return cached.data;
    }
    const buf = await fs.promises.readFile(file);
    const data = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
    if (staticCache.size >= 256) staticCache.clear();
    staticCache.set(file, { checked: now, mtimeMs: st.mtimeMs, size: st.size, data });
    return data;
  } catch {
    staticCache.delete(file);
    return null;
  }
}

/** The raw header fields as [lower-case name, value], for the client identity. */
function headerFields(req) {
  const raw = req.rawHeaders;
  const out = [];
  for (let i = 0; i + 1 < raw.length; i += 2) out.push([raw[i].toLowerCase(), raw[i + 1]]);
  return out;
}

/** 503 without reading the request: too many requests in flight from this client. */
function refuseBusy(res) {
  if (res.headersSent || res.destroyed) return;
  res.writeHead(503, { "Retry-After": BUSY_RETRY_AFTER, "Cache-Control": "no-store", Connection: "close",
    "Content-Length": "0" });
  res.end();
  res.once("finish", () => res.socket && res.socket.destroy());
}

function hostName(req) {
  const host = req.headers.host;
  if (!host) return os.hostname();
  return host.startsWith("[") ? host.slice(0, host.indexOf("]") + 1) : host.split(":")[0];
}

function main() {
  // What this process creates (databases, spool files, the secret, the log file) is readable by its owner only,
  // as Python's wsgi.create_app and jobs.main arrange.
  try {
    process.umask(0o077);
  } catch {
    // not available here (a worker thread): files keep the modes they are created with
  }
  // Node's own HMAC and zlib: allowance keys and hashed addresses computed synchronously, and gzip bodies decoded
  // as Python decodes them (the first member only, its CRC-32 and length checked, zlib's error texts).
  setSyncHmac((secret, text) => crypto.createHmac("sha256", secret).update(text).digest("hex"));
  setNativeZlib({ // the interface core/gzip.mjs documents
    inflateRaw(data, limit, window) {
      const r = zlib.inflateRawSync(data, { info: true, maxOutputLength: Math.max(1, limit),
        chunkSize: Math.max(64, window) });
      return { out: new Uint8Array(r.buffer.buffer, r.buffer.byteOffset, r.buffer.byteLength),
        consumed: r.engine.bytesWritten };
    },
    crc32: (bytes) => zlib.crc32(bytes),
  });

  const opts = parseArgs(process.argv.slice(2));
  let log = createLogger();
  let holder;
  try {
    holder = new ConfigHolder(opts.config, opts.data, log.child("tidesight.config"));
  } catch (exc) {
    process.stderr.write(`tidesight: cannot load ${opts.config}: ${exc.message}\n`);
    process.exit(1);
  }
  if (holder.config.server.log_file) log = createLogger(holder.config.server.log_file);
  holder.log = log.child("tidesight.config");
  // One throttle for every request-path exception of the process, the collector's and the store's (collect.ERRORS).
  const errors = new ErrorLog(log.child("tidesight.collect"));
  const stores = new StoreHolder(log.child("tidesight.storage"), errors);
  const collector = createCollector({
    connector: "node",
    getConfig: () => holder.get(),
    getStore: (cfg) => stores.get(cfg),
    log: log.child("tidesight.collect"),
    errors,
    readStatic,
    diskFree,
    maxBodyMemory: MAX_BODY_MEMORY,
  });
  const clients = new Map(); // client key → its requests in flight that may carry a body

  const server = http.createServer({
    requestTimeout: REQUEST_TIMEOUT_MS, headersTimeout: HEADERS_TIMEOUT_MS, keepAliveTimeout: KEEP_ALIVE_MS,
    connectionsCheckingInterval: 1000,
  }, async (req, res) => {
    const socket = req.socket;
    if (socket.destroyed) return;
    // Pipelined requests wait for the responses before them: a client that sends many and reads none would pin
    // them all. More than MAX_PIPELINED in flight on one connection closes it.
    socket[INFLIGHT] = (socket[INFLIGHT] || 0) + 1;
    res.once("close", () => {
      socket[INFLIGHT] -= 1;
    });
    if (socket[INFLIGHT] > MAX_PIPELINED) {
      socket.destroy();
      return;
    }
    let client = null;
    try {
      const cfg = holder.get();
      if (req.method !== "GET" && req.method !== "HEAD" && req.method !== "OPTIONS") {
        // A request body takes time and memory: one client (its allowance identity) gets MAX_CLIENT_REQUESTS.
        const key = clientKeyFor(cfg, remoteAddress(req), headerFields(req)) || "-";
        const n = (clients.get(key) || 0) + 1;
        if (n > MAX_CLIENT_REQUESTS) {
          refuseBusy(res);
          return;
        }
        clients.set(key, n);
        client = key;
      }
      const scheme = opts.scheme || (/^https:\/\//i.test(cfg.server.public_url || "") ? "https" : "http");
      const request = toFetchRequest(req, { scheme });
      const response = await collector.fetch(request, {
        remoteAddr: remoteAddress(req),
        rawTarget: req.url,
        rawHeaders: req.rawHeaders,
        method: req.method,
        scheme,
        basePath: opts.basePath,
        server: {
          server_protocol: `HTTP/${req.httpVersion}`,
          https: scheme === "https" ? "on" : "",
          server_name: hostName(req),
          server_port: socket.localPort,
          remote_port: socket.remotePort,
          server_software: SERVER_SOFTWARE,
        },
      });
      await sendResponse(req, res, response);
    } catch (exc) {
      errors.exception("request failed", exc);
      sendError(res);
    } finally {
      if (client !== null) {
        const n = clients.get(client) - 1;
        if (n > 0) clients.set(client, n);
        else clients.delete(client);
      }
    }
  });
  server.maxConnections = Number(opts.maxConnections); // further connections are closed as they arrive
  server.setTimeout(SOCKET_TIMEOUT_MS); // no 'timeout' listener: a socket silent this long is destroyed
  server.on("clientError", (err, socket) => {
    const pending = socket._httpMessage; // a response under way: never write another into it
    if (!socket.writable || (pending && pending.headersSent) || (err && err.code === "ECONNRESET")) {
      socket.destroy();
      return;
    }
    const status = err && err.code === "ERR_HTTP_REQUEST_TIMEOUT" ? "408 Request Timeout"
      : err && err.code === "HPE_HEADER_OVERFLOW" ? "431 Request Header Fields Too Large" : "400 Bad Request";
    socket.end(`HTTP/1.1 ${status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`, () => socket.destroy());
  });
  let listening = false;
  server.on("error", (exc) => {
    if (!listening) {
      process.stderr.write(`tidesight: cannot listen on ${opts.host}:${opts.port}: ${exc.message}\n`);
      process.exit(1);
    }
    errors.exception("server error", exc); // e.g. EMFILE on accept: the server keeps running
  });

  const shutdown = () => {
    server.close();
    stores.close();
    setTimeout(() => process.exit(0), 2000).unref();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  server.listen(Number(opts.port), opts.host, () => {
    listening = true;
    const addr = server.address();
    const where = typeof addr === "string" ? addr : `http://${opts.host.includes(":") ? `[${opts.host}]` : opts.host}:${addr.port}`;
    process.stdout.write(`Tidesight Node collector on ${where} (data: ${holder.config.server.data_dir})\n`);
  });
}

main();
