/**
 * Python-compatible JSON for the Tidesight connectors (runtime-neutral, no dependencies).
 *
 * The reference collector parses bodies with Python's `json.loads` and stores events re-serialised with
 * `json.dumps`, so what lands in the database depends on Python's value model. Native JSON.parse would lose
 * some of it, so this module reproduces it:
 *
 *   Python   JavaScript value produced by loads() and accepted by dumps()
 *   int      number when it is a safe integer, BigInt otherwise (exact at any size)
 *   float    PyFloat (keeps 1.0 distinct from 1 and prints with Python's repr)
 *   dict     Map (insertion order, including integer-like keys that plain objects would reorder;
 *            a duplicate key keeps its first position and its last value, as in Python)
 *   list     Array;  str → string;  True/False → boolean;  None → null
 *
 * The parser is a port of CPython's C scanner (Modules/_json.c, 3.9–3.12), including its error messages
 * ("Expecting value: line 1 column 1 (char 0)"), so a flagged request stores the same `error` text.
 *
 * dumps(..., {ensureAscii: false}) writes a lone UTF-16 surrogate as U+FFFD, as the Python collector
 * stores it (it replaces them right after parsing: JSON can carry them, UTF-8 cannot); SQLite bindings in
 * JavaScript do the same for text columns.
 */

/** A Python float. `value` is the IEEE double. */
export class PyFloat {
  constructor(value) {
    this.value = value;
  }

  valueOf() {
    return this.value;
  }
}

/** json.JSONDecodeError: `message` is str(exc), e.g. "Expecting value: line 1 column 1 (char 0)". */
export class JSONDecodeError extends Error {
  constructor(msg, doc, pos) {
    let lineno = 1;
    let last = -1;
    for (let i = doc.indexOf("\n"); i !== -1 && i < pos; i = doc.indexOf("\n", i + 1)) {
      lineno += 1;
      last = i;
    }
    const cp = codePointIndex(doc, pos);
    const colno = cp - (last === -1 ? -1 : codePointIndex(doc, last));
    super(`${msg}: line ${lineno} column ${colno} (char ${cp})`);
    this.name = "JSONDecodeError";
    this.msg = msg;
    this.pos = cp;
    this.lineno = lineno;
    this.colno = colno;
  }
}

/** Python positions count code points; JavaScript indexes count UTF-16 units. */
function codePointIndex(s, i) {
  let n = i;
  for (let k = 0; k < i - 1; k++) {
    const c = s.charCodeAt(k);
    if (c >= 0xd800 && c <= 0xdbff) {
      const d = s.charCodeAt(k + 1);
      if (d >= 0xdc00 && d <= 0xdfff) {
        n -= 1;
        k += 1;
      }
    }
  }
  return n;
}

class StopIteration {
  constructor(idx) {
    this.idx = idx;
  }
}

/** Python's default parse_constant: NaN, Infinity and -Infinity become floats. */
function defaultConstant(name) {
  return new PyFloat(name === "NaN" ? NaN : name === "Infinity" ? Infinity : -Infinity);
}

const INT_MAX_STR_DIGITS = 4300;

function intFromDigits(numstr) {
  const digits = numstr.charCodeAt(0) === 0x2d ? numstr.length - 1 : numstr.length;
  if (digits <= 15) {
    return Number(numstr) || 0; // "-0" is the int 0
  }
  if (digits > INT_MAX_STR_DIGITS) {
    // Python >= 3.10.7 refuses to convert longer decimal strings (ValueError, caught by the collector).
    throw new Error(`Exceeds the limit (${INT_MAX_STR_DIGITS}) for integer string conversion: value has ${digits} digits; ` +
      "use sys.set_int_max_str_digits() to increase the limit");
  }
  const big = BigInt(numstr);
  return big >= -9007199254740991n && big <= 9007199254740991n ? Number(big) : big;
}

function hexValue(c) {
  if (c >= 0x30 && c <= 0x39) return c - 0x30;
  if (c >= 0x61 && c <= 0x66) return c - 0x61 + 10;
  if (c >= 0x41 && c <= 0x46) return c - 0x41 + 10;
  return -1;
}

const SIMPLE_ESCAPES = { 0x22: '"', 0x5c: "\\", 0x2f: "/", 0x62: "\b", 0x66: "\f", 0x6e: "\n", 0x72: "\r", 0x74: "\t" };

/**
 * json.loads(s) for a str. Options:
 *   parseConstant(name)  called for NaN / Infinity / -Infinity (may throw, as Tidesight's collector does)
 *   maxDepth             nesting depth that raises Python's RecursionError message (981: where Python 3.10,
 *                        with its default recursion limit, fails inside a WSGI request; it varies by version)
 */
export function loads(s, { parseConstant = defaultConstant, maxDepth = 981 } = {}) {
  if (s.length && s.charCodeAt(0) === 0xfeff) {
    throw new JSONDecodeError("Unexpected UTF-8 BOM (decode using utf-8-sig)", s, 0);
  }
  const len = s.length;
  const endIdx = len - 1;
  let next = 0; // index after the value just scanned (the C scanner's next_idx_ptr)
  let depth = 0;

  const isWs = (c) => c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d;

  function recursionError(kind) {
    return new RangeError(`maximum recursion depth exceeded while decoding a JSON ${kind} from a unicode string`);
  }

  function scanString(end) {
    const begin = end - 1;
    let chunks = null;
    for (;;) {
      let nxt = end;
      let c = 0;
      for (; nxt < len; nxt++) {
        c = s.charCodeAt(nxt);
        if (c === 0x22 || c === 0x5c) break;
        if (c <= 0x1f) throw new JSONDecodeError("Invalid control character at", s, nxt);
      }
      if (nxt >= len) throw new JSONDecodeError("Unterminated string starting at", s, begin);
      if (c === 0x22) {
        if (chunks === null) {
          next = nxt + 1;
          return s.slice(end, nxt);
        }
        if (nxt !== end) chunks.push(s.slice(end, nxt));
        next = nxt + 1;
        return chunks.join("");
      }
      if (chunks === null) chunks = [];
      if (nxt !== end) chunks.push(s.slice(end, nxt));
      nxt += 1;
      if (nxt === len) throw new JSONDecodeError("Unterminated string starting at", s, begin);
      c = s.charCodeAt(nxt);
      if (c !== 0x75) { // not \u
        end = nxt + 1;
        const ch = SIMPLE_ESCAPES[c];
        if (ch === undefined) throw new JSONDecodeError("Invalid \\escape", s, end - 2);
        chunks.push(ch);
        continue;
      }
      nxt += 1;
      end = nxt + 4;
      if (end >= len) throw new JSONDecodeError("Invalid \\uXXXX escape", s, nxt - 1);
      let cp = 0;
      for (; nxt < end; nxt++) {
        const d = hexValue(s.charCodeAt(nxt));
        if (d < 0) throw new JSONDecodeError("Invalid \\uXXXX escape", s, end - 5);
        cp = (cp << 4) | d;
      }
      if (cp >= 0xd800 && cp <= 0xdbff && end + 6 < len && s.charCodeAt(nxt) === 0x5c && s.charCodeAt(nxt + 1) === 0x75) {
        const end2 = end + 6;
        let c2 = 0;
        for (let k = nxt + 2; k < end2; k++) {
          const d = hexValue(s.charCodeAt(k));
          if (d < 0) throw new JSONDecodeError("Invalid \\uXXXX escape", s, end2 - 5);
          c2 = (c2 << 4) | d;
        }
        if (c2 >= 0xdc00 && c2 <= 0xdfff) {
          cp = 0x10000 + (((cp - 0xd800) << 10) | (c2 - 0xdc00));
          end = end2;
        }
      }
      chunks.push(String.fromCodePoint(cp));
    }
  }

  function matchNumber(start) {
    let idx = start;
    if (s.charCodeAt(idx) === 0x2d) {
      idx += 1;
      if (idx > endIdx) throw new StopIteration(start);
    }
    let c = s.charCodeAt(idx);
    if (c >= 0x31 && c <= 0x39) {
      idx += 1;
      while (idx <= endIdx && (c = s.charCodeAt(idx)) >= 0x30 && c <= 0x39) idx += 1;
    } else if (c === 0x30) {
      idx += 1;
    } else {
      throw new StopIteration(start);
    }
    let isFloat = false;
    if (idx < endIdx && s.charCodeAt(idx) === 0x2e && (c = s.charCodeAt(idx + 1)) >= 0x30 && c <= 0x39) {
      isFloat = true;
      idx += 2;
      while (idx <= endIdx && (c = s.charCodeAt(idx)) >= 0x30 && c <= 0x39) idx += 1;
    }
    if (idx < endIdx && ((c = s.charCodeAt(idx)) === 0x65 || c === 0x45)) {
      const eStart = idx;
      idx += 1;
      if (idx < endIdx && ((c = s.charCodeAt(idx)) === 0x2d || c === 0x2b)) idx += 1;
      while (idx <= endIdx && (c = s.charCodeAt(idx)) >= 0x30 && c <= 0x39) idx += 1;
      c = s.charCodeAt(idx - 1);
      if (c >= 0x30 && c <= 0x39) isFloat = true;
      else idx = eStart;
    }
    const numstr = s.slice(start, idx);
    const value = isFloat ? new PyFloat(Number(numstr)) : intFromDigits(numstr);
    next = idx;
    return value;
  }

  function parseObject(idx) {
    const map = new Map();
    while (idx <= endIdx && isWs(s.charCodeAt(idx))) idx += 1;
    if (idx > endIdx || s.charCodeAt(idx) !== 0x7d) {
      for (;;) {
        if (idx > endIdx || s.charCodeAt(idx) !== 0x22) {
          throw new JSONDecodeError("Expecting property name enclosed in double quotes", s, idx);
        }
        const key = scanString(idx + 1);
        idx = next;
        while (idx <= endIdx && isWs(s.charCodeAt(idx))) idx += 1;
        if (idx > endIdx || s.charCodeAt(idx) !== 0x3a) throw new JSONDecodeError("Expecting ':' delimiter", s, idx);
        idx += 1;
        while (idx <= endIdx && isWs(s.charCodeAt(idx))) idx += 1;
        const value = scanOnce(idx);
        map.set(key, value);
        idx = next;
        while (idx <= endIdx && isWs(s.charCodeAt(idx))) idx += 1;
        if (idx <= endIdx && s.charCodeAt(idx) === 0x7d) break;
        if (idx > endIdx || s.charCodeAt(idx) !== 0x2c) throw new JSONDecodeError("Expecting ',' delimiter", s, idx);
        idx += 1;
        while (idx <= endIdx && isWs(s.charCodeAt(idx))) idx += 1;
      }
    }
    next = idx + 1;
    return map;
  }

  function parseArray(idx) {
    const arr = [];
    while (idx <= endIdx && isWs(s.charCodeAt(idx))) idx += 1;
    if (idx > endIdx || s.charCodeAt(idx) !== 0x5d) {
      for (;;) {
        arr.push(scanOnce(idx));
        idx = next;
        while (idx <= endIdx && isWs(s.charCodeAt(idx))) idx += 1;
        if (idx <= endIdx && s.charCodeAt(idx) === 0x5d) break;
        if (idx > endIdx || s.charCodeAt(idx) !== 0x2c) throw new JSONDecodeError("Expecting ',' delimiter", s, idx);
        idx += 1;
        while (idx <= endIdx && isWs(s.charCodeAt(idx))) idx += 1;
      }
    }
    if (idx > endIdx || s.charCodeAt(idx) !== 0x5d) throw new JSONDecodeError("Expecting value", s, endIdx);
    next = idx + 1;
    return arr;
  }

  function scanOnce(idx) {
    if (idx >= len) throw new StopIteration(idx);
    const c = s.charCodeAt(idx);
    switch (c) {
      case 0x22: // "
        return scanString(idx + 1);
      case 0x7b: // {
        if (++depth > maxDepth) throw recursionError("object");
        try {
          return parseObject(idx + 1);
        } finally {
          depth -= 1;
        }
      case 0x5b: // [
        if (++depth > maxDepth) throw recursionError("array");
        try {
          return parseArray(idx + 1);
        } finally {
          depth -= 1;
        }
      case 0x6e: // null
        if (idx + 3 < len && s.startsWith("ull", idx + 1)) {
          next = idx + 4;
          return null;
        }
        break;
      case 0x74: // true
        if (idx + 3 < len && s.startsWith("rue", idx + 1)) {
          next = idx + 4;
          return true;
        }
        break;
      case 0x66: // false
        if (idx + 4 < len && s.startsWith("alse", idx + 1)) {
          next = idx + 5;
          return false;
        }
        break;
      case 0x4e: // NaN
        if (idx + 2 < len && s.startsWith("aN", idx + 1)) {
          const value = parseConstant("NaN");
          next = idx + 3;
          return value;
        }
        break;
      case 0x49: // Infinity
        if (idx + 7 < len && s.startsWith("nfinity", idx + 1)) {
          const value = parseConstant("Infinity");
          next = idx + 8;
          return value;
        }
        break;
      case 0x2d: // -Infinity
        if (idx + 8 < len && s.startsWith("Infinity", idx + 1)) {
          const value = parseConstant("-Infinity");
          next = idx + 9;
          return value;
        }
        break;
      default:
        break;
    }
    return matchNumber(idx);
  }

  let idx = 0;
  while (idx < len && isWs(s.charCodeAt(idx))) idx += 1;
  let value;
  try {
    value = scanOnce(idx);
  } catch (exc) {
    if (exc instanceof StopIteration) throw new JSONDecodeError("Expecting value", s, exc.idx);
    throw exc;
  }
  let end = next;
  while (end < len && isWs(s.charCodeAt(end))) end += 1;
  if (end !== len) throw new JSONDecodeError("Extra data", s, end);
  return value;
}

// ---------------------------------------------------------------------------
// dumps
// ---------------------------------------------------------------------------

const HEX = "0123456789abcdef";
const ESCAPES = { 0x22: '\\"', 0x5c: "\\\\", 0x08: "\\b", 0x0c: "\\f", 0x0a: "\\n", 0x0d: "\\r", 0x09: "\\t" };
const NEEDS_ESCAPE_UNICODE = /["\\\u0000-\u001f\ud800-\udfff]/;
const NEEDS_ESCAPE_ASCII = /[^\x20\x21\x23-\x5b\x5d-\x7e]/;

function u4(c) {
  return "\\u" + HEX[(c >> 12) & 15] + HEX[(c >> 8) & 15] + HEX[(c >> 4) & 15] + HEX[c & 15];
}

/** A JSON string literal, escaped like Python's encoder (ensure_ascii on or off). */
export function encodeString(s, ensureAscii) {
  if (ensureAscii) {
    if (!NEEDS_ESCAPE_ASCII.test(s)) return '"' + s + '"';
    let out = '"';
    let run = 0;
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      if (c >= 0x20 && c <= 0x7e && c !== 0x22 && c !== 0x5c) continue;
      out += s.slice(run, i) + (ESCAPES[c] || u4(c));
      run = i + 1;
    }
    return out + s.slice(run) + '"';
  }
  if (!NEEDS_ESCAPE_UNICODE.test(s)) return '"' + s + '"';
  let out = '"';
  let run = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdfff) {
      if (c <= 0xdbff && i + 1 < s.length) {
        const d = s.charCodeAt(i + 1);
        if (d >= 0xdc00 && d <= 0xdfff) {
          i += 1; // a valid pair stays as is
          continue;
        }
      }
      out += s.slice(run, i) + "\u{fffd}"; // lone surrogate (see the module comment)
      run = i + 1;
      continue;
    }
    if (c > 0x1f && c !== 0x22 && c !== 0x5c) continue;
    out += s.slice(run, i) + (ESCAPES[c] || u4(c));
    run = i + 1;
  }
  return out + s.slice(run) + '"';
}

/** repr(float) for a finite or non-finite double, exactly as CPython prints it. */
export function floatRepr(x) {
  if (x !== x) return "nan";
  if (x === Infinity) return "inf";
  if (x === -Infinity) return "-inf";
  if (x === 0) return Object.is(x, -0) ? "-0.0" : "0.0";
  const sign = x < 0 ? "-" : "";
  const [mantissa, expText] = Math.abs(x).toExponential().split("e"); // shortest round-trip digits
  const digits = mantissa.replace(".", "");
  const decpt = parseInt(expText, 10) + 1;
  if (decpt > -4 && decpt <= 16) {
    if (decpt <= 0) return sign + "0." + "0".repeat(-decpt) + digits;
    if (decpt >= digits.length) return sign + digits + "0".repeat(decpt - digits.length) + ".0";
    return sign + digits.slice(0, decpt) + "." + digits.slice(decpt);
  }
  const exp = decpt - 1;
  const m = digits.length > 1 ? digits[0] + "." + digits.slice(1) : digits;
  return sign + m + "e" + (exp < 0 ? "-" : "+") + String(Math.abs(exp)).padStart(2, "0");
}

function floatJson(x) {
  if (x !== x) return "NaN";
  if (x === Infinity) return "Infinity";
  if (x === -Infinity) return "-Infinity";
  return floatRepr(x);
}

/** Code point order (Python's str ordering); JavaScript's default sort compares UTF-16 units. */
export function compareCodePoints(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a.codePointAt(i);
    const y = b.codePointAt(i);
    if (x !== y) return x < y ? -1 : 1;
    if (x > 0xffff) i += 1;
  }
  return a.length === b.length ? 0 : a.length < b.length ? -1 : 1;
}

/**
 * json.dumps(value). Options mirror Python's: ensureAscii (default true), itemSep/keySep (default ", " and ": "),
 * sortKeys. Plain objects are accepted as dicts too; a JS number that is not an integer prints as a float.
 */
export function dumps(value, { ensureAscii = true, itemSep = ", ", keySep = ": ", sortKeys = false } = {}) {
  const parts = [];
  const encode = (v, depth) => {
    if (depth > 5000) throw new RangeError("maximum recursion depth exceeded while encoding a JSON object");
    if (v === null || v === undefined) {
      parts.push("null");
    } else if (typeof v === "string") {
      parts.push(encodeString(v, ensureAscii));
    } else if (typeof v === "number") {
      if (Number.isInteger(v)) parts.push(Math.abs(v) < 1e21 ? String(v) : BigInt(v).toString());
      else parts.push(floatJson(v));
    } else if (typeof v === "boolean") {
      parts.push(v ? "true" : "false");
    } else if (typeof v === "bigint") {
      parts.push(v.toString());
    } else if (v instanceof PyFloat) {
      parts.push(floatJson(v.value));
    } else if (Array.isArray(v)) {
      if (!v.length) {
        parts.push("[]");
        return;
      }
      parts.push("[");
      for (let i = 0; i < v.length; i++) {
        if (i) parts.push(itemSep);
        encode(v[i], depth + 1);
      }
      parts.push("]");
    } else {
      const entries = v instanceof Map ? [...v.entries()] : Object.entries(v);
      if (!entries.length) {
        parts.push("{}");
        return;
      }
      if (sortKeys) entries.sort((a, b) => compareCodePoints(a[0], b[0]));
      parts.push("{");
      for (let i = 0; i < entries.length; i++) {
        if (i) parts.push(itemSep);
        parts.push(encodeString(String(entries[i][0]), ensureAscii), keySep);
        encode(entries[i][1], depth + 1);
      }
      parts.push("}");
    }
  };
  encode(value, 0);
  return parts.join("");
}

/** json.dumps(value, ensure_ascii=False, separators=(",", ":")): how the collector stores JSON columns. */
export function dumpsCompact(value) {
  return dumps(value, { ensureAscii: false, itemSep: ",", keySep: ":" });
}

/** True for a Python int (bool excluded). */
export function isPyInt(v) {
  return (typeof v === "number" && Number.isInteger(v)) || typeof v === "bigint";
}
