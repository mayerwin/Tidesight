/**
 * The Worker's D1 store on the D1 shim: refusals that cost no D1 writes, early refusal from what the isolate
 * knows, rows D1 cannot hold, failed batches that still charge the allowances, and the client address behind
 * Cloudflare's Pseudo IPv4.
 *
 *   node --test server/worker/test/d1-store.test.mjs        (from the repository root; or cd server/node && npm test)
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { RequestRecord, StorageError, budget, makeLimits } from "../../node/core/records.mjs";
import { D1Store } from "../src/d1-store.mjs";
import { clientAddress } from "../src/index.mjs";
import { D1Database } from "./d1-shim.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function database(name) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tidesight-d1-test-"));
  const db = new D1Database(path.join(dir, `${name}.db`));
  const migrations = path.join(HERE, "..", "migrations", name);
  for (const file of fs.readdirSync(migrations).filter((f) => /^\d+_.*\.sql$/.test(f)).sort()) {
    db.db.exec(fs.readFileSync(path.join(migrations, file), "utf8"));
  }
  return db;
}

function store() {
  const events = database("events");
  const replay = database("replay");
  const s = new D1Store(events, replay);
  const written = () => Number(events.db.prepare("SELECT total_changes() AS n").get().n) +
    Number(replay.db.prepare("SELECT total_changes() AS n").get().n);
  const counter = (name) => {
    const row = events.db.prepare("SELECT SUM(n) AS n FROM counters WHERE name = ?").get(name);
    return Number(row.n || 0);
  };
  return { s, events, replay, written, counter };
}

const request = (fields = {}) => new RequestRecord({ received_at: Date.now(), endpoint: "collect", method: "POST",
  site_id: "example", site_status: "ok", headers: new Map(), ...fields });

test("refused requests cost no D1 writes once the isolate knows the client is over", async () => {
  const { s, written, counter } = store();
  const limits = makeLimits({ budgets: [budget("r:h:client", 1, 2, "rate_limited")], site: "example" });
  assert.deepEqual([await s.writeRequest(request(), limits), await s.writeRequest(request(), limits)],
    ["stored", "stored"]);
  const before = written();
  const outcomes = [];
  for (let i = 0; i < 100; i++) outcomes.push(await s.writeRequest(request(), limits));
  assert.ok(outcomes.every((o) => o === "rate_limited"));
  // The first refusal is found by D1 (its allowance row and counter); the next 4 are counted at once, then the
  // rest are summed: one flush now, the others at most a minute later.
  assert.ok(written() - before <= 8, `rows written: ${written() - before}`);
  assert.equal(counter("rate_limited"), 6);
  s.flushedAt = -Infinity; // a minute later
  assert.equal(await s.writeRequest(request(), limits), "rate_limited");
  assert.equal(counter("rate_limited"), 101);
});

test("early refusal works from what the isolate knows", async () => {
  const { s, written, counter } = store();
  const now = Date.now();
  const limits = makeLimits({ budgets: [budget("r:h:c", 1, 1, "rate_limited"), budget("kb:h:c", 1, 10, "rate_limited")],
    site: "example" });
  assert.deepEqual(await s.usage("events", ["r:h:c", "kb:h:c"], now), {});
  assert.equal(await s.writeRequest(request(), limits), "stored");
  assert.deepEqual(await s.usage("events", ["r:h:c", "kb:h:c"], now), { "r:h:c": 1, "kb:h:c": 1 });
  assert.deepEqual(await s.usage("chunks", ["r:h:c"], now), {}); // replay.db's allowances are separate
  const before = written();
  assert.equal(await s.refuse("events", limits, now), "rate_limited");
  assert.equal(written() - before, 1); // the refusal's counter only
  assert.equal(counter("rate_limited"), 1);
  assert.deepEqual(await s.usage("events", ["r:h:c"], now), { "r:h:c": 2 }); // charged in memory
});

test("a row D1 cannot hold is stored as its metadata, charged, and answered too_large", async () => {
  const { s, events } = store();
  const limits = makeLimits({ budgets: [budget("r:h:big", 1, 100, "rate_limited")], site: "example", hits: 1 });
  const rec = request({ body_bytes: 7000, body_encoding: "gzip", envelope: new Map([["v", 1], ["pad", "y".repeat(2100000)]]),
    proto_v: 1, query: "site=example" });
  assert.equal(await s.writeRequest(rec, limits), "too_large");
  const row = events.db.prepare("SELECT * FROM raw_requests").get();
  assert.equal(row.site_status, "too_large");
  assert.equal(row.error, "too_large");
  assert.equal(row.envelope, null);
  assert.equal(row.body, null);
  assert.equal(row.event_count, 0);
  assert.equal(row.body_bytes, 7000);
  assert.equal(row.query, "site=example");
  assert.equal(events.db.prepare("SELECT hits FROM rate_limits WHERE key = 'r:h:big'").get().hits, 1);
  assert.equal(events.db.prepare("SELECT COUNT(*) AS n FROM counters WHERE name = 'hits'").get().n, 0);
});

test("a failed batch still charges the allowances", async () => {
  const { s, events } = store();
  const batch = events.batch.bind(events);
  events.batch = async (stmts) => {
    if (stmts.some((st) => st.sql.startsWith("INSERT INTO raw_requests"))) throw new Error("D1_ERROR: boom");
    return batch(stmts);
  };
  const limits = makeLimits({ budgets: [budget("r:h:retry", 1, 5, "rate_limited")], site: "example" });
  await assert.rejects(s.writeRequest(request(), limits), StorageError);
  assert.equal(events.db.prepare("SELECT hits FROM rate_limits WHERE key = 'r:h:retry'").get().hits, 1);
});

test("old minutes are pruned in bounded batches, at most once a minute", async () => {
  const { s, events } = store();
  const minute = Math.floor(Date.now() / 60000);
  const insert = events.db.prepare("INSERT INTO rate_limits(key, minute, hits) VALUES (?, ?, 1)");
  for (let i = 0; i < 50; i++) insert.run(`r:old${i}`, minute - 120);
  const limits = makeLimits({ budgets: [budget("r:h:p", 1, 100, "rate_limited")], site: "example" });
  await s.writeRequest(request(), limits);
  assert.equal(events.db.prepare("SELECT COUNT(*) AS n FROM rate_limits WHERE minute < ?").get(minute - 60).n, 0);
  for (let i = 0; i < 5; i++) insert.run(`r:older${i}`, minute - 120);
  await s.writeRequest(request(), limits); // within the minute: no second prune
  assert.equal(events.db.prepare("SELECT COUNT(*) AS n FROM rate_limits WHERE minute < ?").get(minute - 60).n, 5);
});

test("the client address: CF-Connecting-IP, or CF-Connecting-IPv6 behind a Pseudo IPv4 stand-in", () => {
  const h = (o) => new Headers(o);
  assert.equal(clientAddress(h({ "cf-connecting-ip": "203.0.113.7", "cf-connecting-ipv6": "2001:db8::1" })), "203.0.113.7");
  assert.equal(clientAddress(h({ "cf-connecting-ip": "240.12.0.9", "cf-connecting-ipv6": "2001:db8::1" })), "2001:db8::1");
  assert.equal(clientAddress(h({ "cf-connecting-ip": "240.12.0.9", "cf-connecting-ipv6": "junk" })), "240.12.0.9");
  assert.equal(clientAddress(h({ "cf-connecting-ip": "240.12.0.9" })), "240.12.0.9");
  assert.equal(clientAddress(h({ "cf-connecting-ip": "2001:db8::2" })), "2001:db8::2");
  assert.equal(clientAddress(h({})), null);
});
