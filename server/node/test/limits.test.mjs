/**
 * Unit tests for the Node connector's spool allowances and the error-log throttle (the conformance suite in
 * server/python/tests covers the rest over HTTP).
 *
 *   cd server/node && npm test        (or: node --test server/node/test/  from the repository root)
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { ErrorLog } from "../core/errorlog.mjs";
import { ChunkRecord, RequestRecord, budget, makeLimits } from "../core/records.mjs";
import { SpoolStore } from "../storage.mjs";

const T = 1728380000000;
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "tidesight-node-test-"));
const request = (ms = T) => new RequestRecord({ received_at: ms, endpoint: "collect", method: "POST",
  site_id: "example", site_status: "ok", headers: new Map() });

function spoolLines(dir) {
  const root = path.join(dir, "spool", "events");
  return fs.readdirSync(root).flatMap((d) => fs.readdirSync(path.join(root, d))
    .flatMap((f) => fs.readFileSync(path.join(root, d, f), "utf8").trim().split("\n"))).map((l) => JSON.parse(l));
}

test("spool: the allowances of events and chunks are separate (as events.db and replay.db)", async () => {
  const spool = new SpoolStore(tmp());
  const limits = makeLimits({ budgets: [budget("r:x", 1, 2, "rate_limited")], site: "example" });
  const requests = [];
  for (let i = 0; i < 3; i++) requests.push(await spool.writeRequest(request(), limits));
  assert.deepEqual(requests, ["stored", "stored", "rate_limited"]);
  const chunk = () => new ChunkRecord({ received_at: T, kind: "replay", site_id: "example", site_status: "ok",
    payload: new Uint8Array([120]), encoding: "identity", meta: new Map() });
  const chunks = [];
  for (let i = 0; i < 3; i++) chunks.push(await spool.writeChunk(chunk(), limits));
  assert.deepEqual(chunks, ["stored", "stored", "rate_limited"]);
  spool.close();
});

test("spool: counter-only writes use the events allowance", async () => {
  const dir = tmp();
  const spool = new SpoolStore(dir);
  const limits = makeLimits({ budgets: [budget("r:x", 1, 1, "")] });
  const day = new Date().toISOString().slice(0, 10);
  await spool.recordCounter(day, "example", "dropped_dnt", 1, limits);
  await spool.recordCounter(day, "example", "dropped_dnt", 1, limits); // over r:x
  spool.close();
  assert.equal(spoolLines(dir).filter((l) => l.k === "counter").length, 1);
});

test("spool: refusals are written summed", async () => {
  const dir = tmp();
  const spool = new SpoolStore(dir);
  const limits = makeLimits({ budgets: [budget("r:x", 1, 1, "rate_limited")], site: "example" });
  const outcomes = [];
  for (let i = 0; i < 50; i++) outcomes.push(await spool.writeRequest(request(T + i), limits));
  assert.equal(outcomes.filter((o) => o === "rate_limited").length, 49);
  spool.close();
  const counters = spoolLines(dir).filter((l) => l.k === "counter");
  assert.equal(counters.reduce((a, c) => a + c.n, 0), 49);
  assert.ok(counters.length <= 2);
});

test("spool: budgets stay cheap with many clients", () => {
  const spool = new SpoolStore(tmp());
  const started = performance.now();
  for (let i = 0; i < 60000; i++) {
    spool.overBudget(makeLimits({ budgets: [budget(`r:c${i}`, 1, 5, "rate_limited")] }), T, "events");
  }
  assert.ok(performance.now() - started < 5000);
  assert.equal(spool.rate.size, 1);
});

test("error log: 5 errors a minute in full, then a count when the next minute's first error comes", () => {
  const lines = [];
  let now = T;
  const errors = new ErrorLog({ error: (msg, exc) => lines.push([msg, exc ? "stack" : ""]) }, 5, () => now);
  for (let i = 0; i < 12; i++) errors.exception(`failure ${i}`, new Error("x"));
  assert.deepEqual(lines.map(([m]) => m), ["failure 0", "failure 1", "failure 2", "failure 3", "failure 4"]);
  assert.ok(lines.every(([, s]) => s === "stack"));
  now += 60000;
  errors.exception("later", new Error("y"));
  assert.deepEqual(lines.slice(5).map(([m]) => m), ["7 more errors in the previous minute (not logged)", "later"]);
  now += 60000;
  errors.exception("quiet minute before");
  assert.equal(lines.length, 8); // no summary: the previous minute had a single error
});
