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
 *
 * REMOTE_ADDR is the socket peer. Forwarding headers (X-Forwarded-For, Forwarded, ...) are stored raw with
 * every other header and resolved at enrichment ([enrich] ip_header / trusted_proxies); they are never trusted
 * at ingest.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { createCollector } from "./core/collector.mjs";
import { parseConfig } from "./core/config.mjs";
import { VERSION } from "./core/version.mjs";
import { remoteAddress, sendError, sendResponse, toFetchRequest } from "./http-adapter.mjs";
import { createLogger } from "./log.mjs";
import { openStore } from "./storage.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const SERVER_SOFTWARE = `Tidesight-Node/${VERSION} Node.js/${process.version}`;

function usage(code, message = "") {
  const text = (message ? message + "\n\n" : "") +
    "usage: node server.mjs --port 8080 --config tidesight.json [--host 127.0.0.1] [--data DIR]\n" +
    "                       [--scheme http|https] [--base-path /prefix]\n";
  (code ? process.stderr : process.stdout).write(text);
  process.exit(code);
}

function parseArgs(argv) {
  const opts = {
    port: process.env.PORT || "8080", host: process.env.HOST || "127.0.0.1",
    config: process.env.TIDESIGHT_CONFIG || "", data: "", scheme: "", basePath: "",
  };
  const names = { "--port": "port", "--host": "host", "--config": "config", "--data": "data", "--scheme": "scheme",
    "--base-path": "basePath" };
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
  constructor(log) {
    this.log = log;
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
        s.spool_fallback, this.log);
      this.key = key;
    }
    return this.store;
  }

  close() {
    if (this.store) this.store.close();
    this.store = null;
  }
}

async function readStatic(cfg, parts) {
  const root = cfg.server.static_dir || path.join(REPO_ROOT, "tracker", "build");
  const file = path.join(root, ...parts);
  try {
    if (!(await fs.promises.stat(file)).isFile()) return null;
    return new Uint8Array(await fs.promises.readFile(file));
  } catch {
    return null;
  }
}

function hostName(req) {
  const host = req.headers.host;
  if (!host) return os.hostname();
  return host.startsWith("[") ? host.slice(0, host.indexOf("]") + 1) : host.split(":")[0];
}

function main() {
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
  const stores = new StoreHolder(log.child("tidesight.storage"));
  const collector = createCollector({
    connector: "node",
    getConfig: () => holder.get(),
    getStore: (cfg) => stores.get(cfg),
    log: log.child("tidesight.collect"),
    readStatic,
  });

  const server = http.createServer({ requestTimeout: 120000, headersTimeout: 70000, keepAliveTimeout: 65000 },
    async (req, res) => {
      try {
        const cfg = holder.get();
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
            server_port: req.socket.localPort,
            remote_port: req.socket.remotePort,
            server_software: SERVER_SOFTWARE,
          },
        });
        await sendResponse(req, res, response);
      } catch (exc) {
        log.error("request failed", exc);
        sendError(res);
      }
    });
  server.on("clientError", (err, socket) => {
    if (socket.writable) socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n");
    else socket.destroy();
  });

  const shutdown = () => {
    server.close();
    stores.close();
    setTimeout(() => process.exit(0), 2000).unref();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  server.listen(Number(opts.port), opts.host, () => {
    const addr = server.address();
    const where = typeof addr === "string" ? addr : `http://${opts.host.includes(":") ? `[${opts.host}]` : opts.host}:${addr.port}`;
    process.stdout.write(`Tidesight Node collector on ${where} (data: ${holder.config.server.data_dir})\n`);
  });
}

main();
