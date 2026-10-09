/**
 * IP address anonymisation, identical in output to server/python/tidesight/anonymize.py (runtime-neutral), and
 * the address and network parsing it needs (Python's ipaddress module; enrich/ip.py's in_networks).
 *
 * Address parsing follows Python's ipaddress module (strict dotted quads without leading zeros, IPv6 with an
 * optional IPv4 tail) and IPv6 output uses its compressed form. Hashing is HMAC-SHA256 through Web Crypto.
 */

import { pyStrip } from "./pycompat.mjs";

// Request headers that can carry client IP addresses.
export const IP_HEADERS = [
  "x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "true-client-ip",
  "x-client-ip", "x-cluster-client-ip", "fastly-client-ip", "x-original-forwarded-for",
  "x-forwarded", "forwarded-for", "x-appengine-user-ip", "cf-connecting-ipv6", "via",
];
export const IP_HEADER_PARTS = ["ip", "forwarded", "client", "addr"];

export const HASH_PREFIX = "h:";

const IP_TOKEN = /\[?([0-9A-Fa-f:.]{2,45}(?:%[0-9A-Za-z]+)?)\]?/g;

/** IPv4Address(s) → 32-bit value, or null when Python would raise. */
export function parseIPv4(s) {
  if (!s) return null;
  const octets = s.split(".");
  if (octets.length !== 4) return null;
  let value = 0;
  for (const o of octets) {
    if (!o || !/^[0-9]+$/.test(o) || o.length > 3) return null;
    if (o !== "0" && o[0] === "0") return null; // leading zeros are refused
    const n = Number(o);
    if (n > 255) return null;
    value = value * 256 + n;
  }
  return value;
}

function parseHextet(h) {
  if (!h || h.length > 4 || !/^[0-9A-Fa-f]+$/.test(h)) return null;
  return parseInt(h, 16);
}

/** IPv6Address(s) (without a scope) → 128-bit BigInt, or null when Python would raise. */
export function parseIPv6(s) {
  if (!s) return null;
  const parts = s.split(":");
  if (parts.length < 3) return null;
  if (parts[parts.length - 1].includes(".")) {
    const v4 = parseIPv4(parts.pop());
    if (v4 === null) return null;
    parts.push(Math.floor(v4 / 65536).toString(16), (v4 % 65536).toString(16));
  }
  if (parts.length > 9) return null;
  let skip = -1;
  for (let i = 1; i < parts.length - 1; i++) {
    if (!parts[i]) {
      if (skip >= 0) return null; // at most one '::'
      skip = i;
    }
  }
  let hi;
  let lo;
  let skipped;
  if (skip >= 0) {
    hi = skip;
    lo = parts.length - skip - 1;
    if (!parts[0]) {
      hi -= 1;
      if (hi) return null;
    }
    if (!parts[parts.length - 1]) {
      lo -= 1;
      if (lo) return null;
    }
    skipped = 8 - (hi + lo);
    if (skipped < 1) return null;
  } else {
    if (parts.length !== 8 || !parts[0] || !parts[7]) return null;
    hi = 8;
    lo = 0;
    skipped = 0;
  }
  let value = 0n;
  for (let i = 0; i < hi; i++) {
    const h = parseHextet(parts[i]);
    if (h === null) return null;
    value = (value << 16n) | BigInt(h);
  }
  value <<= BigInt(16 * skipped);
  for (let i = parts.length - lo; i < parts.length; i++) {
    const h = parseHextet(parts[i]);
    if (h === null) return null;
    value = (value << 16n) | BigInt(h);
  }
  return value;
}

/** IPv6Address(s) → 128-bit BigInt, or null when Python would raise: a %scope suffix is accepted and ignored. */
export function parseIPv6Scoped(s) {
  if (!s || s.includes("/")) return null;
  const pct = s.indexOf("%");
  if (pct < 0) return parseIPv6(s);
  const scope = s.slice(pct + 1);
  if (!scope || scope.includes("%")) return null; // _split_scope_id
  return parseIPv6(s.slice(0, pct));
}

/** str(IPv4Address(v)) */
export function v4String(v) {
  return [Math.floor(v / 16777216) % 256, Math.floor(v / 65536) % 256, Math.floor(v / 256) % 256, v % 256].join(".");
}

/** str(IPv6Address(v)): lower-case hextets, the longest run of zeros (>= 2) compressed to '::'. */
export function v6String(v) {
  let hextets = [];
  for (let shift = 112n; shift >= 0n; shift -= 16n) hextets.push(Number((v >> shift) & 0xffffn).toString(16));
  let bestStart = -1;
  let bestLen = 0;
  let start = -1;
  let len = 0;
  hextets.forEach((h, i) => {
    if (h === "0") {
      len += 1;
      if (start === -1) start = i;
      if (len > bestLen) {
        bestLen = len;
        bestStart = start;
      }
    } else {
      len = 0;
      start = -1;
    }
  });
  if (bestLen > 1) {
    const bestEnd = bestStart + bestLen;
    if (bestEnd === hextets.length) hextets.push("");
    hextets.splice(bestStart, bestLen, "");
    if (bestStart === 0) hextets = [""].concat(hextets);
  }
  return hextets.join(":");
}

function isIp(token) {
  const base = token.split("%", 1)[0];
  return parseIPv4(base) !== null || parseIPv6(base) !== null;
}

/**
 * Keep the /24 network of an IPv4 address or the /48 of an IPv6 address (the /40 of a 6to4 address, 2002::/16,
 * whose next 32 bits are an IPv4 address: its /24, as for IPv4); anything else unchanged.
 */
export function truncateIp(ip) {
  const base = ip.split("%", 1)[0];
  let v4 = parseIPv4(base);
  if (v4 === null) {
    const v6 = parseIPv6(base);
    if (v6 === null) return ip;
    if ((v6 >> 32n) !== 0xffffn) {
      const hostBits = (v6 >> 112n) === 0x2002n ? 88n : 80n; // 6to4: /40, else /48
      return v6String(v6 & ~((1n << hostBits) - 1n));
    }
    v4 = Number(v6 & 0xffffffffn); // IPv4-mapped
  }
  return v4String(v4 - (v4 % 256));
}

const keyCache = new Map();
let syncHmacHex = null;

/**
 * Use a synchronous HMAC-SHA256 (fn(secret, text) → hex digest), e.g. node:crypto's createHmac: one async Web
 * Crypto call per IP token is costly when a header carries thousands of them. The Worker keeps Web Crypto.
 */
export function setSyncHmac(fn) {
  syncHmacHex = fn;
}

function hmacKey(secret) {
  let key = keyCache.get(secret);
  if (!key) {
    let raw = new TextEncoder().encode(secret);
    // Web Crypto refuses empty HMAC keys; HMAC zero-pads short keys, so 64 zero bytes give the same digest.
    if (!raw.length) raw = new Uint8Array(64);
    key = crypto.subtle.importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    if (keyCache.size > 16) keyCache.clear();
    keyCache.set(secret, key);
  }
  return key;
}

/**
 * A keyed, non-reversible stand-in for an IP address ('h:' + 16 hex chars). Synchronous (a string) when a
 * synchronous HMAC was set, else a promise: `await` it either way.
 */
export function hashIp(ip, secret) {
  if (ip.startsWith(HASH_PREFIX)) return ip;
  if (syncHmacHex) return HASH_PREFIX + syncHmacHex(secret, ip).slice(0, 16);
  return (async () => {
    const sig = await crypto.subtle.sign("HMAC", await hmacKey(secret), new TextEncoder().encode(ip));
    let hex = "";
    for (const b of new Uint8Array(sig, 0, 8)) hex += b.toString(16).padStart(2, "0");
    return HASH_PREFIX + hex;
  })();
}

/** Apply the configured transformations: truncate first, then hash. */
export async function anonymizeIp(ip, doHash, doTruncate, secret) {
  if (!ip) return ip;
  let out = ip;
  if (doTruncate) out = truncateIp(out);
  if (doHash) out = await hashIp(out, secret);
  return out;
}

/**
 * Replace every IP address inside a header value (lists, Forwarded syntax, ports). Synchronous (a string) when no
 * hashing is asked for or a synchronous HMAC was set; else a promise. `await` it either way.
 */
export function anonymizeHeaderValue(value, doHash, doTruncate, secret) {
  const pieces = [];
  let last = 0;
  let pending = false;
  for (const m of value.matchAll(IP_TOKEN)) {
    const token = m[1];
    let candidate = token;
    let port = "";
    if (candidate.split(":").length === 2 && candidate.includes(".")) {
      [candidate, port] = candidate.split(":");
      port = ":" + port;
    }
    pieces.push(value.slice(last, m.index));
    if (isIp(candidate)) {
      let anonymized = doTruncate ? truncateIp(candidate) : candidate;
      if (doHash) anonymized = hashIp(anonymized, secret);
      const whole = m[0];
      if (typeof anonymized === "string") {
        pieces.push(whole.split(token).join((anonymized || candidate) + port));
      } else {
        pending = true;
        pieces.push(anonymized.then((a) => whole.split(token).join((a || candidate) + port)));
      }
    } else {
      pieces.push(m[0]);
    }
    last = m.index + m[0].length;
  }
  pieces.push(value.slice(last));
  return pending ? Promise.all(pieces).then((p) => p.join("")) : pieces.join("");
}

/**
 * Whether a header (lower-case name) may carry client addresses: the known ones (Via included), and any name
 * containing "ip", "forwarded", "client" or "addr" (proxies and CDNs invent their own: x-originating-ip,
 * fly-client-ip, cloudfront-viewer-address, x-remote-addr...). Only tokens that parse as IP addresses change.
 */
export function ipBearing(name) {
  return IP_HEADERS.includes(name) || IP_HEADER_PARTS.some((part) => name.includes(part));
}

/** A copy of the header map (Map name → value) with addresses in IP-bearing headers anonymised. */
export async function anonymizeHeaders(headers, doHash, doTruncate, secret) {
  if (!(doHash || doTruncate)) return headers;
  const out = new Map(headers);
  for (const [name, value] of headers) {
    if (typeof value === "string" && ipBearing(name)) {
      const anonymized = anonymizeHeaderValue(value, doHash, doTruncate, secret);
      out.set(name, typeof anonymized === "string" ? anonymized : await anonymized);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Networks (ipaddress.ip_network(strict=False) and enrich/ip.py's in_networks)
// ---------------------------------------------------------------------------

const ALL_ONES_V4 = 0xffffffffn;
const ALL_ONES_V6 = (1n << 128n) - 1n;

/** _prefix_from_prefix_string: ASCII digits only, 0..max; else null. */
function prefixFromString(s, max) {
  if (!/^[0-9]+$/.test(s)) return null;
  const n = Number(s);
  return n <= max ? n : null;
}

/** _prefix_from_ip_int: the prefix length of a mask of ones then zeroes, or null when they mix. */
function prefixFromInt(v, bits) {
  let trailing = 0;
  if (v === 0n) trailing = bits;
  else while (trailing < bits && ((v >> BigInt(trailing)) & 1n) === 0n) trailing += 1;
  const prefix = bits - trailing;
  return v >> BigInt(trailing) === (1n << BigInt(prefix)) - 1n ? prefix : null;
}

function maskOf(prefix, bits) {
  return ((1n << BigInt(prefix)) - 1n) << BigInt(bits - prefix);
}

/**
 * ipaddress.ip_network(s.strip(), strict=False) → {version, network, mask} (BigInts), or null where Python raises
 * ValueError. IPv4 accepts a prefix length, a netmask or a hostmask; IPv6 a prefix length (and a %scope).
 */
export function parseNetwork(cidr) {
  const parts = pyStrip(String(cidr)).split("/");
  if (parts.length > 2) return null;
  const [addr, mask] = parts;
  const v4 = parseIPv4(addr);
  if (v4 !== null) {
    let prefix = mask === undefined ? 32 : prefixFromString(mask, 32);
    if (prefix === null) { // a netmask, else a hostmask, in dotted-quad form
      const m = parseIPv4(mask);
      if (m === null) return null;
      prefix = prefixFromInt(BigInt(m), 32);
      if (prefix === null) prefix = prefixFromInt(BigInt(m) ^ ALL_ONES_V4, 32);
      if (prefix === null) return null;
    }
    const bits = maskOf(prefix, 32);
    return { version: 4, network: BigInt(v4) & bits, mask: bits };
  }
  const v6 = parseIPv6Scoped(addr);
  if (v6 === null) return null;
  const prefix = mask === undefined ? 128 : prefixFromString(mask, 128);
  if (prefix === null) return null;
  const bits = maskOf(prefix, 128) & ALL_ONES_V6;
  return { version: 6, network: v6 & bits, mask: bits };
}

/**
 * enrich/ip.py _parse: an address as a proxy or header gives it (quotes, "[v6]:port", "v4:port" and a %scope
 * ignored), IPv4-mapped IPv6 as IPv4 → {version, value} (BigInt), or null.
 */
export function hostAddress(ip) {
  if (!ip) return null;
  let s = pyStrip(ip).replace(/^"+|"+$/g, "");
  if (s.startsWith("[") && s.includes("]")) s = s.slice(1, s.indexOf("]"));
  else if (s.split(":").length === 2 && s.includes(".")) s = s.split(":")[0];
  s = s.split("%")[0];
  const v4 = parseIPv4(s);
  if (v4 !== null) return { version: 4, value: BigInt(v4) };
  const v6 = parseIPv6(s);
  if (v6 === null) return null;
  if (v6 >> 32n === 0xffffn) return { version: 4, value: v6 & ALL_ONES_V4 };
  return { version: 6, value: v6 };
}

/** enrich/ip.py in_networks: whether an address belongs to one of the parsed networks. */
export function inNetworks(ip, nets) {
  const addr = hostAddress(ip);
  return addr !== null && nets.some((n) => n.version === addr.version && (addr.value & n.mask) === n.network);
}
