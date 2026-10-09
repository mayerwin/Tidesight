/**
 * IP address anonymisation, identical in output to server/python/tidesight/anonymize.py (runtime-neutral).
 *
 * Address parsing follows Python's ipaddress module (strict dotted quads without leading zeros, IPv6 with an
 * optional IPv4 tail) and IPv6 output uses its compressed form. Hashing is HMAC-SHA256 through Web Crypto.
 */

// Request headers that can carry client IP addresses.
export const IP_HEADERS = [
  "x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "true-client-ip",
  "x-client-ip", "x-cluster-client-ip", "fastly-client-ip", "x-original-forwarded-for",
  "x-forwarded", "forwarded-for", "x-appengine-user-ip", "cf-connecting-ipv6",
];

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

function v4String(v) {
  return [Math.floor(v / 16777216) % 256, Math.floor(v / 65536) % 256, Math.floor(v / 256) % 256, v % 256].join(".");
}

/** str(IPv6Address(v)): lower-case hextets, the longest run of zeros (>= 2) compressed to '::'. */
function v6String(v) {
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

/** Keep the /24 network of an IPv4 address or the /48 of an IPv6 address; anything else unchanged. */
export function truncateIp(ip) {
  const base = ip.split("%", 1)[0];
  let v4 = parseIPv4(base);
  if (v4 === null) {
    const v6 = parseIPv6(base);
    if (v6 === null) return ip;
    if (v6 >> 32n !== 0xffffn) return v6String(v6 & ~((1n << 80n) - 1n));
    v4 = Number(v6 & 0xffffffffn); // IPv4-mapped
  }
  return v4String(v4 - (v4 % 256));
}

const keyCache = new Map();

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

/** A keyed, non-reversible stand-in for an IP address ('h:' + 16 hex chars). */
export async function hashIp(ip, secret) {
  if (ip.startsWith(HASH_PREFIX)) return ip;
  const sig = await crypto.subtle.sign("HMAC", await hmacKey(secret), new TextEncoder().encode(ip));
  let hex = "";
  for (const b of new Uint8Array(sig, 0, 8)) hex += b.toString(16).padStart(2, "0");
  return HASH_PREFIX + hex;
}

/** Apply the configured transformations: truncate first, then hash. */
export async function anonymizeIp(ip, doHash, doTruncate, secret) {
  if (!ip) return ip;
  let out = ip;
  if (doTruncate) out = truncateIp(out);
  if (doHash) out = await hashIp(out, secret);
  return out;
}

/** Replace every IP address inside a header value (lists, Forwarded syntax, ports). */
export async function anonymizeHeaderValue(value, doHash, doTruncate, secret) {
  let out = "";
  let last = 0;
  for (const m of value.matchAll(IP_TOKEN)) {
    const token = m[1];
    let candidate = token;
    let port = "";
    if (candidate.split(":").length === 2 && candidate.includes(".")) {
      [candidate, port] = candidate.split(":");
      port = ":" + port;
    }
    let replacement = m[0];
    if (isIp(candidate)) {
      const anonymized = (await anonymizeIp(candidate, doHash, doTruncate, secret)) || candidate;
      replacement = m[0].split(token).join(anonymized + port);
    }
    out += value.slice(last, m.index) + replacement;
    last = m.index + m[0].length;
  }
  return out + value.slice(last);
}

/** A copy of the header map (Map name → value) with addresses in IP-bearing headers anonymised. */
export async function anonymizeHeaders(headers, doHash, doTruncate, secret) {
  if (!(doHash || doTruncate)) return headers;
  const out = new Map(headers);
  for (const name of IP_HEADERS) {
    const value = out.get(name);
    if (typeof value === "string") out.set(name, await anonymizeHeaderValue(value, doHash, doTruncate, secret));
  }
  return out;
}
