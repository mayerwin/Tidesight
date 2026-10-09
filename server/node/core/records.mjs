/**
 * The records a request produces and the storage interface they are written through (runtime-neutral).
 * Field names and spool serialisation (toDict) match server/python/tidesight/storage.py, so `jobs.py load-spool`
 * imports spool files written by the JavaScript connectors.
 *
 * Storage interface (implemented by the Node SQLite/spool stores and by the Worker's D1 store):
 *
 *   label                                   what /health reports as "storage"
 *   async writeRequest(rec, rateKey, limit)  → STORED | RATE_LIMITED | SPOOLED; throws StorageError when the
 *   async writeChunk(rec, rateKey, limit)      record could not be stored anywhere. The per-key rate limit
 *                                             (requests per minute, 0 = off) is applied atomically with the
 *                                             write, and a refused /collect request counts "rate_limited".
 *   async recordCounter(day, siteId, name, n)
 *   async healthy()                          → boolean
 */

import { dumpsCompact } from "./pyjson.mjs";

export const STORED = "stored";
export const RATE_LIMITED = "rate_limited";
export const SPOOLED = "spooled";

/** The record could not be stored anywhere (the collector answers 503 so trackers retry). */
export class StorageError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "StorageError";
  }
}

/** collect.py's _json(): None stays None, anything else compact JSON (ensure_ascii=False). */
export function jsonOrNull(value) {
  return value === null || value === undefined ? null : dumpsCompact(value);
}

export function toBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}

/** One event of a batch, with the typed columns extracted at ingest. */
export class HitRecord {
  constructor(f) {
    this.seq = f.seq;
    this.type = f.type ?? null;
    this.name = f.name ?? null;
    this.client_ts = f.client_ts ?? null;
    this.event_id = f.event_id ?? null;
    this.visitor_id = f.visitor_id ?? null;
    this.session_id = f.session_id ?? null;
    this.page_id = f.page_id ?? null;
    this.user_id = f.user_id ?? null;
    this.url = f.url ?? null;
    this.data = f.data;
  }

  toDict() {
    return {
      seq: this.seq, type: this.type, name: this.name, client_ts: this.client_ts, event_id: this.event_id,
      visitor_id: this.visitor_id, session_id: this.session_id, page_id: this.page_id, user_id: this.user_id,
      url: this.url, data: this.data,
    };
  }
}

/** Everything stored about one HTTP request to /collect or /p.gif. */
export class RequestRecord {
  constructor(f) {
    this.received_at = f.received_at;
    this.endpoint = f.endpoint;
    this.method = f.method;
    this.site_id = f.site_id ?? null;
    this.site_status = f.site_status;
    this.ip = f.ip ?? null;
    this.headers = f.headers; // Map
    this.cookies = f.cookies ?? null; // Map {kept, others} or null
    this.query = f.query ?? null;
    this.content_type = f.content_type ?? null;
    this.body_bytes = f.body_bytes ?? 0;
    this.body_encoding = f.body_encoding ?? null;
    this.envelope = f.envelope ?? null; // Map or null
    this.body = f.body ?? null; // Uint8Array or null
    this.error = f.error ?? null;
    this.proto_v = f.proto_v ?? null;
    this.server = f.server ?? null;
    this.hits = f.hits ?? [];
  }

  get site_ok() {
    return this.site_status === "ok" ? 1 : 0;
  }

  /** RequestRecord.to_dict() key order: the attributes, then body (base64) and hits. */
  toDict() {
    return {
      received_at: this.received_at, endpoint: this.endpoint, method: this.method, site_id: this.site_id,
      site_status: this.site_status, ip: this.ip, headers: this.headers, cookies: this.cookies, query: this.query,
      content_type: this.content_type, body_bytes: this.body_bytes, body_encoding: this.body_encoding,
      envelope: this.envelope, error: this.error, proto_v: this.proto_v, server: this.server,
      body: this.body === null ? null : toBase64(this.body), hits: this.hits.map((h) => h.toDict()),
    };
  }
}

/** One heavy-data chunk for replay.db (stored without parsing the payload). */
export class ChunkRecord {
  constructor(f) {
    this.received_at = f.received_at;
    this.kind = f.kind;
    this.site_id = f.site_id ?? null;
    this.site_status = f.site_status;
    this.payload = f.payload; // Uint8Array
    this.encoding = f.encoding;
    this.meta = f.meta; // Map
    this.visitor_id = f.visitor_id ?? null;
    this.session_id = f.session_id ?? null;
    this.page_id = f.page_id ?? null;
    this.seq = f.seq ?? null;
    this.client_ts = f.client_ts ?? null;
    this.url = f.url ?? null;
    this.ip = f.ip ?? null;
    this.error = f.error ?? null;
  }

  get site_ok() {
    return this.site_status === "ok" ? 1 : 0;
  }

  toDict() {
    return {
      received_at: this.received_at, kind: this.kind, site_id: this.site_id, site_status: this.site_status,
      payload: toBase64(this.payload), encoding: this.encoding, meta: this.meta, visitor_id: this.visitor_id,
      session_id: this.session_id, page_id: this.page_id, seq: this.seq, client_ts: this.client_ts, url: this.url,
      ip: this.ip, error: this.error,
    };
  }
}
