/**
 * URL redaction (runtime-neutral): a port of server/python/tidesight/redact.py, whose docstring is the spec and
 * whose test vectors (server/python/tests/redact_vectors.json) fix it. Secrets in URLs (login links, reset
 * tokens, signed URLs...) become "redacted". The collector applies it to the Referer header it stores, which a
 * browser may fill with the full page URL (beacons, the pixel).
 *
 * Patterns are globs where only `*` is special (any run of characters, `/` included); ASCII letters match
 * case-insensitively. With the site's redact_params (parameter names) and redact_paths (paths):
 *  - URLs with a scheme other than http, https, ws and wss are left as they are;
 *  - credentials before the host become `redacted@host`;
 *  - a parameter whose (percent-decoded) name matches redact_params gets the value `redacted`: in the query, in a
 *    fragment made of parameters, in a hash route's query and in path parameters (`;name=value`); a name longer
 *    than 256 characters always has its value redacted;
 *  - a parameter value that is itself a URL or a query with a secret in it is redacted whole: the value, decoded
 *    the same way, goes through these rules (as a URL, and as a query when it has no `?` or `#` but an `=`), two
 *    levels deep at most;
 *  - the path, and a hash route's path, are compared with redact_paths: the first pattern that matches the whole
 *    path applies, and each non-empty part a `*` matched becomes `redacted`.
 * It runs in linear time whatever the URL: no regular expression is built from the patterns.
 */

import { pyLen } from "./pycompat.mjs";

export const REDACTED = "redacted";
const MAX_NAME = 256;
const MAX_NESTING = 2; // how deep parameter values are looked into (a URL in a URL in a URL)
const SCHEMES = new Set(["http", "https", "ws", "wss"]);

/** ASCII lower case (other characters unchanged, so positions stay the same). */
export function fold(s) {
  return s.replace(/[A-Z]+/g, (m) => m.toLowerCase());
}

/**
 * Where each `*` of the glob matched, as [start, end] positions in text, if the glob matches the whole text; else
 * null. The pieces between stars are taken at their first occurrence, left to right, which finds a match whenever
 * there is one; each piece is searched once, so the time is linear in the text.
 */
export function globSpans(pattern, text, caseInsensitive = true) {
  if (caseInsensitive) {
    pattern = fold(pattern);
    text = fold(text);
  }
  const parts = pattern.split("*");
  if (parts.length === 1) return pattern === text ? [] : null;
  const head = parts[0];
  const tail = parts[parts.length - 1];
  const end = text.length - tail.length;
  if (end < head.length || !text.startsWith(head) || !text.endsWith(tail)) return null;
  const spans = [];
  let pos = head.length;
  for (let i = 1; i < parts.length - 1; i++) {
    const piece = parts[i];
    const at = text.indexOf(piece, pos); // str.find(piece, pos, end): the piece must end by `end`
    if (at < 0 || at + piece.length > end) return null;
    spans.push([pos, at]);
    pos = at + piece.length;
  }
  spans.push([pos, end]);
  return spans;
}

export function globMatch(pattern, text, caseInsensitive = true) {
  return globSpans(pattern, text, caseInsensitive) !== null;
}

/** The strings of a configured pattern list (anything else is ignored). */
export function patterns(value) {
  return Array.isArray(value) ? value.filter((p) => typeof p === "string") : [];
}

const SCHEME_RE = /^[A-Za-z][A-Za-z0-9+.-]*$/;

/**
 * A parameter name (or value) as decodeURIComponent(name.replace(/\+/g, " ")) reads it, or unchanged when that
 * fails (a "%" without two hex digits after it, or escapes that are not UTF-8).
 */
export function decodeName(name) {
  const plus = name.replace(/\+/g, " ");
  if (!plus.includes("%")) return plus;
  try {
    return decodeURIComponent(plus);
  } catch {
    return name;
  }
}

/** True when a parameter value, decoded, is a URL or a query that these rules would change. */
function secretInside(value, names, paths, depth) {
  const inner = decodeName(value);
  if (redact(inner, names, paths, depth) !== inner) return true;
  return !inner.includes("?") && !inner.includes("#") && inner.includes("=") &&
    redactPairs(inner, "&", names, paths, depth) !== inner;
}

function nameMatches(raw, names) {
  if (raw.length > MAX_NAME && pyLen(raw) > MAX_NAME) return true; // characters as Python counts them
  const name = decodeName(raw);
  return names.some((p) => globMatch(p, name));
}

function redactPairs(s, sep, names, paths, depth) {
  const out = [];
  for (let pair of s.split(sep)) {
    const i = pair.indexOf("=");
    if (i >= 1) {
      if (names.length && nameMatches(pair.slice(0, i), names)) {
        pair = pair.slice(0, i + 1) + REDACTED;
      } else if (depth < MAX_NESTING && secretInside(pair.slice(i + 1), names, paths, depth + 1)) {
        pair = pair.slice(0, i + 1) + REDACTED;
      }
    }
    out.push(pair);
  }
  return out.join(sep);
}

function redactPath(path, names, paths, depth) {
  if (path.includes(";")) {
    path = path.split("/").map((seg) => {
      const i = seg.indexOf(";");
      return i < 0 ? seg : seg.slice(0, i + 1) + redactPairs(seg.slice(i + 1), ";", names, paths, depth);
    }).join("/");
  }
  for (const pattern of paths) {
    const spans = globSpans(pattern, path);
    if (spans !== null) {
      for (let k = spans.length - 1; k >= 0; k--) {
        const [start, end] = spans[k];
        if (end > start) path = path.slice(0, start) + REDACTED + path.slice(end);
      }
      break;
    }
  }
  return path;
}

function redactFragment(frag, names, paths, depth) {
  const q = frag.indexOf("?");
  let head = q < 0 ? frag : frag.slice(0, q);
  if (head.startsWith("/") || head.startsWith("!/")) {
    const k = head.startsWith("!") ? 1 : 0;
    head = head.slice(0, k) + redactPath(head.slice(k), names, paths, depth);
  } else if (q < 0) {
    return frag.indexOf("=") > 0 ? redactPairs(frag, "&", names, paths, depth) : frag;
  }
  if (q < 0) return head;
  return head + "?" + redactPairs(frag.slice(q + 1), "&", names, paths, depth);
}

function redact(url, names, paths, depth) {
  const h = url.indexOf("#");
  const main = h < 0 ? url : url.slice(0, h);
  const q = main.indexOf("?");
  const base = q < 0 ? main : main.slice(0, q);
  const c = base.indexOf(":");
  let start = -1;
  if (c > 0 && SCHEME_RE.test(base.slice(0, c))) {
    if (!SCHEMES.has(fold(base.slice(0, c)))) return url;
    if (base.slice(c, c + 3) === "://") start = c + 3;
  } else if (base.startsWith("//")) {
    start = 2;
  }
  let origin = "";
  let path = base;
  if (start >= 0) {
    const j = base.indexOf("/", start);
    if (j < 0) {
      origin = base;
      path = "";
    } else {
      origin = base.slice(0, j);
      path = base.slice(j);
    }
    const at = origin.lastIndexOf("@");
    if (at > start) origin = origin.slice(0, start) + REDACTED + origin.slice(at);
  }
  let out = origin + redactPath(path, names, paths, depth);
  if (q >= 0) out += "?" + redactPairs(main.slice(q + 1), "&", names, paths, depth);
  if (h >= 0) out += "#" + redactFragment(url.slice(h + 1), names, paths, depth);
  return out;
}

/** The URL with the secrets the patterns describe replaced (see the module comment). */
export function redactUrl(url, names, paths) {
  if (typeof url !== "string" || !url || (!names.length && !paths.length)) return url;
  return redact(url, names, paths, 0);
}
