/**
 * Small Python behaviours the reference collector relies on, reproduced exactly (runtime-neutral):
 * str.strip(), slicing by code point, the collector's _str()/_int() coercions, urllib.parse.parse_qs/unquote,
 * urlsplit() as used by origin_of(), normalize_origin() and fnmatch.fnmatchcase().
 */

import { PyFloat, floatRepr } from "./pyjson.mjs";

// Characters for which Python's str.isspace() is true (what strip() removes).
const PY_SPACE = new Set([0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f,
  0x205f, 0x3000]);

/** str.strip() */
export function pyStrip(s) {
  let a = 0;
  let b = s.length;
  while (a < b && PY_SPACE.has(s.charCodeAt(a))) a += 1;
  while (b > a && PY_SPACE.has(s.charCodeAt(b - 1))) b -= 1;
  return a === 0 && b === s.length ? s : s.slice(a, b);
}

/** str.rstrip() */
export function pyRstrip(s) {
  let b = s.length;
  while (b > 0 && PY_SPACE.has(s.charCodeAt(b - 1))) b -= 1;
  return b === s.length ? s : s.slice(0, b);
}

/** len(s) — Python counts code points (a surrogate pair is one). */
export function pyLen(s) {
  let n = s.length;
  for (let i = 0; i + 1 < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff && (s.charCodeAt(i + 1) & 0xfc00) === 0xdc00) {
      n -= 1;
      i += 1;
    }
  }
  return n;
}

/** s[:n] — Python slices by code point, never splitting a surrogate pair. */
export function pySlice(s, n) {
  if (s.length <= n) return s;
  let i = 0;
  let count = 0;
  while (i < s.length && count < n) {
    const c = s.charCodeAt(i);
    i += c >= 0xd800 && c <= 0xdbff && i + 1 < s.length && (s.charCodeAt(i + 1) & 0xfc00) === 0xdc00 ? 2 : 1;
    count += 1;
  }
  return s.slice(0, i);
}

/** str(value) for the numbers produced by pyjson (ints, BigInt, PyFloat). */
export function pyNumberStr(value) {
  if (value instanceof PyFloat) return floatRepr(value.value);
  if (typeof value === "bigint") return value.toString();
  if (Number.isInteger(value)) return Math.abs(value) < 1e21 ? String(value) : BigInt(value).toString();
  return floatRepr(value);
}

/** The collector's _str(value, max_len): strings and numbers as bounded text, anything else None. */
export function pyStrOf(value, maxLen) {
  if (typeof value === "string") return value ? pySlice(value, maxLen) : null;
  if (typeof value === "number" || typeof value === "bigint" || value instanceof PyFloat) {
    return pySlice(pyNumberStr(value), maxLen);
  }
  return null;
}

const INT64_MIN = -(2n ** 63n);
const INT64_MAX = 2n ** 63n - 1n;

function fromBig(big) {
  if (big < INT64_MIN || big > INT64_MAX) return null;
  return big >= -9007199254740991n && big <= 9007199254740991n ? Number(big) : big;
}

function floatToInt(f) {
  if (f !== f || !(Math.abs(f) < 9.2e18)) return null;
  const t = Math.trunc(f);
  return Number.isSafeInteger(t) ? t || 0 : BigInt(t);
}

const ND_RUN = /^\p{Nd}$/u;

// The runs of Unicode decimal digits (General_Category Nd, Unicode 16.0): [first code point, length]. Each run is
// made of whole sets of ten, zero first, so a digit's value is (cp - first) % 10.
const ND_RUNS = [0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66, 0xde6,
  0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90, 0x1b50, 0x1bb0, 0x1c40,
  0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10, 0x104a0, 0x10d30, 0x10d40, 0x11066,
  0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0, 0x11650, 0x116c0, [0x116d0, 20], 0x11730, 0x118e0,
  0x11950, 0x11bf0, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16130, 0x16a60, 0x16ac0, 0x16b50, 0x16d70, 0x1ccf0,
  [0x1d7ce, 50], 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e5f1, 0x1e950, 0x1fbf0].map((r) => (Array.isArray(r) ? r : [r, 10]));

/** Value of a Unicode decimal digit: a binary search in ND_RUNS (a walk back to the run's zero for a digit the
 * table does not know, from a newer Unicode than the table's). */
function decimalValue(cp) {
  let lo = 0;
  let hi = ND_RUNS.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ND_RUNS[mid][0] <= cp) lo = mid;
    else hi = mid - 1;
  }
  const [first, length] = ND_RUNS[lo];
  if (cp >= first && cp < first + length) return (cp - first) % 10;
  let start = cp;
  while (start > 0 && ND_RUN.test(String.fromCodePoint(start - 1))) start -= 1;
  return (cp - start) % 10;
}

const MAX_INT_DIGITS = 4300; // int() refuses longer digit strings (ValueError) since Python 3.10.7

/**
 * The collector's _int(value): ints within int64, floats truncated, digit strings parsed; else None.
 * Returns a number, or a BigInt beyond Number.MAX_SAFE_INTEGER.
 */
export function pyIntOf(value) {
  if (typeof value === "number") {
    if (Number.isInteger(value)) return Number.isSafeInteger(value) ? value || 0 : fromBig(BigInt(value));
    return floatToInt(value);
  }
  if (typeof value === "bigint") return fromBig(value);
  if (value instanceof PyFloat) return floatToInt(value.value);
  if (typeof value !== "string") return null; // booleans, None, lists, dicts
  // value.strip().lstrip("-").isdigit() and then int(value.strip()): one optional minus and decimal digits.
  const s = pyStrip(value);
  // Over 4300 digits int() refuses (→ None): decided on the length first, before any conversion work (a digit
  // is one or two UTF-16 units).
  if (s.length > 2 * MAX_INT_DIGITS + 1) return null;
  const m = /^(-?)(\p{Nd}+)$/u.exec(s);
  if (!m) return null;
  let digits = m[2];
  if (!/^[0-9]+$/.test(digits)) {
    if (pyLen(digits) > MAX_INT_DIGITS) return null;
    let ascii = "";
    for (const ch of digits) ascii += String(decimalValue(ch.codePointAt(0)));
    digits = ascii;
  }
  if (digits.length > MAX_INT_DIGITS) return null;
  return fromBig(BigInt(m[1] + digits));
}

// ---------------------------------------------------------------------------
// urllib.parse
// ---------------------------------------------------------------------------

const UTF8_REPLACE = new TextDecoder("utf-8", { ignoreBOM: true });

function isHex(c) {
  return (c >= 0x30 && c <= 0x39) || (c >= 0x41 && c <= 0x46) || (c >= 0x61 && c <= 0x66);
}

function unquoteAsciiRun(run, latin1) {
  if (!run.includes("%")) return run;
  const bytes = new Uint8Array(run.length);
  let n = 0;
  for (let i = 0; i < run.length; i++) {
    const c = run.charCodeAt(i);
    if (c === 0x25 && i + 2 < run.length && isHex(run.charCodeAt(i + 1)) && isHex(run.charCodeAt(i + 2))) {
      bytes[n++] = parseInt(run.slice(i + 1, i + 3), 16);
      i += 2;
    } else {
      bytes[n++] = c;
    }
  }
  const view = bytes.subarray(0, n);
  if (latin1) {
    let out = "";
    for (let i = 0; i < view.length; i += 8192) out += String.fromCharCode(...view.subarray(i, i + 8192));
    return out;
  }
  return UTF8_REPLACE.decode(view);
}

function unquoteWith(s, latin1) {
  if (!s.includes("%")) return s;
  let out = "";
  let i = 0;
  while (i < s.length) {
    let j = i;
    if (s.charCodeAt(i) < 0x80) {
      while (j < s.length && s.charCodeAt(j) < 0x80) j += 1;
      out += unquoteAsciiRun(s.slice(i, j), latin1);
    } else {
      while (j < s.length && s.charCodeAt(j) >= 0x80) j += 1;
      out += s.slice(i, j);
    }
    i = j;
  }
  return out;
}

/** urllib.parse.unquote(s) (UTF-8, errors="replace"). */
export function unquote(s) {
  return unquoteWith(s, false);
}

/** urllib.parse.unquote(s, "iso-8859-1"): how WSGI servers turn the request path into PATH_INFO. */
export function unquoteLatin1(s) {
  return unquoteWith(s, true);
}

/** urllib.parse.parse_qs(qs, keep_blank_values): a Map of name → list of values, in first-seen order. */
export function parseQs(qs, keepBlankValues = false) {
  const out = new Map();
  if (!qs) return out;
  for (const nameValue of qs.split("&")) {
    if (!nameValue) continue;
    const eq = nameValue.indexOf("=");
    let name;
    let value;
    if (eq < 0) {
      if (!keepBlankValues) continue;
      name = nameValue;
      value = "";
    } else {
      name = nameValue.slice(0, eq);
      value = nameValue.slice(eq + 1);
    }
    if (value.length || keepBlankValues) {
      name = unquote(name.replace(/\+/g, " "));
      value = unquote(value.replace(/\+/g, " "));
      const list = out.get(name);
      if (list) list.push(value);
      else out.set(name, [value]);
    }
  }
  return out;
}

function isAscii(s) {
  return /^[\x00-\x7f]*$/.test(s);
}

/** collect.origin_of(url): scheme://netloc (lower-cased) as urllib.parse.urlsplit sees it, or null. */
export function originOf(url) {
  if (!url) return null;
  let u = url.replace(/^[\x00-\x20]+/, "").replace(/[\t\r\n]/g, "");
  let scheme = "";
  const colon = u.indexOf(":");
  if (colon > 0 && /^[A-Za-z0-9+.-]+$/.test(u.slice(0, colon))) {
    scheme = u.slice(0, colon).toLowerCase();
    u = u.slice(colon + 1);
  }
  let netloc = "";
  if (u.startsWith("//")) {
    let delim = u.length;
    for (const ch of "/?#") {
      const w = u.indexOf(ch, 2);
      if (w >= 0 && w < delim) delim = w;
    }
    netloc = u.slice(2, delim);
    const open = netloc.includes("[");
    const close = netloc.includes("]");
    if (open !== close) return null; // ValueError("Invalid IPv6 URL")
  }
  if (netloc && !isAscii(netloc)) { // urlsplit's NFKC check
    const n = netloc.replace(/[@:#?]/g, "");
    const n2 = n.normalize("NFKC");
    if (n !== n2) {
      for (const ch of "/?#@:") if (n2.includes(ch)) return null;
    }
  }
  if (!scheme || !netloc) return null;
  return scheme + "://" + netloc.toLowerCase();
}

/** config.normalize_origin */
export function normalizeOrigin(origin) {
  let o = pyStrip(origin).toLowerCase().replace(/\/+$/, "");
  if (o.startsWith("https://") && o.endsWith(":443")) o = o.slice(0, -4);
  else if (o.startsWith("http://") && o.endsWith(":80")) o = o.slice(0, -3);
  return o;
}

// ---------------------------------------------------------------------------
// fnmatch
// ---------------------------------------------------------------------------

const RE_SYNTAX = /[\\^$.*+?()[\]{}|/]/g;
const CLASS_SPECIAL = /[\\\]\[^-]/g;

function escapeClass(s) {
  return s.replace(CLASS_SPECIAL, "\\$&");
}

const STAR = Symbol("*");

/**
 * fnmatch.translate as a JavaScript RegExp (same semantics as fnmatch.fnmatchcase). As CPython 3.9+ does, each
 * interior `*` followed by a fixed piece is matched as an atomic group, emulated with a lookahead and a
 * backreference (`(?=([\s\S]*?FIXED))\1`): the shortest match is never revisited, so a pattern with several
 * stars cannot backtrack polynomially on a long subject (an attacker's Origin header).
 */
export function fnmatchCompile(pattern) {
  const pat = Array.from(pattern); // Python iterates by code point
  const n = pat.length;
  const items = []; // regex pieces, and STAR for each run of stars
  const add = (piece) => items.push(piece);
  let i = 0;
  while (i < n) {
    const c = pat[i];
    i += 1;
    if (c === "*") {
      if (!items.length || items[items.length - 1] !== STAR) add(STAR); // compress consecutive stars
      continue;
    }
    if (c === "?") {
      add("[\\s\\S]");
    } else if (c === "[") {
      let j = i;
      if (j < n && pat[j] === "!") j += 1;
      if (j < n && pat[j] === "]") j += 1;
      while (j < n && pat[j] !== "]") j += 1;
      if (j >= n) {
        add("\\[");
        continue;
      }
      const stuff = pat.slice(i, j);
      let chunks;
      if (!stuff.includes("-")) {
        chunks = [stuff.join("")];
      } else {
        chunks = [];
        let start = i;
        let k = pat[i] === "!" ? i + 2 : i + 1;
        for (;;) {
          let found = -1;
          for (let q = k; q < j; q++) if (pat[q] === "-") { found = q; break; }
          if (found < 0) break;
          chunks.push(pat.slice(start, found).join(""));
          start = found + 1;
          k = found + 3;
        }
        const chunk = pat.slice(start, j).join("");
        if (chunk) chunks.push(chunk);
        else chunks[chunks.length - 1] += "-";
        for (let q = chunks.length - 1; q > 0; q--) { // remove empty ranges
          const prev = Array.from(chunks[q - 1]);
          const cur = Array.from(chunks[q]);
          if (prev[prev.length - 1].codePointAt(0) > cur[0].codePointAt(0)) {
            chunks[q - 1] = prev.slice(0, -1).join("") + cur.slice(1).join("");
            chunks.splice(q, 1);
          }
        }
      }
      i = j + 1;
      const flat = chunks.join("-");
      if (!flat) {
        add("(?!)");
      } else if (flat === "!") {
        add("[\\s\\S]");
      } else {
        const negate = chunks[0].startsWith("!");
        if (negate) chunks[0] = chunks[0].slice(1);
        add("[" + (negate ? "^" : "") + chunks.map(escapeClass).join("-") + "]");
      }
    } else {
      add(c.replace(RE_SYNTAX, "\\$&"));
    }
  }
  // Deal with the stars as fnmatch.translate does: fixed pieces at the start, then `STAR fixed` pairs.
  let res = "";
  let k = 0;
  while (k < items.length && items[k] !== STAR) res += items[k++];
  let group = 0;
  while (k < items.length) {
    k += 1; // the STAR
    if (k === items.length) {
      res += "[\\s\\S]*";
      break;
    }
    let fixed = "";
    while (k < items.length && items[k] !== STAR) fixed += items[k++];
    if (k === items.length) {
      res += "[\\s\\S]*" + fixed;
    } else {
      group += 1;
      res += `(?=([\\s\\S]*?${fixed}))\\${group}`;
    }
  }
  return new RegExp("^(?:" + res + ")$", "u");
}

/** fnmatch.fnmatchcase(name, pattern) */
export function fnmatchcase(name, pattern) {
  return fnmatchCompile(pattern).test(name);
}

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

/** time.strftime("%Y-%m-%d", time.gmtime(ms / 1000)) */
export function utcDay(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}
