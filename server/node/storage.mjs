/**
 * Raw storage for the Node connector: a port of server/python/tidesight/storage.py on node:sqlite.
 *
 * * SQLiteStore appends to <data_dir>/events.db (raw_requests + raw_hits) and replay.db (chunks), one short
 *   BEGIN IMMEDIATE transaction per request, with the allowances (per-minute budgets in rate_limits, the site's
 *   daily quota in counters) checked inside it. Connections use the configured journal_mode and synchronous, and
 *   migrations run on first use (PRAGMA user_version).
 * * SpoolStore appends one JSON line per record to <data_dir>/spool/<stream>/<YYYY-MM-DD>/<host>-<pid>.jsonl,
 *   byte-compatible with Python's SpoolStore, so `python -m tidesight.jobs load-spool` imports it. Its allowances
 *   are kept in memory, per process (approximate with several processes), and its refusals are written as summed
 *   counter lines at most once a minute and on close, as in Python.
 * * FallbackStore writes to SQLite and falls back to the spool when SQLite fails (never lose a hit).
 *
 * node:sqlite is synchronous: a transaction runs to completion without yielding, so concurrent requests in this
 * process never interleave inside one, but SQLite's own wait for another process's lock (a cron job) would freeze
 * every request of this single-threaded process. So the write lock is taken without SQLite's busy handler and,
 * while another process holds it, tried again every 10 ms between turns of the event loop (tracker files,
 * /config.js and other requests are served meanwhile), for at most 200 ms whatever busy_timeout_ms says. After a
 * lock failure SQLite is left alone for 10 s: writes go to the spool meanwhile (or fail fast with 503 without the
 * spool fallback).
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import { ErrorLog } from "./core/errorlog.mjs";
import { dumpsCompact } from "./core/pyjson.mjs";
import { utcDay } from "./core/pycompat.mjs";
import { NO_LIMITS, RATE_LIMITED, SPOOLED, STORED, StorageError, activeBudget, jsonOrNull } from "./core/records.mjs";
import {
  EVENTS_MIGRATIONS, INSERT_CHUNK_COLUMNS, INSERT_HIT_COLUMNS, INSERT_REQUEST_COLUMNS, REPLAY_MIGRATIONS,
} from "./core/schema.mjs";

const REQUEST_BUSY_MS = 200; // the longest a request waits for another process's write lock
const BUSY_RETRY_MS = 10; // how often the write lock is tried meanwhile
const BUSY_BACKOFF_MS = 10000; // after a lock failure, SQLite is not touched for this long
const BUSY = Symbol("busy");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const PRUNE_EVERY_MS = 60000; // old allowance minutes: at most once a minute per database
const PRUNE_BATCH = 20000; // rows per prune

/** Errors raised by SQLite itself (the ones Python sees as sqlite3.Error). */
export function isSqliteError(exc) {
  return Boolean(exc) && (exc.code === "ERR_SQLITE_ERROR" || typeof exc.errcode === "number");
}

/** SQLITE_BUSY or SQLITE_LOCKED (extended codes included): another connection holds the lock. */
export function isBusy(exc) {
  const code = exc && exc.errcode;
  return typeof code === "number" && ((code & 0xff) === 5 || (code & 0xff) === 6);
}

function backingOffError() {
  return Object.assign(new Error("database is locked (waiting before trying again)"),
    { code: "ERR_SQLITE_ERROR", errcode: 5, errstr: "database is locked" });
}

function lockedError() {
  return Object.assign(new Error("database is locked"), { code: "ERR_SQLITE_ERROR", errcode: 5, errstr: "database is locked" });
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

const ADD_COLUMN_RE = /^ALTER TABLE (\w+) ADD COLUMN (\w+)/i;

/**
 * storage._column_exists: true for an ADD COLUMN whose column is already there, so that a database that lost its
 * user_version (a D1 export) migrates cleanly instead of failing on a duplicate column.
 */
function columnExists(db, sql) {
  const m = ADD_COLUMN_RE.exec(sql.trim());
  if (!m) return false;
  return db.prepare(`PRAGMA table_info(${m[1]})`).all().some((row) => row.name === m[2]);
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
      for (const sql of statements) if (!columnExists(db, sql)) db.exec(sql);
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
  rateUpdate: "UPDATE rate_limits SET hits = hits + ? WHERE key = ? AND minute = ?",
  rateSelect: "SELECT hits FROM rate_limits WHERE key = ? AND minute = ?",
  ratePrune: "DELETE FROM rate_limits WHERE (key, minute) IN (SELECT key, minute FROM rate_limits " +
    `WHERE minute < ? LIMIT ${PRUNE_BATCH})`,
  quotaSelect: "SELECT n FROM counters WHERE day = ? AND site_id = ? AND name = 'hits'",
  counterInsert: "INSERT OR IGNORE INTO counters(day, site_id, name, n) VALUES (?, ?, ?, 0)",
  counterUpdate: "UPDATE counters SET n = n + ? WHERE day = ? AND site_id = ? AND name = ?",
  request: `INSERT INTO raw_requests(${INSERT_REQUEST_COLUMNS}) VALUES (${Array(18).fill("?").join(", ")})`,
  hit: `INSERT INTO raw_hits(${INSERT_HIT_COLUMNS}) VALUES (${Array(15).fill("?").join(", ")})`,
  chunk: `INSERT INTO chunks(${INSERT_CHUNK_COLUMNS}) VALUES (${Array(15).fill("?").join(", ")})`,
};

/** Appends raw records to events.db and replay.db. */
export class SQLiteStore {
  /**
   * @param {ErrorLog} [errors]  the process's error-log throttle (messages logged on every request otherwise)
   */
  constructor(dataDir, journalMode = "wal", synchronous = "normal", busyTimeoutMs = 10000, log = null, errors = null) {
    this.dataDir = dataDir;
    this.eventsPath = path.join(dataDir, "events.db");
    this.replayPath = path.join(dataDir, "replay.db");
    this.journalMode = journalMode;
    this.synchronous = synchronous;
    this.busyTimeoutMs = Math.min(busyTimeoutMs, REQUEST_BUSY_MS);
    this.log = log;
    this.label = "sqlite";
    this.conns = new Map(); // which → {db, stmts}
    this.lastPrune = new Map(); // which → performance.now() of the last rate_limits prune
    this.errors = errors || (log ? new ErrorLog(log) : null);
    // A reconnection after each failure would repeat the journal_mode warning: throttled like errors.
    this.warn = { warning: (msg) => (this.errors ? this.errors.exception(msg, undefined, "warning") : log && log.warning(msg)) };
    this.busyUntil = 0;
  }

  conn(which) {
    let entry = this.conns.get(which);
    if (!entry) {
      const file = which === "events" ? this.eventsPath : this.replayPath;
      const db = connect(file, this.journalMode, this.synchronous, this.busyTimeoutMs, this.warn);
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

  /** True while SQLite is left alone after a lock failure. */
  backingOff() {
    return performance.now() < this.busyUntil;
  }

  /**
   * Run fn against `which` unless SQLite is being left alone (then fail at once, as a busy database would); a lock
   * failure starts that pause. A connection broken by an SQLite error is reopened next time.
   */
  guarded(which, fn) {
    if (this.backingOff()) throw backingOffError();
    try {
      return fn(this.conn(which));
    } catch (exc) {
      if (isBusy(exc)) this.busyUntil = performance.now() + BUSY_BACKOFF_MS;
      if (isSqliteError(exc)) this.discard(which);
      throw exc;
    }
  }

  /**
   * Run fn(entry) in a BEGIN IMMEDIATE transaction. The write lock is taken without SQLite's busy handler: while
   * another process holds it, the attempt is repeated every BUSY_RETRY_MS between turns of the event loop, up to
   * the busy timeout, and then fails as a busy database (starting the pause). Each attempt runs synchronously from
   * BEGIN to COMMIT, so nothing else in this process can use the connection in between.
   */
  async transaction(which, fn) {
    const deadline = performance.now() + this.busyTimeoutMs;
    for (;;) {
      const attempt = this.guarded(which, (entry) => {
        entry.db.exec("PRAGMA busy_timeout=0");
        try {
          entry.db.exec("BEGIN IMMEDIATE");
        } catch (exc) {
          if (isBusy(exc)) return BUSY;
          throw exc;
        } finally {
          entry.db.exec(`PRAGMA busy_timeout=${Math.trunc(this.busyTimeoutMs)}`); // for the statements inside
        }
        try {
          const result = fn(entry);
          entry.db.exec("COMMIT");
          return { result };
        } catch (exc) {
          try {
            entry.db.exec("ROLLBACK");
          } catch {
            // keep the original error
          }
          throw exc;
        }
      });
      if (attempt !== BUSY) return attempt.result;
      if (performance.now() >= deadline) {
        this.busyUntil = performance.now() + BUSY_BACKOFF_MS;
        throw lockedError();
      }
      await sleep(BUSY_RETRY_MS);
    }
  }

  /**
   * Count this request against its budgets, in order; the counter name of the first one exceeded (the later ones
   * are left untouched), else null.
   */
  overBudget(which, entry, limits, nowMs) {
    const minute = Math.floor(nowMs / 60000);
    let refused = null;
    for (const b of limits.budgets) {
      if (!activeBudget(b)) continue;
      this.stmt(entry, SQL.rateInsert).run(b.key, minute);
      this.stmt(entry, SQL.rateUpdate).run(b.amount, b.key, minute);
      const used = this.stmt(entry, SQL.rateSelect).get(b.key, minute).hits;
      if (used > b.limit) {
        refused = b.counter;
        break;
      }
    }
    const now = performance.now();
    const last = this.lastPrune.get(which);
    if (last === undefined || now - last > PRUNE_EVERY_MS) { // old minutes, a bounded batch at a time
      this.lastPrune.set(which, now);
      this.stmt(entry, SQL.ratePrune).run(minute - 60);
    }
    return refused;
  }

  /** True when this request's hits would take its site beyond today's quota (the counters row "hits"). */
  overQuota(entry, limits, day) {
    if (!(limits.quota > 0) || !(limits.hits > 0)) return false;
    const row = this.stmt(entry, SQL.quotaSelect).get(day, limits.site);
    return (row ? row.n : 0) + limits.hits > limits.quota;
  }

  count(entry, day, siteId, name, n = 1) {
    this.stmt(entry, SQL.counterInsert).run(day, siteId || "", name);
    this.stmt(entry, SQL.counterUpdate).run(n, day, siteId || "", name);
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

  async writeRequest(rec, limits = NO_LIMITS) {
    const day = utcDay(rec.received_at);
    return this.transaction("events", (entry) => {
      let refused = this.overBudget("events", entry, limits, rec.received_at);
      if (refused === null && this.overQuota(entry, limits, day)) refused = "over_quota";
      if (refused !== null) {
        this.count(entry, day, limits.site, refused);
        return RATE_LIMITED;
      }
      const requestId = this.insertRequest(entry, rec);
      this.insertHits(entry, requestId, rec);
      if (limits.site && limits.hits > 0) this.count(entry, day, limits.site, "hits", limits.hits);
      return STORED;
    });
  }

  /** A refused chunk is counted in events.db (replay.db has no counters): a separate, best-effort write. */
  async countRefusedChunk(day, site, name) {
    try {
      await this.recordCounter(day, site, name);
    } catch (exc) {
      if (!isSqliteError(exc)) throw exc;
      if (this.errors) this.errors.exception("could not count a refused chunk");
    }
  }

  async writeChunk(rec, limits = NO_LIMITS) {
    const refused = await this.transaction("replay", (entry) => {
      const over = this.overBudget("replay", entry, limits, rec.received_at);
      if (over === null) this.insertChunk(entry, rec);
      return over;
    });
    if (refused === null) return STORED;
    await this.countRefusedChunk(utcDay(rec.received_at), limits.site, refused);
    return RATE_LIMITED;
  }

  /** Count something; with budgets, only while they hold (the budgets are consumed either way). */
  async recordCounter(day, siteId, name, n = 1, limits = NO_LIMITS) {
    await this.transaction("events", (entry) => {
      if (this.overBudget("events", entry, limits, Date.now()) === null) this.count(entry, day, siteId, name, n);
    });
  }

  /** What the current minute has used of these allowance keys, in the stream's database. Read only. */
  async usage(stream, keys, nowMs) {
    if (!keys.length) return {};
    const which = stream === "events" ? "events" : "replay";
    return this.guarded(which, (entry) => {
      const rows = this.stmt(entry, `SELECT key, hits FROM rate_limits WHERE minute = ? AND key IN (${keys.map(() => "?").join(",")})`)
        .all(Math.floor(nowMs / 60000), ...keys);
      return Object.fromEntries(rows.map((r) => [r.key, r.hits]));
    });
  }

  /**
   * Charge a request refused before its body was parsed (its client was already over an allowance) and count the
   * refusal. The counter name, or null if the allowance had just been renewed (a new minute).
   */
  async refuse(stream, limits, nowMs) {
    const which = stream === "events" ? "events" : "replay";
    const day = utcDay(nowMs);
    const refused = await this.transaction(which, (entry) => {
      const over = this.overBudget(which, entry, limits, nowMs);
      if (over !== null && which === "events") this.count(entry, day, limits.site, over);
      return over;
    });
    if (refused !== null && which === "replay") await this.countRefusedChunk(day, limits.site, refused);
    return refused;
  }

  async healthy() {
    if (this.backingOff()) return false;
    try {
      const entry = this.conn("events");
      this.stmt(entry, "SELECT 1 FROM raw_requests LIMIT 1").all();
      fs.accessSync(this.dataDir, fs.constants.W_OK);
      return true;
    } catch (exc) {
      if (isBusy(exc)) this.busyUntil = performance.now() + BUSY_BACKOFF_MS;
      this.discard("events");
      return false;
    }
  }
}

/**
 * Appends one JSON line per record to a per-process file (no locking needed).
 * The allowances are approximate (per process, in memory) in this mode.
 */
export class SpoolStore {
  constructor(dataDir) {
    this.dataDir = dataDir;
    this.root = path.join(dataDir, "spool");
    this.host = os.hostname().split(".")[0] || "host";
    this.label = "spool";
    this.fds = new Map(); // `${stream}/${day}` → fd
    this.rate = new Map(); // minute → Map(`${stream}\0${budget key}` → amount used)
    this.quota = new Map(); // `${day}\0${site}` → hits stored
    this.refusals = new Map(); // `${day}\0${site}\0${name}` → {day, site, name, n}: refusals not yet written
    this.refusalsAt = -Infinity; // performance.now() of the last write of refusals
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

  /**
   * As SQLiteStore.overBudget, in memory: per process, so only approximate with several processes. The allowances
   * of each stream ("events": requests and counters, "chunks") are separate, as those of events.db and replay.db
   * are. With a day, also the site's daily quota, counting the hits this process stored.
   */
  overBudget(limits, nowMs, stream, quotaDay = null) {
    const minute = Math.floor(nowMs / 60000);
    if (this.rate.size > 1 || !this.rate.has(minute)) { // only the current minute is ever needed
      for (const m of [...this.rate.keys()]) if (m < minute) this.rate.delete(m);
    }
    let used = this.rate.get(minute);
    if (!used) this.rate.set(minute, (used = new Map()));
    for (const b of limits.budgets) {
      if (!activeBudget(b)) continue;
      const k = `${stream}\u0000${b.key}`;
      const n = (used.get(k) || 0) + b.amount;
      used.set(k, n);
      if (n > b.limit) return b.counter;
    }
    if (quotaDay && limits.quota > 0 && limits.hits > 0) {
      const key = `${quotaDay}\u0000${limits.site}`;
      const stored = this.quota.get(key) || 0;
      if (stored + limits.hits > limits.quota) return "over_quota";
      if (this.quota.size > 10000) {
        for (const k of [...this.quota.keys()]) if (!k.startsWith(quotaDay + "\u0000")) this.quota.delete(k);
      }
      this.quota.set(key, stored + limits.hits);
    }
    return null;
  }

  /**
   * Count a refusal. Counts are written at most once a minute per process (summed), so a refused flood does not
   * grow the spool line by line; close() writes what is pending.
   */
  refused(day, site, name) {
    const key = `${day}\u0000${site}\u0000${name}`;
    const entry = this.refusals.get(key);
    if (entry) entry.n += 1;
    else this.refusals.set(key, { day, site, name, n: 1 });
    if (performance.now() - this.refusalsAt >= 60000 || this.refusals.size > 1000) this.flushRefusals();
  }

  flushRefusals() {
    const pending = this.refusals;
    this.refusals = new Map();
    this.refusalsAt = performance.now();
    for (const { day, site, name, n } of pending.values()) {
      this.append("events", Date.now(), { k: "counter", day, site_id: site, name, n });
    }
  }

  async writeRequest(rec, limits = NO_LIMITS) {
    const day = utcDay(rec.received_at);
    const refused = this.overBudget(limits, rec.received_at, "events", day);
    if (refused !== null) {
      this.refused(day, limits.site, refused);
      return RATE_LIMITED;
    }
    this.append("events", rec.received_at, { k: "request", rec: rec.toDict() });
    return STORED;
  }

  async writeChunk(rec, limits = NO_LIMITS) {
    const refused = this.overBudget(limits, rec.received_at, "chunks");
    if (refused !== null) {
      this.refused(utcDay(rec.received_at), limits.site, refused);
      return RATE_LIMITED;
    }
    this.append("chunks", rec.received_at, { k: "chunk", rec: rec.toDict() });
    return STORED;
  }

  async usage(stream, keys, nowMs) {
    const used = this.rate.get(Math.floor(nowMs / 60000)) || new Map();
    const out = {};
    for (const k of keys) {
      const n = used.get(`${stream}\u0000${k}`);
      if (n !== undefined) out[k] = n;
    }
    return out;
  }

  async refuse(stream, limits, nowMs) {
    const refused = this.overBudget(limits, nowMs, stream);
    if (refused !== null) this.refused(utcDay(nowMs), limits.site, refused);
    return refused;
  }

  async recordCounter(day, siteId, name, n = 1, limits = NO_LIMITS) {
    if (this.overBudget(limits, Date.now(), "events") !== null) return;
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
    try {
      this.flushRefusals();
    } catch {
      // the spool is unwritable: nothing more can be done
    }
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
  constructor(primary, spool, log = null, errors = null) {
    this.primary = primary;
    this.spool = spool;
    this.label = primary.label;
    this.log = log;
    this.errors = errors || (log ? new ErrorLog(log) : null); // logged on every request while SQLite fails
  }

  async call(method, args) {
    try {
      return await this.primary[method](...args);
    } catch (exc) {
      if (!isSqliteError(exc)) throw exc;
      if (this.errors) this.errors.exception(`SQLite write failed (${exc.message}); writing to the spool instead`);
      let outcome;
      try {
        outcome = await this.spool[method](...args);
      } catch (exc2) {
        throw new StorageError(`could not store the record: ${exc.message} / ${exc2.message}`, { cause: exc2 });
      }
      return outcome === STORED ? SPOOLED : outcome; // RATE_LIMITED: only a counter was written
    }
  }

  writeRequest(rec, limits = NO_LIMITS) {
    return this.call("writeRequest", [rec, limits]);
  }

  writeChunk(rec, limits = NO_LIMITS) {
    return this.call("writeChunk", [rec, limits]);
  }

  async recordCounter(day, siteId, name, n = 1, limits = NO_LIMITS) {
    try {
      await this.primary.recordCounter(day, siteId, name, n, limits);
    } catch (exc) {
      if (!isSqliteError(exc)) throw exc;
      await this.spool.recordCounter(day, siteId, name, n, limits);
    }
  }

  async usage(stream, keys, nowMs) {
    try {
      return await this.primary.usage(stream, keys, nowMs);
    } catch (exc) {
      if (!isSqliteError(exc)) throw exc;
      return this.spool.usage(stream, keys, nowMs);
    }
  }

  async refuse(stream, limits, nowMs) {
    try {
      return await this.primary.refuse(stream, limits, nowMs);
    } catch (exc) {
      if (!isSqliteError(exc)) throw exc;
      return this.spool.refuse(stream, limits, nowMs);
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
  busyTimeoutMs = 10000, spoolFallback = true, log = null, errors = null) {
  if (storageMode === "spool") return new SpoolStore(dataDir);
  const primary = new SQLiteStore(dataDir, journalMode, synchronous, busyTimeoutMs, log, errors);
  if (spoolFallback) return new FallbackStore(primary, new SpoolStore(dataDir), log, primary.errors);
  return primary;
}
