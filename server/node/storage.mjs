/**
 * Raw storage for the Node connector: a port of server/python/tidesight/storage.py on node:sqlite.
 *
 * * SQLiteStore appends to <data_dir>/events.db (raw_requests + raw_hits) and replay.db (chunks), one short
 *   BEGIN IMMEDIATE transaction per request, with the per-key rate limit applied inside it. Connections use the
 *   configured journal_mode, synchronous and busy_timeout, and migrations run on first use (PRAGMA user_version).
 * * SpoolStore appends one JSON line per record to <data_dir>/spool/<stream>/<YYYY-MM-DD>/<host>-<pid>.jsonl,
 *   byte-compatible with Python's SpoolStore, so `python -m tidesight.jobs load-spool` imports it.
 * * FallbackStore writes to SQLite and falls back to the spool when SQLite fails (never lose a hit).
 *
 * node:sqlite is synchronous: a transaction runs to completion without yielding, so concurrent requests in this
 * process never interleave inside one. A lock held by another process (a cron job) blocks for at most
 * busy_timeout_ms.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import { dumpsCompact } from "./core/pyjson.mjs";
import { utcDay } from "./core/pycompat.mjs";
import { RATE_LIMITED, SPOOLED, STORED, StorageError, jsonOrNull } from "./core/records.mjs";
import {
  EVENTS_MIGRATIONS, INSERT_CHUNK_COLUMNS, INSERT_HIT_COLUMNS, INSERT_REQUEST_COLUMNS, REPLAY_MIGRATIONS,
} from "./core/schema.mjs";

/** Errors raised by SQLite itself (the ones Python sees as sqlite3.Error). */
export function isSqliteError(exc) {
  return Boolean(exc) && (exc.code === "ERR_SQLITE_ERROR" || typeof exc.errcode === "number");
}

/** Open a connection with Tidesight's pragmas (autocommit; transactions are explicit). */
export function connect(file, journalMode = "wal", synchronous = "normal", busyTimeoutMs = 10000, log = null) {
  fs.mkdirSync(path.dirname(path.resolve(file)), { recursive: true });
  const db = new DatabaseSync(file);
  try {
    db.exec(`PRAGMA busy_timeout=${Math.trunc(busyTimeoutMs)}`);
    const row = db.prepare(`PRAGMA journal_mode=${journalMode === "wal" ? "WAL" : "DELETE"}`).get();
    const mode = String(row && row.journal_mode).toLowerCase();
    if (mode !== journalMode && log) log.warning(`journal_mode ${journalMode} requested for ${file}, got ${mode}`);
    db.exec(`PRAGMA synchronous=${synchronous === "full" ? "FULL" : "NORMAL"}`);
  } catch (exc) {
    db.close();
    throw exc;
  }
  return db;
}

/** Apply pending migrations; safe to call concurrently from several processes. */
export function migrate(db, migrations) {
  const userVersion = () => db.prepare("PRAGMA user_version").get().user_version;
  let current = userVersion();
  const target = migrations.length ? migrations[migrations.length - 1][0] : 0;
  if (current >= target) return current;
  db.exec("BEGIN IMMEDIATE");
  try {
    current = userVersion(); // re-check under the lock
    for (const [version, statements] of migrations) {
      if (version <= current) continue;
      for (const sql of statements) db.exec(sql);
      db.exec(`PRAGMA user_version=${version}`);
      current = version;
    }
    db.exec("COMMIT");
  } catch (exc) {
    try {
      db.exec("ROLLBACK");
    } catch {
      // the original error matters
    }
    throw exc;
  }
  return current;
}

const SQL = {
  rateInsert: "INSERT OR IGNORE INTO rate_limits(key, minute, hits) VALUES (?, ?, 0)",
  rateUpdate: "UPDATE rate_limits SET hits = hits + 1 WHERE key = ? AND minute = ?",
  rateSelect: "SELECT hits FROM rate_limits WHERE key = ? AND minute = ?",
  ratePrune: "DELETE FROM rate_limits WHERE minute < ?",
  counterInsert: "INSERT OR IGNORE INTO counters(day, site_id, name, n) VALUES (?, ?, ?, 0)",
  counterUpdate: "UPDATE counters SET n = n + ? WHERE day = ? AND site_id = ? AND name = ?",
  request: `INSERT INTO raw_requests(${INSERT_REQUEST_COLUMNS}) VALUES (${Array(18).fill("?").join(", ")})`,
  hit: `INSERT INTO raw_hits(${INSERT_HIT_COLUMNS}) VALUES (${Array(15).fill("?").join(", ")})`,
  chunk: `INSERT INTO chunks(${INSERT_CHUNK_COLUMNS}) VALUES (${Array(15).fill("?").join(", ")})`,
};

/** Appends raw records to events.db and replay.db. */
export class SQLiteStore {
  constructor(dataDir, journalMode = "wal", synchronous = "normal", busyTimeoutMs = 10000, log = null) {
    this.dataDir = dataDir;
    this.eventsPath = path.join(dataDir, "events.db");
    this.replayPath = path.join(dataDir, "replay.db");
    this.journalMode = journalMode;
    this.synchronous = synchronous;
    this.busyTimeoutMs = busyTimeoutMs;
    this.log = log;
    this.label = "sqlite";
    this.conns = new Map(); // which → {db, stmts}
    this.lastPrune = 0;
  }

  conn(which) {
    let entry = this.conns.get(which);
    if (!entry) {
      const file = which === "events" ? this.eventsPath : this.replayPath;
      const db = connect(file, this.journalMode, this.synchronous, this.busyTimeoutMs, this.log);
      try {
        migrate(db, which === "events" ? EVENTS_MIGRATIONS : REPLAY_MIGRATIONS);
      } catch (exc) {
        db.close();
        throw exc;
      }
      entry = { db, stmts: new Map() };
      this.conns.set(which, entry);
    }
    return entry;
  }

  stmt(entry, sql) {
    let st = entry.stmts.get(sql);
    if (!st) {
      st = entry.db.prepare(sql);
      entry.stmts.set(sql, st);
    }
    return st;
  }

  discard(which) {
    const entry = this.conns.get(which);
    this.conns.delete(which);
    if (entry) {
      try {
        entry.db.close();
      } catch {
        // already broken
      }
    }
  }

  close() {
    for (const which of [...this.conns.keys()]) this.discard(which);
  }

  /** Count this request; true when the key exceeded `limit` requests this minute. */
  rateLimited(entry, key, limit, nowMs) {
    if (!key || limit <= 0) return false;
    const minute = Math.floor(nowMs / 60000);
    this.stmt(entry, SQL.rateInsert).run(key, minute);
    this.stmt(entry, SQL.rateUpdate).run(key, minute);
    const hits = this.stmt(entry, SQL.rateSelect).get(key, minute).hits;
    const now = performance.now();
    if (now - this.lastPrune > 600000 || this.lastPrune === 0) { // prune old minutes at most every 10 minutes
      this.lastPrune = now;
      this.stmt(entry, SQL.ratePrune).run(minute - 60);
    }
    return hits > limit;
  }

  count(entry, day, siteId, name, n = 1) {
    this.stmt(entry, SQL.counterInsert).run(day, siteId || "", name);
    this.stmt(entry, SQL.counterUpdate).run(n, day, siteId || "", name);
  }

  transaction(which, fn) {
    const entry = this.conn(which);
    try {
      entry.db.exec("BEGIN IMMEDIATE");
      try {
        const result = fn(entry);
        entry.db.exec("COMMIT");
        return result;
      } catch (exc) {
        try {
          entry.db.exec("ROLLBACK");
        } catch {
          // keep the original error
        }
        throw exc;
      }
    } catch (exc) {
      if (isSqliteError(exc)) this.discard(which);
      throw exc;
    }
  }

  insertRequest(entry, rec) {
    const result = this.stmt(entry, SQL.request).run(
      rec.received_at, rec.site_id, rec.site_status, rec.endpoint, rec.method, rec.ip,
      jsonOrNull(rec.headers) || "{}", jsonOrNull(rec.cookies), rec.query, rec.content_type, rec.body_bytes,
      rec.body_encoding, jsonOrNull(rec.envelope), rec.body, rec.error, rec.proto_v, rec.hits.length,
      jsonOrNull(rec.server));
    return result.lastInsertRowid;
  }

  insertHits(entry, requestId, rec) {
    if (!rec.hits.length) return;
    const st = this.stmt(entry, SQL.hit);
    const siteOk = rec.site_ok;
    for (const h of rec.hits) {
      st.run(requestId, h.seq, rec.received_at, rec.site_id, siteOk, h.type, h.name, h.client_ts, h.event_id,
        h.visitor_id, h.session_id, h.page_id, h.user_id, h.url, h.data);
    }
  }

  insertChunk(entry, rec) {
    this.stmt(entry, SQL.chunk).run(
      rec.received_at, rec.site_id, rec.site_ok, rec.kind, rec.visitor_id, rec.session_id, rec.page_id, rec.seq,
      rec.client_ts, rec.url, rec.ip, jsonOrNull(rec.meta) || "{}", rec.encoding, rec.payload, rec.error);
  }

  async writeRequest(rec, rateKey = null, rateLimit = 0) {
    return this.transaction("events", (entry) => {
      if (this.rateLimited(entry, rateKey, rateLimit, rec.received_at)) {
        this.count(entry, utcDay(rec.received_at), rec.site_id, "rate_limited");
        return RATE_LIMITED;
      }
      const requestId = this.insertRequest(entry, rec);
      this.insertHits(entry, requestId, rec);
      return STORED;
    });
  }

  async writeChunk(rec, rateKey = null, rateLimit = 0) {
    return this.transaction("replay", (entry) => {
      if (this.rateLimited(entry, rateKey, rateLimit, rec.received_at)) return RATE_LIMITED;
      this.insertChunk(entry, rec);
      return STORED;
    });
  }

  async recordCounter(day, siteId, name, n = 1) {
    this.transaction("events", (entry) => this.count(entry, day, siteId, name, n));
  }

  async healthy() {
    try {
      const entry = this.conn("events");
      this.stmt(entry, "SELECT 1 FROM raw_requests LIMIT 1").all();
      fs.accessSync(this.dataDir, fs.constants.W_OK);
      return true;
    } catch {
      this.discard("events");
      return false;
    }
  }
}

/**
 * Appends one JSON line per record to a per-process file (no locking needed).
 * Rate limiting is approximate (per process, in memory) in this mode.
 */
export class SpoolStore {
  constructor(dataDir) {
    this.dataDir = dataDir;
    this.root = path.join(dataDir, "spool");
    this.host = os.hostname().split(".")[0] || "host";
    this.label = "spool";
    this.fds = new Map(); // `${stream}/${day}` → fd
    this.rate = new Map();
  }

  fd(stream, ms) {
    const day = utcDay(ms);
    const key = `${stream}/${day}`;
    let fd = this.fds.get(key);
    if (fd === undefined) {
      const dir = path.join(this.root, stream, day);
      fs.mkdirSync(dir, { recursive: true });
      fd = fs.openSync(path.join(dir, `${this.host}-${process.pid}.jsonl`), "a", 0o600);
      for (const [old, oldFd] of this.fds) {
        if (old.startsWith(stream + "/")) {
          this.fds.delete(old);
          try {
            fs.closeSync(oldFd);
          } catch {
            // ignore
          }
        }
      }
      this.fds.set(key, fd);
    }
    return fd;
  }

  append(stream, ms, obj) {
    const line = Buffer.from(dumpsCompact(obj) + "\n", "utf8");
    const fd = this.fd(stream, ms);
    let offset = 0;
    while (offset < line.length) offset += fs.writeSync(fd, line, offset, line.length - offset);
  }

  rateLimited(key, limit, nowMs) {
    if (!key || limit <= 0) return false;
    const minute = Math.floor(nowMs / 60000);
    if (this.rate.size > 50000) {
      for (const k of [...this.rate.keys()]) if (Number(k.slice(k.lastIndexOf("\u0000") + 1)) < minute - 1) this.rate.delete(k);
    }
    const k = `${key}\u0000${minute}`;
    const n = (this.rate.get(k) || 0) + 1;
    this.rate.set(k, n);
    return n > limit;
  }

  async writeRequest(rec, rateKey = null, rateLimit = 0) {
    if (this.rateLimited(rateKey, rateLimit, rec.received_at)) {
      this.append("events", rec.received_at, { k: "counter", day: utcDay(rec.received_at), site_id: rec.site_id,
        name: "rate_limited", n: 1 });
      return RATE_LIMITED;
    }
    this.append("events", rec.received_at, { k: "request", rec: rec.toDict() });
    return STORED;
  }

  async writeChunk(rec, rateKey = null, rateLimit = 0) {
    if (this.rateLimited(rateKey, rateLimit, rec.received_at)) return RATE_LIMITED;
    this.append("chunks", rec.received_at, { k: "chunk", rec: rec.toDict() });
    return STORED;
  }

  async recordCounter(day, siteId, name, n = 1) {
    this.append("events", Date.now(), { k: "counter", day, site_id: siteId, name, n });
  }

  async healthy() {
    try {
      fs.mkdirSync(this.root, { recursive: true });
      fs.accessSync(this.root, fs.constants.W_OK);
      return true;
    } catch {
      return false;
    }
  }

  close() {
    for (const fd of this.fds.values()) {
      try {
        fs.closeSync(fd);
      } catch {
        // ignore
      }
    }
    this.fds.clear();
  }
}

/** SQLite first; on a database error, append to the spool instead (never lose a hit). */
export class FallbackStore {
  constructor(primary, spool, log = null) {
    this.primary = primary;
    this.spool = spool;
    this.label = primary.label;
    this.log = log;
  }

  async call(method, args) {
    try {
      return await this.primary[method](...args);
    } catch (exc) {
      if (!isSqliteError(exc)) throw exc;
      if (this.log) this.log.error(`SQLite write failed (${exc.message}); writing to the spool instead`);
      let outcome;
      try {
        outcome = await this.spool[method](...args);
      } catch (exc2) {
        throw new StorageError(`could not store the record: ${exc.message} / ${exc2.message}`, { cause: exc2 });
      }
      // The Python FallbackStore reports SPOOLED even when the spool's own rate limiter refused the record
      // (only a counter line is written then); report the refusal so the tracker retries instead.
      return outcome === RATE_LIMITED ? RATE_LIMITED : SPOOLED;
    }
  }

  writeRequest(rec, rateKey = null, rateLimit = 0) {
    return this.call("writeRequest", [rec, rateKey, rateLimit]);
  }

  writeChunk(rec, rateKey = null, rateLimit = 0) {
    return this.call("writeChunk", [rec, rateKey, rateLimit]);
  }

  async recordCounter(day, siteId, name, n = 1) {
    try {
      await this.primary.recordCounter(day, siteId, name, n);
    } catch (exc) {
      if (!isSqliteError(exc)) throw exc;
      await this.spool.recordCounter(day, siteId, name, n);
    }
  }

  async healthy() {
    return (await this.primary.healthy()) || (await this.spool.healthy());
  }

  close() {
    this.primary.close();
    this.spool.close();
  }
}

/** Create the store described by the [server] config. */
export function openStore(dataDir, storageMode = "sqlite", journalMode = "wal", synchronous = "normal",
  busyTimeoutMs = 10000, spoolFallback = true, log = null) {
  if (storageMode === "spool") return new SpoolStore(dataDir);
  const primary = new SQLiteStore(dataDir, journalMode, synchronous, busyTimeoutMs, log);
  if (spoolFallback) return new FallbackStore(primary, new SpoolStore(dataDir), log);
  return primary;
}
