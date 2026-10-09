/**
 * Raw storage on Cloudflare D1: the same rows as server/python/tidesight/storage.py's SQLiteStore, written
 * through the D1 bindings EVENTS (events.db schema) and REPLAY (replay.db schema).
 *
 * D1 has no interactive transactions, so each request is ONE db.batch() — an atomic transaction — whose
 * statements carry the rate-limit decision in SQL:
 *
 *   1. upsert rate_limits(key, minute) and RETURN the new count
 *   2. count "rate_limited" in counters        ... only WHERE that count > limit
 *   3. insert the raw_requests row             ... only WHERE that count <= limit
 *   4. insert the raw_hits rows, request_id = (SELECT MAX(id) FROM raw_requests), from a JSON array expanded by
 *      json_each (one bound parameter instead of 15 per event: D1 allows 100 parameters per statement)
 *
 * A failed batch changes nothing and raises StorageError, so the collector answers 503 and the tracker retries.
 * There is no spool fallback: a Worker has no filesystem.
 */

import { utcDay } from "../../node/core/pycompat.mjs";
import { RATE_LIMITED, STORED, StorageError, jsonOrNull } from "../../node/core/records.mjs";
import { INSERT_CHUNK_COLUMNS, INSERT_HIT_COLUMNS, INSERT_REQUEST_COLUMNS } from "../../node/core/schema.mjs";

const RATE_UPSERT = "INSERT INTO rate_limits(key, minute, hits) VALUES (?, ?, 1) " +
  "ON CONFLICT(key, minute) DO UPDATE SET hits = hits + 1 RETURNING hits";
const RATE_HITS = "(SELECT hits FROM rate_limits WHERE key = ? AND minute = ?)";
const COUNT_IF_LIMITED = "INSERT INTO counters(day, site_id, name, n) SELECT ?, ?, 'rate_limited', 1 " +
  `WHERE ${RATE_HITS} > ? ON CONFLICT(day, site_id, name) DO UPDATE SET n = n + 1`;
const COUNT = "INSERT INTO counters(day, site_id, name, n) VALUES (?, ?, ?, ?) " +
  "ON CONFLICT(day, site_id, name) DO UPDATE SET n = n + excluded.n";
const PRUNE = "DELETE FROM rate_limits WHERE minute < ?";

const placeholders = (n) => Array(n).fill("?").join(", ");
const INSERT_REQUEST = `INSERT INTO raw_requests(${INSERT_REQUEST_COLUMNS}) SELECT ${placeholders(18)}`;
const HIT_FIELDS = 11; // seq, type, name, client_ts, event_id, visitor_id, session_id, page_id, user_id, url, data
const INSERT_HITS_JSON = `INSERT INTO raw_hits(${INSERT_HIT_COLUMNS}) SELECT (SELECT MAX(id) FROM raw_requests), ` +
  "json_extract(value, '$[0]'), ?, ?, ?, " +
  Array.from({ length: HIT_FIELDS - 1 }, (_, i) => `json_extract(value, '$[${i + 1}]')`).join(", ") +
  " FROM json_each(?)";
const INSERT_HIT_ONE = `INSERT INTO raw_hits(${INSERT_HIT_COLUMNS}) SELECT (SELECT MAX(id) FROM raw_requests), ${placeholders(14)}`;
const INSERT_CHUNK = `INSERT INTO chunks(${INSERT_CHUNK_COLUMNS}) SELECT ${placeholders(15)}`;

// D1 refuses any single value over 2,000,000 bytes; JSON arrays of hits are split well below that.
const GROUP_BYTES = 900000;

/** D1 binds null, numbers, strings and ArrayBuffers; integers beyond 2^53 go as text (INTEGER affinity converts). */
function bindable(v) {
  if (v === undefined) return null;
  if (typeof v === "bigint") return v.toString();
  if (v instanceof Uint8Array) return v.buffer.slice(v.byteOffset, v.byteOffset + v.byteLength);
  return v;
}

function jsonValue(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return JSON.stringify(v.toWellFormed());
  return String(v); // ints (number or BigInt): exact digits, read back as INTEGER by json_extract
}

function utf8Length(s) {
  let n = s.length;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0x80) n += c >= 0x800 && (c < 0xd800 || c > 0xdbff) ? 2 : 1;
  }
  return n;
}

function hitFields(h) {
  return [h.seq, h.type, h.name, h.client_ts, h.event_id, h.visitor_id, h.session_id, h.page_id, h.user_id, h.url, h.data];
}

export class D1Store {
  /** @param events D1Database (events.db schema)  @param replay D1Database (replay.db schema) */
  constructor(events, replay) {
    this.events = events;
    this.replay = replay;
    this.label = "d1";
    this.lastPrune = 0;
  }

  /** Statements 1-2 (and an occasional prune) plus the WHERE clause gating the inserts. */
  rateStatements(db, key, limit, receivedAt, siteId, counted) {
    if (!key || limit <= 0) return { stmts: [], where: "", args: [] };
    const minute = Math.floor(receivedAt / 60000);
    const stmts = [db.prepare(RATE_UPSERT).bind(key, minute)];
    if (counted) stmts.push(db.prepare(COUNT_IF_LIMITED).bind(utcDay(receivedAt), siteId || "", key, minute, limit));
    const now = Date.now();
    if (now - this.lastPrune > 600000) { // prune old minutes at most every 10 minutes per isolate
      this.lastPrune = now;
      stmts.push(db.prepare(PRUNE).bind(minute - 60));
    }
    return { stmts, where: ` WHERE ${RATE_HITS} <= ?`, args: [key, minute, limit] };
  }

  async run(db, stmts, limited, limit) {
    let results;
    try {
      results = await db.batch(stmts);
    } catch (exc) {
      throw new StorageError(`D1 write failed: ${exc && exc.message}`, { cause: exc });
    }
    if (limited) {
      const row = results[0] && results[0].results && results[0].results[0];
      if (row && Number(row.hits) > limit) return RATE_LIMITED;
    }
    return STORED;
  }

  async writeRequest(rec, rateKey = null, rateLimit = 0) {
    const db = this.events;
    const rate = this.rateStatements(db, rateKey, rateLimit, rec.received_at, rec.site_id, true);
    const stmts = [...rate.stmts];
    stmts.push(db.prepare(INSERT_REQUEST + rate.where).bind(...[
      rec.received_at, rec.site_id, rec.site_status, rec.endpoint, rec.method, rec.ip,
      jsonOrNull(rec.headers) || "{}", jsonOrNull(rec.cookies), rec.query, rec.content_type, rec.body_bytes,
      rec.body_encoding, jsonOrNull(rec.envelope), rec.body, rec.error, rec.proto_v, rec.hits.length,
      jsonOrNull(rec.server),
    ].map(bindable), ...rate.args));

    const siteOk = rec.site_ok;
    let group = [];
    let groupBytes = 0;
    const flush = () => {
      if (!group.length) return;
      stmts.push(db.prepare(INSERT_HITS_JSON + rate.where + " ORDER BY key")
        .bind(rec.received_at, rec.site_id, siteOk, "[" + group.join(",") + "]", ...rate.args));
      group = [];
      groupBytes = 0;
    };
    for (const h of rec.hits) {
      const json = "[" + hitFields(h).map(jsonValue).join(",") + "]";
      const bytes = utf8Length(json);
      if (bytes > GROUP_BYTES) { // a very large event: its own statement, values bound directly
        flush();
        const [seq, ...rest] = hitFields(h);
        stmts.push(db.prepare(INSERT_HIT_ONE + rate.where)
          .bind(...[seq, rec.received_at, rec.site_id, siteOk, ...rest].map(bindable), ...rate.args));
        continue;
      }
      if (groupBytes + bytes > GROUP_BYTES) flush();
      group.push(json);
      groupBytes += bytes + 1;
    }
    flush();
    return this.run(db, stmts, rate.stmts.length > 0, rateLimit);
  }

  async writeChunk(rec, rateKey = null, rateLimit = 0) {
    const db = this.replay;
    const rate = this.rateStatements(db, rateKey, rateLimit, rec.received_at, rec.site_id, false);
    const stmts = [...rate.stmts, db.prepare(INSERT_CHUNK + rate.where).bind(...[
      rec.received_at, rec.site_id, rec.site_ok, rec.kind, rec.visitor_id, rec.session_id, rec.page_id, rec.seq,
      rec.client_ts, rec.url, rec.ip, jsonOrNull(rec.meta) || "{}", rec.encoding, rec.payload, rec.error,
    ].map(bindable), ...rate.args)];
    return this.run(db, stmts, rate.stmts.length > 0, rateLimit);
  }

  async recordCounter(day, siteId, name, n = 1) {
    try {
      await this.events.prepare(COUNT).bind(day, siteId || "", name, n).run();
    } catch (exc) {
      throw new StorageError(`D1 write failed: ${exc && exc.message}`, { cause: exc });
    }
  }

  async healthy() {
    try {
      await this.events.prepare("SELECT 1 FROM raw_requests LIMIT 1").all();
      return true;
    } catch {
      return false;
    }
  }
}
