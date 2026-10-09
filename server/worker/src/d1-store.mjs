/**
 * Raw storage on Cloudflare D1: the same rows as server/python/tidesight/storage.py's SQLiteStore, written
 * through the D1 bindings EVENTS (events.db schema) and REPLAY (replay.db schema).
 *
 * D1 has no interactive transactions, so a stored request is ONE db.batch(): an atomic transaction whose
 * statements run in order. The decisions Python takes between statements (SQLiteStore._over_budget, _over_quota)
 * are written into the SQL instead:
 *
 *   1. one upsert per budget into rate_limits(key, minute), each only WHERE every earlier budget is still within
 *      its limit (Python stops at the first budget exceeded and leaves the later ones untouched);
 *   2. SELECT the refusal R: the counter of the first budget over its limit; else 'over_quota' when the request's
 *      hits would take its site past today's quota (counters row 'hits'); else NULL. The same SELECT returns each
 *      budget's use this minute and the site's hits today, which this isolate remembers (below);
 *   3. count R in counters                      ... only WHERE R IS NOT NULL
 *   4. insert the raw_requests row              ... only WHERE R IS NULL
 *   5. insert the raw_hits rows                 ... only WHERE R IS NULL, request_id = (SELECT MAX(id) FROM
 *      raw_requests), from a JSON array expanded by json_each (one bound parameter instead of 15 per event: D1
 *      allows 100 parameters per statement)
 *   6. add the stored hits to counters 'hits'   ... only WHERE R IS NULL
 *
 * Each statement re-evaluates R from the database. Its inputs do not change after step 1: no later statement
 * writes this minute's rate_limits rows (the prune deletes rows an hour old), and the 'hits' row is written only
 * by step 6, whose WHERE SQLite evaluates before writing. Every budget of a request has its own row (the keys
 * carry prefixes: r:, kb:, f: and *f), so a row's final count is the count its check saw. Every statement
 * therefore sees the same R as step 2, which tells the Worker the outcome.
 *
 * A chunk is steps 1, 2 and the chunks insert over REPLAY. A counter-only write (a DNT/GPC drop, an oversize
 * chunk) is steps 1, 2 and the counter, written only WHERE R IS NULL.
 *
 * Refusals cost no D1 writes once this isolate knows the client is over (D1's free plan allows 100,000 rows
 * written a day: a refused flood must not use them up). The isolate remembers, for the current minute, what each
 * allowance key has used (step 2's answers, plus what it refused in memory) and, for today, each site's stored
 * hits. A request those numbers already refuse is refused without D1, charged in memory only; the early refusal
 * (usage/refuse: a client over its allowance is refused before its body is parsed) works from the same numbers.
 * Refusals found by D1 are counted in their batch (step 3). Refusals found in memory are counted at once for the
 * first 5 of each client (or quota-bound site) and 50 in all per minute, so that small numbers stay exact; beyond
 * those they are summed in memory and written at most once a minute, at most 20 counters at a time (an evicted
 * isolate loses its unwritten sums). Separate isolates keep separate numbers: each learns from its own batches.
 *
 * A row D1 cannot hold (2,000,000 bytes) is never sent: the request is stored as its metadata only, like an oversize
 * body (site_status and error "too_large"), charged as it was, and answered 413; a chunk is refused with 413 and
 * counted as "chunk_too_large". A failed batch changes nothing; the budgets are then charged by a batch of their
 * own, so that a request D1 fails to store still counts against its allowances, and StorageError makes the
 * collector answer 503 (the tracker retries). There is no spool fallback: a Worker has no filesystem.
 */

import { ErrorLog } from "../../node/core/errorlog.mjs";
import { utcDay } from "../../node/core/pycompat.mjs";
import {
  NO_LIMITS, RATE_LIMITED, RequestRecord, STORED, StorageError, TOO_LARGE, activeBudget, jsonOrNull,
} from "../../node/core/records.mjs";
import { INSERT_CHUNK_COLUMNS, INSERT_HIT_COLUMNS, INSERT_REQUEST_COLUMNS } from "../../node/core/schema.mjs";

const USED = "(SELECT hits FROM rate_limits WHERE key = ? AND minute = ?)";
const HITS_TODAY = "COALESCE((SELECT n FROM counters WHERE day = ? AND site_id = ? AND name = 'hits'), 0)";
const upsertBudget = (cond) => `INSERT INTO rate_limits(key, minute, hits) SELECT ?, ?, ? WHERE ${cond} ` +
  "ON CONFLICT(key, minute) DO UPDATE SET hits = hits + excluded.hits";
const OVER_QUOTA = `${HITS_TODAY} + ? > ?`;
const countRefusal = (refusal) => `INSERT INTO counters(day, site_id, name, n) SELECT ?, ?, r, 1 FROM (SELECT ${refusal} ` +
  "AS r) WHERE r IS NOT NULL ON CONFLICT(day, site_id, name) DO UPDATE SET n = n + 1";
const countHits = (cond) => `INSERT INTO counters(day, site_id, name, n) SELECT ?, ?, 'hits', ? WHERE ${cond} ` +
  "ON CONFLICT(day, site_id, name) DO UPDATE SET n = n + excluded.n";
const countIf = (cond) => `INSERT INTO counters(day, site_id, name, n) SELECT ?, ?, ?, ? WHERE ${cond} ` +
  "ON CONFLICT(day, site_id, name) DO UPDATE SET n = n + excluded.n";
const COUNT = countIf("1");
// Old minutes, a bounded batch at a time (storage.SQLiteStore._over_budget).
const PRUNE = "DELETE FROM rate_limits WHERE (key, minute) IN (SELECT key, minute FROM rate_limits " +
  "WHERE minute < ? LIMIT 20000)";

const placeholders = (n) => Array(n).fill("?").join(", ");
const INSERT_REQUEST = `INSERT INTO raw_requests(${INSERT_REQUEST_COLUMNS}) SELECT ${placeholders(18)}`;
const HIT_FIELDS = 11; // seq, type, name, client_ts, event_id, visitor_id, session_id, page_id, user_id, url, data
const INSERT_HITS_JSON = `INSERT INTO raw_hits(${INSERT_HIT_COLUMNS}) SELECT (SELECT MAX(id) FROM raw_requests), ` +
  "json_extract(value, '$[0]'), ?, ?, ?, " +
  Array.from({ length: HIT_FIELDS - 1 }, (_, i) => `json_extract(value, '$[${i + 1}]')`).join(", ") +
  " FROM json_each(?)";
const INSERT_HIT_ONE = `INSERT INTO raw_hits(${INSERT_HIT_COLUMNS}) SELECT (SELECT MAX(id) FROM raw_requests), ${placeholders(14)}`;
const INSERT_CHUNK = `INSERT INTO chunks(${INSERT_CHUNK_COLUMNS}) SELECT ${placeholders(15)}`;

// D1 refuses any value or row over 2,000,000 bytes: rows are kept below this (room for the record header), and
// JSON arrays of hits are split well below it.
const ROW_BYTES = 1990000;
const GROUP_BYTES = 900000;

const PRUNE_EVERY_MS = 60000; // per database and isolate, as Python prunes at most once a minute per process
const IMMEDIATE_PER_KEY = 5; // refusals found in memory counted at once, per client (or site) and minute...
const IMMEDIATE_PER_MINUTE = 50; // ...and in all, per isolate and minute
const FLUSH_EVERY_MS = 60000; // summed refusal counts are written at most this often...
const FLUSH_MAX = 20; // ...at most this many counters at a time (D1 counts each statement against its query limits)
const MAX_KEYS = 20000; // allowance keys (and refusal keys) remembered per minute
const MAX_PENDING = 1000; // distinct summed counters before a flush is due anyway

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

/** Whether a row of these values (as bound) exceeds what D1 stores; exact only when it could. */
function rowTooLarge(values) {
  let bound = 0;
  for (const v of values) {
    if (typeof v === "string") bound += v.length * 3;
    else if (v instanceof Uint8Array) bound += v.byteLength;
    else bound += 9;
  }
  if (bound <= ROW_BYTES) return false;
  let n = 0;
  for (const v of values) {
    if (typeof v === "string") n += utf8Length(v);
    else if (v instanceof Uint8Array) n += v.byteLength;
    else n += 9;
  }
  return n > ROW_BYTES;
}

function hitFields(h) {
  return [h.seq, h.type, h.name, h.client_ts, h.event_id, h.visitor_id, h.session_id, h.page_id, h.user_id, h.url, h.data];
}

function requestValues(rec) {
  return [
    rec.received_at, rec.site_id, rec.site_status, rec.endpoint, rec.method, rec.ip, jsonOrNull(rec.headers) || "{}",
    jsonOrNull(rec.cookies), rec.query, rec.content_type, rec.body_bytes, rec.body_encoding, jsonOrNull(rec.envelope),
    rec.body, rec.error, rec.proto_v, rec.hits.length, jsonOrNull(rec.server),
  ];
}

function chunkValues(rec) {
  return [
    rec.received_at, rec.site_id, rec.site_ok, rec.kind, rec.visitor_id, rec.session_id, rec.page_id, rec.seq,
    rec.client_ts, rec.url, rec.ip, jsonOrNull(rec.meta) || "{}", rec.encoding, rec.payload, rec.error,
  ];
}

/** What is kept of a request D1 cannot hold: its metadata, as for an oversize body. */
function metadataOnly(rec) {
  return new RequestRecord({
    received_at: rec.received_at, endpoint: rec.endpoint, method: rec.method, site_id: rec.site_id,
    site_status: "too_large", ip: rec.ip, headers: rec.headers, cookies: rec.cookies, query: rec.query,
    content_type: rec.content_type, body_bytes: rec.body_bytes, server: rec.server, error: "too_large",
  });
}

/** An SQL fragment and the values of its parameters, in order. */
const fragment = (sql, params = []) => ({ sql, params });
const ALWAYS = fragment("1");

const refusalOf = (row) => (row && row.refused != null ? String(row.refused) : null);

export class D1Store {
  /**
   * @param events D1Database (events.db schema)  @param replay D1Database (replay.db schema)
   * @param [log] {info, warning, error(msg, exc)}
   * @param [errors] ErrorLog shared with the collector (one throttle for the isolate's logged errors)
   */
  constructor(events, replay, log = null, errors = null) {
    this.events = events;
    this.replay = replay;
    this.log = log;
    this.errors = errors || (log ? new ErrorLog(log) : null);
    this.label = "d1";
    this.lastPrune = new Map(); // database → Date.now() of its last rate_limits prune in this isolate
    this.minute = -1; // the minute the numbers below are for
    // `${stream}\0${key}` → {d1, mem}: this minute's use of an allowance key as D1 last answered (it only grows),
    // and what this isolate charged in memory only (refusals); their sum is at most the use Python would count.
    this.used = new Map();
    this.immediate = new Map(); // refusal key → refusals found in memory and counted at once this minute
    this.immediateTotal = 0;
    this.hitsToday = new Map(); // `${site}\0${day}` → hits the site has stored that day, at least
    this.pending = new Map(); // `${day}\0${site}\0${name}` → refusals counted in memory, not yet written
    this.flushedAt = 0;
  }

  error(msg, exc) {
    if (this.errors) this.errors.exception(msg, exc);
  }

  // -- what this isolate knows -----------------------------------------------

  /** Move to `minute`; false when the request belongs to an earlier minute (its numbers are gone). */
  roll(minute) {
    if (minute > this.minute) {
      this.minute = minute;
      this.used.clear();
      this.immediate.clear();
      this.immediateTotal = 0;
    }
    return minute === this.minute;
  }

  entry(stream, key) {
    const k = `${stream}\0${key}`;
    let e = this.used.get(k);
    if (!e && this.used.size < MAX_KEYS) this.used.set(k, (e = { d1: 0, mem: 0 }));
    return e || null;
  }

  /** This minute's use of a key as far as this isolate knows, or undefined. */
  usedOf(stream, key) {
    const e = this.used.get(`${stream}\0${key}`);
    return e ? e.d1 + e.mem : undefined;
  }

  learn(stream, key, value) {
    if (value === null || value === undefined) return;
    const e = this.entry(stream, key);
    if (e) e.d1 = Math.max(e.d1, Number(value));
  }

  /** Charge an allowance key in memory; its use after the charge. */
  charge(stream, key, amount) {
    const e = this.entry(stream, key);
    if (!e) return (this.usedOf(stream, key) ?? 0) + amount;
    e.mem += amount;
    return e.d1 + e.mem;
  }

  learnHits(site, day, n) {
    const k = `${site}\0${day}`;
    if (this.hitsToday.size >= 1000 && !this.hitsToday.has(k)) {
      for (const key of this.hitsToday.keys()) if (!key.endsWith(`\0${day}`)) this.hitsToday.delete(key);
      if (this.hitsToday.size >= 1000) return;
    }
    this.hitsToday.set(k, Math.max(this.hitsToday.get(k) ?? 0, Number(n)));
  }

  /**
   * The refusal these numbers already make certain (they only ever undercount): the counter of the first budget
   * they put over its limit, else 'over_quota'; null when D1 must decide. A refusal charges the budgets up to the
   * one exceeded (all of them for the quota) in memory, as Python charges them in the database.
   */
  predict(stream, limits, quotaDay) {
    const active = limits.budgets.filter(activeBudget);
    let at = -1;
    for (let i = 0; i < active.length; i++) {
      const used = this.usedOf(stream, active[i].key);
      if (used !== undefined && used + active[i].amount > active[i].limit) {
        at = i;
        break;
      }
    }
    let refused = at >= 0 ? active[at].counter : null;
    if (refused === null && quotaDay !== null && limits.quota > 0 && limits.hits > 0) {
      const today = this.hitsToday.get(`${limits.site}\0${quotaDay}`);
      if (today !== undefined && today + limits.hits > limits.quota) refused = "over_quota";
    }
    if (refused === null) return null;
    const upto = at >= 0 ? at : active.length - 1;
    for (let i = 0; i <= upto; i++) this.charge(stream, active[i].key, active[i].amount);
    return refused;
  }

  // -- refusal counts ----------------------------------------------------------

  /** Whose allowance of immediate counts a refusal uses: the client's (its first key), or the site's for its quota. */
  static refusalKey(limits, name) {
    if (name === "over_quota") return `quota\0${limits.site}`;
    const first = limits.budgets.find(activeBudget);
    return first ? first.key : "";
  }

  /** Count a refusal found in memory: at once while the per-minute allowances last, else summed. */
  async countRefusal(day, site, name, key) {
    const n = this.immediate.get(key) ?? 0;
    if (n < IMMEDIATE_PER_KEY && this.immediateTotal < IMMEDIATE_PER_MINUTE &&
        (n > 0 || this.immediate.size < MAX_KEYS)) {
      this.immediate.set(key, n + 1);
      this.immediateTotal += 1;
      try {
        await this.events.prepare(COUNT).bind(day, site || "", name, 1).run();
        return;
      } catch (exc) {
        this.error(`could not record counter ${name}`, exc); // summed with the others instead
      }
    }
    const k = `${day}\0${site || ""}\0${name}`;
    this.pending.set(k, (this.pending.get(k) ?? 0) + 1);
    const due = this.takeDue();
    if (!due) return;
    try {
      await this.events.batch(this.flushStatements(due));
    } catch (exc) {
      this.restore(due);
      this.error("could not write the refusal counts", exc);
    }
  }

  /** A refusal D1 found and its batch counted: it uses the immediate allowance too. */
  noteCounted(key) {
    if (!this.immediate.has(key) && this.immediate.size >= MAX_KEYS) return;
    this.immediate.set(key, (this.immediate.get(key) ?? 0) + 1);
    this.immediateTotal += 1;
  }

  /** The summed counts due for writing (at most FLUSH_MAX), taken out of `pending`; null when none are due. */
  takeDue() {
    if (!this.pending.size) return null;
    const now = Date.now();
    if (now - this.flushedAt < FLUSH_EVERY_MS && this.pending.size <= MAX_PENDING) return null;
    this.flushedAt = now;
    const due = [];
    for (const entry of this.pending) {
      due.push(entry);
      if (due.length >= FLUSH_MAX) break;
    }
    for (const [k] of due) this.pending.delete(k);
    return due;
  }

  restore(due) {
    for (const [k, n] of due) this.pending.set(k, (this.pending.get(k) ?? 0) + n);
  }

  flushStatements(due) {
    return due.map(([k, n]) => {
      const [day, site, name] = k.split("\0");
      return this.events.prepare(COUNT).bind(day, site, name, n);
    });
  }

  // -- batches -------------------------------------------------------------------

  /**
   * Steps 1 and 2: the budget upserts, the refusal expression R (null when no allowance applies) and the decision
   * SELECT (null likewise), which also returns each budget's use (u0, u1, ...) and, with a quota, the site's hits
   * today before this request (today).
   */
  allowance(db, limits, minute, quotaDay = null) {
    const stmts = [];
    const within = []; // "used <= limit" of each earlier budget: an upsert happens only while they all hold
    const whens = [];
    const keys = [];
    for (const b of limits.budgets) {
      if (!activeBudget(b)) continue;
      const cond = within.length ? within.map((w) => w.sql).join(" AND ") : "1";
      stmts.push(db.prepare(upsertBudget(cond)).bind(...[b.key, minute, b.amount, ...within.flatMap((w) => w.params)]
        .map(bindable)));
      within.push(fragment(`${USED} <= ?`, [b.key, minute, b.limit]));
      whens.push(fragment(`WHEN ${USED} > ? THEN ?`, [b.key, minute, b.limit, b.counter]));
      keys.push(b.key);
    }
    const quota = quotaDay !== null && limits.quota > 0 && limits.hits > 0;
    if (quota) {
      whens.push(fragment(`WHEN ${OVER_QUOTA} THEN 'over_quota'`, [quotaDay, limits.site, limits.hits, limits.quota]));
    }
    if (!whens.length) return { stmts, refusal: null, decision: null, keys, quota };
    const refusal = fragment(`(CASE ${whens.map((w) => w.sql).join(" ")} END)`, whens.flatMap((w) => w.params).map(bindable));
    const cols = [`${refusal.sql} AS refused`, ...keys.map((_, i) => `${USED} AS u${i}`)];
    const params = [...refusal.params, ...keys.flatMap((k) => [k, minute])];
    if (quota) {
      cols.push(`${HITS_TODAY} AS today`);
      params.push(quotaDay, limits.site);
    }
    const decision = db.prepare(`SELECT ${cols.join(", ")}`).bind(...params.map(bindable));
    return { stmts, refusal, decision, keys, quota };
  }

  /** The occasional prune of old minutes, at most once a minute per database and isolate. */
  prune(name, db, minute) {
    const now = Date.now();
    const last = this.lastPrune.get(name);
    if (last !== undefined && now - last < PRUNE_EVERY_MS) return [];
    this.lastPrune.set(name, now);
    return [db.prepare(PRUNE).bind(minute - 60)];
  }

  /**
   * Run one batch built by `build` (→ [{stmt, decision?}]); the decision row, or null. On failure the budgets are
   * charged by a batch of their own (best effort) and StorageError is raised.
   */
  async batch(db, limits, minute, build) {
    let stmts;
    let results;
    try {
      stmts = build();
      results = await db.batch(stmts.map((s) => s.stmt));
    } catch (exc) {
      try {
        const { stmts: charges } = this.allowance(db, limits, minute);
        if (charges.length) await db.batch(charges);
      } catch {
        // D1 is failing: nothing more to do
      }
      throw new StorageError(`D1 write failed: ${exc && exc.message}`, { cause: exc });
    }
    const at = stmts.findIndex((s) => s.decision);
    if (at < 0) return null;
    const res = results[at];
    return (res && res.results && res.results[0]) || null;
  }

  /** Remember what a decision row says this minute's use of each key is. */
  learnAll(stream, keys, row, current) {
    if (!row || !current) return;
    keys.forEach((key, i) => this.learn(stream, key, row[`u${i}`]));
  }

  async writeRequest(rec, limits = NO_LIMITS) {
    const db = this.events;
    const day = utcDay(rec.received_at);
    const minute = Math.floor(rec.received_at / 60000);
    const current = this.roll(minute);
    if (current) {
      const predicted = this.predict("events", limits, day);
      if (predicted !== null) {
        await this.countRefusal(day, limits.site, predicted, D1Store.refusalKey(limits, predicted));
        return RATE_LIMITED;
      }
    }
    let row = rec;
    let values = requestValues(rec);
    let tooLarge = rowTooLarge(values) || rec.hits.some((h) => rowTooLarge(hitFields(h).concat([rec.site_id])));
    if (tooLarge) {
      row = metadataOnly(rec);
      values = requestValues(row);
    }
    const hits = tooLarge ? 0 : limits.hits; // a metadata record adds no hits: no quota, no count
    const effective = { ...limits, hits };
    let due = null;
    let parts;
    const decided = await this.batch(db, limits, minute, () => {
      parts = this.allowance(db, effective, minute, day);
      const { stmts, refusal, decision } = parts;
      const out = stmts.map((stmt) => ({ stmt }));
      const ok = refusal ? fragment(`${refusal.sql} IS NULL`, refusal.params) : ALWAYS;
      if (decision) {
        out.push({ stmt: decision, decision: true });
        out.push({ stmt: db.prepare(countRefusal(refusal.sql)).bind(day, limits.site, ...refusal.params) });
      }
      out.push({ stmt: db.prepare(`${INSERT_REQUEST} WHERE ${ok.sql}`).bind(...values.map(bindable), ...ok.params) });

      const siteOk = row.site_ok;
      let group = [];
      let groupBytes = 0;
      const flush = () => {
        if (!group.length) return;
        out.push({ stmt: db.prepare(`${INSERT_HITS_JSON} WHERE ${ok.sql} ORDER BY key`)
          .bind(row.received_at, bindable(row.site_id), siteOk, "[" + group.join(",") + "]", ...ok.params) });
        group = [];
        groupBytes = 0;
      };
      for (const h of row.hits) {
        const json = "[" + hitFields(h).map(jsonValue).join(",") + "]";
        const bytes = utf8Length(json);
        if (bytes > GROUP_BYTES) { // a very large event: its own statement, values bound directly
          flush();
          const [seq, ...rest] = hitFields(h);
          out.push({ stmt: db.prepare(`${INSERT_HIT_ONE} WHERE ${ok.sql}`)
            .bind(...[seq, row.received_at, row.site_id, siteOk, ...rest].map(bindable), ...ok.params) });
          continue;
        }
        if (groupBytes + bytes > GROUP_BYTES) flush();
        group.push(json);
        groupBytes += bytes + 1;
      }
      flush();
      if (limits.site && hits > 0) {
        out.push({ stmt: db.prepare(countHits(ok.sql)).bind(day, limits.site, hits, ...ok.params) });
      }
      out.push(...this.prune("events", db, minute).map((stmt) => ({ stmt })));
      due = this.takeDue(); // summed refusal counts ride along
      if (due) out.push(...this.flushStatements(due).map((stmt) => ({ stmt })));
      return out;
    }).catch((exc) => {
      if (due) this.restore(due);
      throw exc;
    });
    this.learnAll("events", parts.keys, decided, current);
    const refused = refusalOf(decided);
    if (parts.quota && decided && decided.today != null) {
      this.learnHits(limits.site, day, Number(decided.today) + (refused === null ? hits : 0));
    }
    if (refused !== null) {
      this.noteCounted(D1Store.refusalKey(limits, refused));
      return RATE_LIMITED;
    }
    return tooLarge ? TOO_LARGE : STORED;
  }

  async writeChunk(rec, limits = NO_LIMITS) {
    const db = this.replay;
    const day = utcDay(rec.received_at);
    const minute = Math.floor(rec.received_at / 60000);
    const current = this.roll(minute);
    if (current) {
      const predicted = this.predict("chunks", limits, null);
      if (predicted !== null) {
        await this.countRefusal(day, limits.site, predicted, D1Store.refusalKey(limits, predicted));
        return RATE_LIMITED;
      }
    }
    const values = chunkValues(rec);
    const tooLarge = rowTooLarge(values);
    let parts;
    const decided = await this.batch(db, limits, minute, () => {
      parts = this.allowance(db, limits, minute);
      const { stmts, refusal, decision } = parts;
      const out = stmts.map((stmt) => ({ stmt }));
      const ok = refusal ? fragment(`${refusal.sql} IS NULL`, refusal.params) : ALWAYS;
      if (decision) out.push({ stmt: decision, decision: true });
      if (!tooLarge) {
        out.push({ stmt: db.prepare(`${INSERT_CHUNK} WHERE ${ok.sql}`).bind(...values.map(bindable), ...ok.params) });
      }
      out.push(...this.prune("replay", db, minute).map((stmt) => ({ stmt })));
      return out;
    });
    this.learnAll("chunks", parts.keys, decided, current);
    const refused = refusalOf(decided);
    // replay.db has no counters: refusals are counted in EVENTS, separately and best effort, as in Python.
    if (refused !== null) {
      await this.countRefusal(day, limits.site, refused, D1Store.refusalKey(limits, refused));
      return RATE_LIMITED;
    }
    if (tooLarge) {
      await this.countRefusal(day, limits.site, "chunk_too_large", D1Store.refusalKey(limits, "chunk_too_large"));
      return TOO_LARGE;
    }
    return STORED;
  }

  /** Count something; with budgets, only while they hold (the budgets are consumed either way). */
  async recordCounter(day, siteId, name, n = 1, limits = NO_LIMITS) {
    const db = this.events;
    const minute = Math.floor(Date.now() / 60000);
    const current = this.roll(minute);
    if (current && this.predict("events", limits, null) !== null) return; // over its allowance: not counted
    let due = null;
    let parts;
    const decided = await this.batch(db, limits, minute, () => {
      parts = this.allowance(db, limits, minute);
      const { stmts, refusal, decision } = parts;
      const out = stmts.map((stmt) => ({ stmt }));
      const ok = refusal ? fragment(`${refusal.sql} IS NULL`, refusal.params) : ALWAYS;
      if (decision) out.push({ stmt: decision, decision: true });
      out.push({ stmt: db.prepare(countIf(ok.sql)).bind(day, siteId || "", name, n, ...ok.params) });
      if (parts.keys.length) out.push(...this.prune("events", db, minute).map((stmt) => ({ stmt })));
      due = this.takeDue();
      if (due) out.push(...this.flushStatements(due).map((stmt) => ({ stmt })));
      return out;
    }).catch((exc) => {
      if (due) this.restore(due);
      throw exc;
    });
    this.learnAll("events", parts.keys, decided, current);
  }

  /** What this minute has used of these allowance keys, as far as this isolate knows (no D1 query). */
  async usage(stream, keys, nowMs) {
    if (!this.roll(Math.floor(nowMs / 60000))) return {};
    const out = {};
    for (const key of keys) {
      const used = this.usedOf(stream, key);
      if (used !== undefined) out[key] = used;
    }
    return out;
  }

  /**
   * Charge a request refused before its body was parsed and count the refusal: in memory (it costs no D1 write
   * beyond the counts above). The counter name, or null if the allowance had just been renewed (a new minute).
   */
  async refuse(stream, limits, nowMs) {
    if (!this.roll(Math.floor(nowMs / 60000))) return null;
    let refused = null;
    for (const b of limits.budgets) {
      if (!activeBudget(b)) continue;
      if (this.charge(stream, b.key, b.amount) > b.limit) {
        refused = b.counter;
        break;
      }
    }
    if (refused !== null) {
      await this.countRefusal(utcDay(nowMs), limits.site, refused, D1Store.refusalKey(limits, refused));
    }
    return refused;
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
