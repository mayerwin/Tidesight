/**
 * Tidesight Cloudflare Worker connector: the ingest endpoints of docs/protocol.md on Workers, storing raw hits
 * in D1 with the exact schema of the Python collector (bindings EVENTS → events.db, REPLAY → replay.db).
 *
 * Configuration: the resolved JSON (`python -m tidesight.jobs config-json`) in the TIDESIGHT_CONFIG variable or
 * secret, or bundled with createWorker({config}). Optional TIDESIGHT_BASE_PATH mounts the endpoints under a
 * prefix (e.g. a route "example.com/_ts/*"). An ASSETS binding (Workers static assets) serves /ts.js and /ts/*.
 *
 * REMOTE_ADDR is CF-Connecting-IP: Cloudflare terminates the client's TCP connection and sets this header to
 * that peer's address (a client cannot forge it), so it is the socket address as Cloudflare saw it — what
 * REMOTE_ADDR means for the other connectors. Every other header, X-Forwarded-For included, is stored raw. When the
 * zone's Pseudo IPv4 setting overwrites CF-Connecting-IP with a stand-in from 240.0.0.0/4 for an IPv6 client,
 * Cloudflare puts the real address in CF-Connecting-IPv6, which is then REMOTE_ADDR.
 */

import { parseIPv4, parseIPv6Scoped } from "../../node/core/anonymize.mjs";
import { createCollector } from "../../node/core/collector.mjs";
import { ConfigError, parseConfig } from "../../node/core/config.mjs";
import { ErrorLog } from "../../node/core/errorlog.mjs";
import { D1Store } from "./d1-store.mjs";

// D1 stores at most 2,000,000 bytes per value or row: keep every body the collector accepts storable
// (with room for the other columns of the row).
const D1_LIMITS_KB = { max_payload_kb: 1800, max_decompressed_kb: 1800, max_chunk_kb: 1800 };

// Request metadata from Cloudflare kept with the request (server.cf); nothing finer than the country.
const CF_FIELDS = ["colo", "country", "continent", "isEUCountry", "timezone", "asn", "asOrganization", "clientTcpRtt",
  "httpProtocol", "tlsVersion", "tlsCipher", "requestPriority"];

const log = {
  info: (msg) => console.log(`tidesight INFO ${msg}`),
  warning: (msg) => console.warn(`tidesight WARNING ${msg}`),
  error: (msg, exc) => console.error(`tidesight ERROR ${msg}${exc ? `: ${(exc && exc.stack) || exc}` : ""}`),
};

function loadConfig(source) {
  const cfg = parseConfig(source);
  if (!cfg.secret) {
    throw new ConfigError("server.secret is empty: generate the configuration with `tidesight.jobs config-json`");
  }
  for (const [key, max] of Object.entries(D1_LIMITS_KB)) {
    if (cfg.server[key] > max) {
      log.warning(`server.${key} = ${cfg.server[key]} exceeds what D1 can store in one row; using ${max}`);
      cfg.server[key] = max;
      // /config.js announces max_payload_kb as max_kb, so that trackers keep their requests under it.
      if (key === "max_payload_kb") cfg.overridePublic({ max_kb: max });
    }
  }
  return cfg;
}

function basePath(value) {
  if (!value) return "";
  const p = "/" + String(value).replace(/^\/+|\/+$/g, "");
  return p === "/" ? "" : p;
}

/**
 * The client's address: CF-Connecting-IP, unless it is one of the Class E stand-ins (240.0.0.0/4) that Pseudo IPv4
 * ("Overwrite headers") gives IPv6 clients, in which case Cloudflare's CF-Connecting-IPv6 holds the real one.
 */
export function clientAddress(headers) {
  const ip = headers.get("cf-connecting-ip");
  const v4 = ip ? parseIPv4(ip.trim()) : null;
  if (v4 !== null && v4 >>> 28 === 0xf) {
    const v6 = headers.get("cf-connecting-ipv6");
    if (v6 && parseIPv6Scoped(v6.trim()) !== null) return v6.trim();
  }
  return ip;
}

function cfInfo(cf) {
  if (!cf || typeof cf !== "object") return null;
  const out = {};
  for (const key of CF_FIELDS) {
    const value = cf[key];
    if (value !== undefined && value !== null && value !== "") out[key] = value;
  }
  return Object.keys(out).length ? out : null;
}

/**
 * Build a Worker. Without options the configuration comes from env.TIDESIGHT_CONFIG; pass {config} (the
 * resolved JSON as text, or an object) to bundle it instead.
 */
export function createWorker({ config: bundled = null } = {}) {
  let cachedSource;
  let cachedConfig = null;
  let state = null; // {events, replay, store, collector}

  const getConfig = (env) => {
    const source = bundled ?? env.TIDESIGHT_CONFIG;
    if (source === undefined || source === null || source === "") throw new ConfigError("TIDESIGHT_CONFIG is not set");
    if (source !== cachedSource) {
      cachedConfig = loadConfig(source);
      cachedSource = source;
    }
    return cachedConfig;
  };

  const getState = (env) => {
    if (!state || state.events !== env.EVENTS || state.replay !== env.REPLAY) {
      const errors = new ErrorLog(log); // one throttle for the isolate's logged errors, collector and store
      const store = new D1Store(env.EVENTS, env.REPLAY, log, errors);
      const collector = createCollector({
        connector: "worker",
        getConfig: () => getConfig(env),
        getStore: () => store,
        log,
        errors,
        readStatic: env.ASSETS ? async (cfg, parts) => {
          const res = await env.ASSETS.fetch(new Request(new URL("/" + parts.join("/"), "https://assets.invalid")));
          return res.ok ? new Uint8Array(await res.arrayBuffer()) : null;
        } : null,
      });
      state = { events: env.EVENTS, replay: env.REPLAY, store, collector };
    }
    return state;
  };

  return {
    async fetch(request, env) {
      if (!env || !env.EVENTS || !env.REPLAY) {
        log.error("the D1 bindings EVENTS and REPLAY are missing (see wrangler.toml.example)");
        return new Response("configuration error\n", { status: 503, headers: { "Content-Type": "text/plain" } });
      }
      const url = new URL(request.url);
      const https = url.protocol === "https:";
      const cf = request.cf || {};
      return getState(env).collector.fetch(request, {
        remoteAddr: clientAddress(request.headers),
        scheme: url.protocol.slice(0, -1),
        basePath: basePath(env.TIDESIGHT_BASE_PATH),
        server: {
          server_protocol: cf.httpProtocol,
          https: https ? "on" : "",
          ssl_protocol: cf.tlsVersion,
          ssl_cipher: cf.tlsCipher,
          server_name: url.hostname,
          server_port: url.port || (https ? "443" : "80"),
          server_software: "Cloudflare-Workers",
          cf: cfInfo(request.cf),
        },
      });
    },
  };
}

export default createWorker();
