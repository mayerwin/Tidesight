/**
 * Gzip for the collector, runtime-neutral: the platform's CompressionStream / DecompressionStream (Node 18+,
 * Workers), or a native zlib the platform registers (setNativeZlib: Node).
 *
 * gunzipBounded() mirrors collect.gunzip_bounded(): output is capped, and errors read like Python's. The gzip
 * header is checked here first, in the order zlib checks it, so header problems produce zlib's own messages
 * ("incorrect header check", "unknown compression method", ...). With native zlib (Node), exactly as Python's
 * zlib.decompressobj: only the first member is decoded (anything after it is ignored), its trailer (CRC-32 and
 * length) is checked, and zlib's own error texts are kept. With DecompressionStream (the Worker), errors inside
 * the deflate data only surface as a generic data error, and the platform decides about further members.
 */

export class GzipError extends Error {}

let native = null;

/**
 * Register a native zlib: {inflateRaw(data, limit, window) → {out, consumed}, crc32(bytes) → number}. inflateRaw
 * decodes raw deflate data with output windows of `window` bytes (zlib's avail_out per call), throws an error with
 * code ERR_BUFFER_TOO_LARGE as soon as the output passes `limit` bytes, zlib's own errors otherwise (Node's codes
 * and texts), and reports in `consumed` the length of the deflate stream in bytes.
 */
export function setNativeZlib(impl) {
  native = impl;
}

const TRUNCATED = "truncated gzip data";
const zlibError = (msg) => new GzipError(`Error -3 while decompressing data: ${msg}`);

let crcTable = null;

function crc32(bytes, start, end) {
  if (!crcTable) {
    crcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c >>> 0;
    }
  }
  let crc = 0xffffffff;
  for (let i = start; i < end; i++) crc = crcTable[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/** Validate the gzip member header like zlib's inflate() does; throws GzipError. Returns the deflate data's offset. */
function checkHeader(b) {
  const n = b.length;
  if (n < 2) throw new GzipError(TRUNCATED);
  if (b[0] !== 0x1f || b[1] !== 0x8b) throw zlibError("incorrect header check");
  if (n < 4) throw new GzipError(TRUNCATED);
  if (b[2] !== 8) throw zlibError("unknown compression method");
  const flags = b[3];
  if (flags & 0xe0) throw zlibError("unknown header flags set");
  let p = 10;
  if (n < p) throw new GzipError(TRUNCATED);
  if (flags & 4) { // FEXTRA
    if (n < p + 2) throw new GzipError(TRUNCATED);
    p += 2 + (b[p] | (b[p + 1] << 8));
    if (n < p) throw new GzipError(TRUNCATED);
  }
  for (const bit of [8, 16]) { // FNAME, FCOMMENT: zero-terminated
    if (flags & bit) {
      while (p < n && b[p] !== 0) p += 1;
      if (p >= n) throw new GzipError(TRUNCATED);
      p += 1;
    }
  }
  if (flags & 2) { // FHCRC
    if (n < p + 2) throw new GzipError(TRUNCATED);
    if ((b[p] | (b[p + 1] << 8)) !== (crc32(b, 0, p) & 0xffff)) throw zlibError("header crc mismatch");
    p += 2;
  }
  if (p >= n) throw new GzipError(TRUNCATED);
  return p;
}

const u32 = (b, i) => (b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)) >>> 0;

/**
 * The member's trailer as zlib's gzip mode checks it once the deflate stream has ended: the CRC-32 as soon as its 4
 * bytes are there, then the length. `incomplete` is the error when they are not all there (a GzipError), or null.
 */
function checkTrailer(body, at, out, incomplete) {
  if (body.length < at + 4) return incomplete;
  if (u32(body, at) !== (native.crc32(out) >>> 0)) return zlibError("incorrect data check");
  if (body.length < at + 8) return incomplete;
  if (u32(body, at + 4) !== (out.length >>> 0)) return zlibError("incorrect length check");
  return null;
}

/**
 * The first gzip member, decoded with the native zlib exactly as collect.gunzip_bounded does it with Python's
 * zlib.decompressobj(16 + MAX_WBITS).decompress(body, max_out + 1): one output window of max_out + 1 bytes, in which
 * zlib also checks the trailer when the stream ends there; whatever follows the member is ignored.
 */
function nativeGunzip(body, start, maxOut) {
  const data = body.subarray(start);
  const exceeds = new GzipError(`decompressed body exceeds ${maxOut} bytes`);
  let res;
  try {
    // Past maxOut bytes this throws right after the first window: an error further on is never reached, as in Python.
    res = native.inflateRaw(data, maxOut, maxOut + 1);
  } catch (exc) {
    if (exc && exc.code === "ERR_BUFFER_TOO_LARGE") {
      // The window filled up. Python's call also checked the trailer if the stream ended right there.
      let whole;
      try {
        whole = native.inflateRaw(data, maxOut + 1, maxOut + 1);
      } catch {
        throw exceeds; // the stream goes on past the window
      }
      throw checkTrailer(body, start + whole.consumed, whole.out, exceeds) || exceeds;
    }
    if (exc && exc.code === "Z_BUF_ERROR") throw new GzipError(TRUNCATED); // the input ends inside the stream
    const errno = exc && typeof exc.errno === "number" ? exc.errno : -3;
    throw new GzipError(`Error ${errno} while decompressing data: ${(exc && exc.message) || "invalid compressed data"}`);
  }
  const out = res.out;
  if (out.length > maxOut) throw exceeds;
  const error = checkTrailer(body, start + res.consumed, out, new GzipError(TRUNCATED));
  if (error) throw error;
  return out;
}

function describe(exc) {
  const code = exc && exc.code;
  const message = (exc && exc.message) || "";
  if (code === "Z_BUF_ERROR" || /unexpected end|truncat|incomplete|premature/i.test(message)) return new GzipError(TRUNCATED);
  return zlibError(message && !/^(?:TypeError|Error)?:?\s*$/.test(message) ? message : "invalid compressed data");
}

async function readAll(readable, maxOut) {
  const reader = readable.getReader();
  const parts = [];
  let total = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (maxOut !== undefined && total > maxOut) {
      reader.cancel().catch(() => {});
      throw new GzipError(`decompressed body exceeds ${maxOut} bytes`);
    }
    parts.push(value);
  }
  if (parts.length === 1) return parts[0];
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.byteLength;
  }
  return out;
}

function feed(stream, bytes) {
  const writer = stream.writable.getWriter();
  writer.write(bytes.slice()).catch(() => {}); // a copy: the stream may take ownership of the buffer
  writer.close().catch(() => {});
}

/** Decompress a gzip body, refusing to produce more than maxOut bytes (GzipError with Python's wording). */
export async function gunzipBounded(body, maxOut) {
  const start = checkHeader(body);
  if (native) return nativeGunzip(body, start, maxOut);
  const stream = new DecompressionStream("gzip");
  feed(stream, body);
  try {
    return await readAll(stream.readable, maxOut);
  } catch (exc) {
    if (exc instanceof GzipError) throw exc;
    throw describe(exc);
  }
}

/** gzip.compress(body) (the platform's zlib at its default level, 6 — the level the collector uses). */
export async function gzipCompress(body) {
  const stream = new CompressionStream("gzip");
  feed(stream, body);
  return readAll(stream.readable);
}

export const isGzip = (b) => b.length >= 2 && b[0] === 0x1f && b[1] === 0x8b;
