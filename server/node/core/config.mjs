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
import { parseNetwork } from "./anonymize.mjs";
import { patterns } from "./redact.mjs";

export const FORMAT = "tidesight-resolved-config/1";

export const SERVER_DEFAULTS = {
  data_dir: "", storage_mode: "sqlite", journal_mode: "wal", synchronous: "normal", busy_timeout_ms: 10000,
  max_payload_kb: 64, max_chunk_kb: 1024, max_decompressed_kb: 256, max_events_per_request: 200,
  // Abuse and flood protection (docs/configuration.md, "Limits and abuse protection"); 0 disables one.
  rate_limit_per_minute: 600, rate_limit_kb_per_minute: 2048, chunk_rate_limit_per_minute: 120,
  chunk_kb_per_minute: 8192, flagged_per_minute: 30, flagged_global_per_minute: 300,
  rate_limit_key: "remote_addr", rate_limit_proxies: [], rate_limit_ipv6_prefix: 64, min_free_disk_mb: 512,
  cors: "reflect", accept_ch: true, public_url: "",
  secret: "", static_dir: "", dashboard_dir: "", store_raw_body: false, spool_fallback: true, log_file: "",
};

// config.SITE_DEFAULTS["max_hits_per_day"] (off), for a resolved file written before the key existed.
const MAX_HITS_PER_DAY = 0;

const HEADERS_DEFAULTS = {
  secret_patterns: ["*token*", "*key*", "*secret*", "*password*", "*auth*", "*session*", "*jwt*", "*assertion*",
    "*credential*", "*signature*", "*cookie*"],
  drop: [], cookie_names: ["ts_*"],
};

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
  /**
   * @param {object} [fallbackFeatures]  the features of the site's public document, for a resolved file written
   *                                     before site documents carried `features` (they are the same object)
   */
  constructor(id, doc, globalPrivacy, fallbackFeatures = null) {
    this.id = id;
    this.name = doc.name || id;
    this.origins = (doc.origins || []).map((o) => normalizeOrigin(String(o)));
    this.timezone = doc.timezone ?? "UTC";
    const maxHits = Number(doc.max_hits_per_day ?? MAX_HITS_PER_DAY);
    this.settings = {
      server_cookie_domain: doc.server_cookie_domain ?? "", cookie_days: doc.cookie_days ?? 400,
      max_hits_per_day: Number.isFinite(maxHits) ? Math.max(0, Math.trunc(maxHits)) : 0,
    };
    const features = doc.features ?? fallbackFeatures;
    this.features = features && typeof features === "object" ? { ...features } : {};
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

    const publicDocs = doc.get("public");
    const publicDefault = doc.get("public_default");
    if (!(publicDocs instanceof Map) || !(publicDefault instanceof Map)) {
      throw new ConfigError('"public" and "public_default" are missing: generate the file with `tidesight.jobs config-json`');
    }
    this._publicDocs = publicDocs;
    this._publicDefault = publicDefault;
    this._publicCache = new Map();
    this._publicOverrides = null;
    this._redactCache = new Map();

    const sites = doc.get("sites") ?? new Map();
    if (!(sites instanceof Map)) throw new ConfigError('"sites" must be an object');
    this.sites = new Map();
    for (const [id, siteDoc] of sites) {
      if (!(siteDoc instanceof Map)) throw new ConfigError(`sites.${id} must be an object`);
      const pub = publicDocs.get(id);
      const pubFeatures = pub instanceof Map && pub.get("features") instanceof Map ? plain(pub.get("features")) : null;
      this.sites.set(id, new SiteConfig(id, plain(siteDoc), this.privacy, pubFeatures));
    }

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
   * A site's URL redaction patterns, {params, paths}: the redact_params and redact_paths of its public document
   * (public_default for an unknown site), the same lists the tracker receives; strings only (redact.patterns).
   */
  redactRules(site) {
    const id = site ? site.id : "";
    let rules = this._redactCache.get(id);
    if (!rules) {
      let doc = site ? this._publicDocs.get(site.id) : null;
      if (!(doc instanceof Map)) doc = this._publicDefault;
      rules = { params: patterns(doc.get("redact_params")), paths: patterns(doc.get("redact_paths")) };
      this._redactCache.set(id, rules);
    }
    return rules;
  }

  /**
   * server.rate_limit_proxies as networks (the resolved JSON already expands "cloudflare" to CIDRs): the proxies
   * whose rate_limit_key header is believed. Invalid entries are skipped, as Python's networks() skips them.
   */
  proxyNetworks() {
    const list = Array.isArray(this.server.rate_limit_proxies) ? this.server.rate_limit_proxies : [];
    const key = JSON.stringify(list);
    if (this._proxyKey !== key) {
      this._proxyKey = key;
      this._proxyNets = list.map(parseNetwork).filter(Boolean);
    }
    return this._proxyNets;
  }

  /**
   * Change keys of every public document, for a value the platform cannot honour as configured (the Worker caps
   * max_payload_kb, announced as max_kb). The version hash is recomputed as Python computes it.
   */
  overridePublic(values) {
    this._publicOverrides = { ...(this._publicOverrides || {}), ...values };
    this._publicCache.clear();
  }

  /**
   * Config.public_site_config(site_id), serialised: {cv, body} where body is
   * json.dumps(public, separators=(",", ":"), sort_keys=True).
   */
  async publicSiteConfig(siteId) {
    const cached = this._publicCache.get(siteId);
    if (cached) return cached;
    let doc;
    let rehash = false;
    if (siteId === "") {
      doc = this._publicDefault;
    } else if (this.sites.has(siteId) && this._publicDocs.get(siteId) instanceof Map) {
      doc = this._publicDocs.get(siteId);
    } else {
      // An unknown site: the defaults with this id, and the version hash computed as Python does.
      doc = new Map(this._publicDefault);
      doc.set("site", siteId);
      doc.set("known", this.sites.has(siteId));
      rehash = true;
    }
    if (this._publicOverrides) {
      if (!rehash) doc = new Map(doc);
      for (const [key, value] of Object.entries(this._publicOverrides)) doc.set(key, value);
      rehash = true;
    }
    if (rehash) {
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
