/**
 * Gzip through the platform's CompressionStream / DecompressionStream (Node 18+, Workers), runtime-neutral.
 *
 * gunzipBounded() mirrors collect.gunzip_bounded(): output is capped, and errors read like Python's.
 * The gzip header is checked here first, in the order zlib checks it, so header problems produce zlib's
 * own messages ("incorrect header check", "unknown compression method", ...). Errors inside the deflate
 * data or the trailer only surface as a generic data error, because the streams do not expose zlib's text.
 */

export class GzipError extends Error {}

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

/** Validate the gzip member header like zlib's inflate() does; throws GzipError. */
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
  checkHeader(body);
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
