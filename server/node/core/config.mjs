/**
 * The resolved configuration both JavaScript connectors read (runtime-neutral).
 *
 * The TOML file stays the single source of truth: `python -m tidesight.jobs config-json --out tidesight.json`
 * writes Config.resolved() with every default and per-site layer already applied ("tidesight-resolved-config/1").
 * This module only reads that document. Parse it from text when you can: the text keeps Python's float
 * spelling (1.0 vs 1), which the config version hash (`cv`) of unknown sites depends on.
 */

import { loads, dumps, PyFloat } from "./pyjson.mjs";
import { fnmatchCompile, normalizeOrigin } from "./pycompat.mjs";

export const FORMAT = "tidesight-resolved-config/1";

export const SERVER_DEFAULTS = {
  data_dir: "", storage_mode: "sqlite", journal_mode: "wal", synchronous: "normal", busy_timeout_ms: 10000,
  max_payload_kb: 64, max_chunk_kb: 1024, max_decompressed_kb: 4096, max_events_per_request: 500,
  rate_limit_per_minute: 600, rate_limit_key: "remote_addr", cors: "reflect", accept_ch: true, public_url: "",
  secret: "", static_dir: "", dashboard_dir: "", store_raw_body: false, spool_fallback: true, log_file: "",
};

const HEADERS_DEFAULTS = { secret_patterns: ["*token*", "*key*", "*secret*", "*password*"], drop: [], cookie_names: ["ts_*"] };

const PRIVACY_DEFAULTS = { hash_ip: false, truncate_ip: false, honour_dnt: false, honour_gpc: false, drop_fingerprint: false };

const CHOICES = {
  storage_mode: ["sqlite", "spool"], journal_mode: ["wal", "delete"], synchronous: ["normal", "full"],
  cors: ["reflect", "allowed"],
};

export class ConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = "ConfigError";
  }
}

/** pyjson value → plain JavaScript (Maps become objects, PyFloat a number). */
function plain(v) {
  if (v instanceof Map) {
    const out = {};
    for (const [k, x] of v) out[k] = plain(x);
    return out;
  }
  if (Array.isArray(v)) return v.map(plain);
  if (v instanceof PyFloat) return v.value;
  if (typeof v === "bigint") return Number(v);
  return v;
}

/** Plain JavaScript (e.g. an imported JSON module) → pyjson values. Integral numbers become ints. */
function fromPlain(v) {
  if (Array.isArray(v)) return v.map(fromPlain);
  if (v !== null && typeof v === "object" && !(v instanceof PyFloat)) {
    return new Map(Object.entries(v).map(([k, x]) => [k, fromPlain(x)]));
  }
  if (typeof v === "number" && !Number.isInteger(v)) return new PyFloat(v);
  return v;
}

function typeOk(defaultValue, value) {
  if (typeof defaultValue === "boolean") return typeof value === "boolean";
  if (typeof defaultValue === "number") return typeof value === "number" && Number.isInteger(value);
  if (typeof defaultValue === "string") return typeof value === "string";
  if (Array.isArray(defaultValue)) return Array.isArray(value);
  return true;
}

function section(doc, name, defaults) {
  const raw = doc.get(name);
  if (raw !== undefined && !(raw instanceof Map)) throw new ConfigError(`"${name}" must be an object`);
  const values = raw ? plain(raw) : {};
  const out = { ...defaults };
  for (const [key, value] of Object.entries(values)) {
    if (!(key in defaults)) {
      out[key] = value;
      continue;
    }
    if (!typeOk(defaults[key], value)) throw new ConfigError(`${name}.${key}: unexpected value ${JSON.stringify(value)}`);
    if (CHOICES[key] && name === "server" && !CHOICES[key].includes(value)) {
      throw new ConfigError(`${name}.${key}: must be one of ${CHOICES[key].join(", ")}`);
    }
    out[key] = value;
  }
  return out;
}

/** Resolved configuration of one site (only what the collector needs). */
export class SiteConfig {
  constructor(id, doc, globalPrivacy) {
    this.id = id;
    this.name = doc.name || id;
    this.origins = (doc.origins || []).map((o) => normalizeOrigin(String(o)));
    this.timezone = doc.timezone ?? "UTC";
    this.settings = { server_cookie_domain: doc.server_cookie_domain ?? "", cookie_days: doc.cookie_days ?? 400 };
    this.privacy = { ...PRIVACY_DEFAULTS, ...globalPrivacy, ...(doc.privacy || {}) };
    this._wildcards = this.origins.map((p) => (p.includes("*") ? fnmatchCompile(p) : null));
  }

  /** True when the origin is allowed, or when the site lists no origins. */
  originAllowed(origin) {
    if (!this.origins.length) return true;
    if (!origin) return false;
    const o = normalizeOrigin(origin);
    for (let i = 0; i < this.origins.length; i++) {
      const pattern = this.origins[i];
      if (pattern === "*" || pattern === o) return true;
      if (this._wildcards[i] && this._wildcards[i].test(o)) return true;
    }
    return false;
  }
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  let hex = "";
  for (const b of new Uint8Array(digest)) hex += b.toString(16).padStart(2, "0");
  return hex;
}

export class Config {
  /**
   * @param {Map} doc  the parsed resolved-config document
   * @param {{dataDir?: string}} [options]  dataDir overrides server.data_dir (Node)
   */
  constructor(doc, { dataDir } = {}) {
    if (!(doc instanceof Map)) throw new ConfigError("the configuration must be a JSON object");
    const format = doc.get("format");
    if (format !== FORMAT) {
      throw new ConfigError(`unsupported configuration format ${JSON.stringify(format ?? null)}: expected "${FORMAT}" ` +
        "(generate it with `python -m tidesight.jobs --config tidesight.toml config-json --out tidesight.json`)");
    }
    this.server = section(doc, "server", SERVER_DEFAULTS);
    if (dataDir) this.server.data_dir = dataDir;
    this.headers = section(doc, "headers", HEADERS_DEFAULTS);
    this.privacy = section(doc, "privacy", PRIVACY_DEFAULTS);
    this.secret = this.server.secret;

    const sites = doc.get("sites") ?? new Map();
    if (!(sites instanceof Map)) throw new ConfigError('"sites" must be an object');
    this.sites = new Map();
    for (const [id, siteDoc] of sites) {
      if (!(siteDoc instanceof Map)) throw new ConfigError(`sites.${id} must be an object`);
      this.sites.set(id, new SiteConfig(id, plain(siteDoc), this.privacy));
    }

    const publicDocs = doc.get("public");
    const publicDefault = doc.get("public_default");
    if (!(publicDocs instanceof Map) || !(publicDefault instanceof Map)) {
      throw new ConfigError('"public" and "public_default" are missing: generate the file with `tidesight.jobs config-json`');
    }
    this._publicDocs = publicDocs;
    this._publicDefault = publicDefault;
    this._publicCache = new Map();

    this.headerRules = {
      secret: this.headers.secret_patterns.map((p) => fnmatchCompile(String(p).toLowerCase())),
      drop: new Set(this.headers.drop.map((d) => String(d).toLowerCase())),
      cookies: this.headers.cookie_names.map((p) => fnmatchCompile(String(p))),
    };
  }

  get dataDir() {
    return this.server.data_dir;
  }

  site(siteId) {
    if (!siteId) return null;
    return this.sites.get(siteId) || null;
  }

  /**
   * Config.public_site_config(site_id), serialised: {cv, body} where body is
   * json.dumps(public, separators=(",", ":"), sort_keys=True).
   */
  async publicSiteConfig(siteId) {
    const cached = this._publicCache.get(siteId);
    if (cached) return cached;
    let doc;
    if (siteId === "") {
      doc = this._publicDefault;
    } else if (this.sites.has(siteId) && this._publicDocs.get(siteId) instanceof Map) {
      doc = this._publicDocs.get(siteId);
    } else {
      // An unknown site: the defaults with this id, and the version hash computed as Python does.
      doc = new Map(this._publicDefault);
      doc.set("site", siteId);
      doc.set("known", this.sites.has(siteId));
      doc.delete("cv");
      const digest = await sha256Hex(dumps(doc, { sortKeys: true }));
      doc.set("cv", digest.slice(0, 12));
    }
    const entry = { cv: String(doc.get("cv")), body: dumps(doc, { itemSep: ",", keySep: ":", sortKeys: true }) };
    if (this._publicCache.size >= 512) this._publicCache.clear();
    this._publicCache.set(siteId, entry);
    return entry;
  }
}

/** Build a Config from the resolved JSON text (preferred) or an already parsed object. */
export function parseConfig(input, options = {}) {
  let doc;
  if (typeof input === "string") {
    try {
      doc = loads(input.charCodeAt(0) === 0xfeff ? input.slice(1) : input);
    } catch (exc) {
      throw new ConfigError(`invalid JSON: ${exc.message}`);
    }
  } else {
    doc = fromPlain(input);
  }
  return new Config(doc, options);
}
