/**
 * Unit tests for the hardening of the shared core and the Node connector (security audit of the Node and Worker
 * connectors): the allowance identity, CPython-exact helpers that must stay fast on hostile input, gzip decoding
 * as Python does it, migrations, and SQLite lock waits on the request path.
 *
 *   cd server/node && npm test
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import zlib from "node:zlib";

import { parseNetwork } from "../core/anonymize.mjs";
import { clientKeyFor } from "../core/collector.mjs";
import { gunzipBounded, setNativeZlib } from "../core/gzip.mjs";
import { fnmatchcase, pyIntOf } from "../core/pycompat.mjs";
import { NO_LIMITS, RequestRecord } from "../core/records.mjs";
import { EVENTS_MIGRATIONS } from "../core/schema.mjs";
import { migrate, openStore } from "../storage.mjs";

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "tidesight-node-test-"));

// A configuration with only what clientKeyFor reads.
const keyConfig = (key, proxies = ["127.0.0.1/32"]) => ({
  server: { rate_limit_key: key, rate_limit_ipv6_prefix: 64 },
  proxyNetworks: () => proxies.map(parseNetwork),
});

test("client key: only fields named exactly like rate_limit_key, the last one's last value", () => {
  const cfg = keyConfig("x-real-ip");
  assert.equal(clientKeyFor(cfg, "127.0.0.1", [["x-real-ip", "1.1.1.1"]]), "1.1.1.1");
  assert.equal(clientKeyFor(cfg, "127.0.0.1", [["x-real-ip", "9.9.9.9"], ["x-real-ip", "6.6.6.6, 1.1.1.1:443"]]),
    "1.1.1.1");
  // The configured name with "_" for "-" is another header to a proxy, which neither sets nor removes it, but WSGI
  // servers merge the two: its presence makes the header untrustworthy, and the socket peer is the client.
  assert.equal(clientKeyFor(cfg, "127.0.0.1", [["x-real-ip", "1.1.1.1"], ["x_real_ip", "7.7.7.7"]]), "127.0.0.1");
  assert.equal(clientKeyFor(cfg, "127.0.0.1", [["x_real_ip", "7.7.7.7"], ["x-real-ip", "1.1.1.1"]]), "127.0.0.1");
  assert.equal(clientKeyFor(cfg, "127.0.0.1", [["x_real_ip", "7.7.7.7"]]), "127.0.0.1");
  // Believed only from a listed proxy, and only when it is an address.
  assert.equal(clientKeyFor(cfg, "203.0.113.5", [["x-real-ip", "1.1.1.1"]]), "203.0.113.5");
  assert.equal(clientKeyFor(cfg, "127.0.0.1", [["x-real-ip", "nobody"]]), "127.0.0.1");
  assert.equal(clientKeyFor(keyConfig("remote_addr"), "2001:db8:1:2::9", [["x-real-ip", "1.1.1.1"]]),
    "2001:db8:1:2::/64");
});

test("fnmatch: CPython's results, without backtracking on long names", () => {
  const cases = [
    ["*.example.com", "a.example.com", true], ["https://*.multi.example", "https://a.b.multi.example", true],
    ["a*b*c", "abxbc", true], ["a*b*c", "abxb", false], ["*a*a*b", "aaaaab", true], ["*a*a*b", "aaaaa", false],
    ["[!a]*x", "bx", true], ["x*", "x\ny", true], ["*\n*", "a\nb", true],
  ];
  for (const [pattern, name, expected] of cases) assert.equal(fnmatchcase(name, pattern), expected, `${pattern} ${name}`);
  const started = performance.now();
  assert.equal(fnmatchcase("http" + ":".repeat(15000) + "//x", "http*://*.multi.example"), false);
  assert.equal(fnmatchcase("a".repeat(20000), "*a*a*a*a*a*b"), false);
  assert.ok(performance.now() - started < 500);
});

test("int(): Unicode digits as Python reads them, long strings refused before converting", () => {
  const cases = [["١٢", 12], ["１２", 12], [" 12 ", 12], ["1_000", null], ["+5", null], ["-0", 0],
    ["\u{116C3}", 3], ["\u{1E950}\u{1E951}", 1], ["1".repeat(4300), null], ["\u{1D7FF}".repeat(4301), null]];
  for (const [value, expected] of cases) assert.equal(pyIntOf(value), expected, JSON.stringify(value.slice(0, 8)));
  const started = performance.now();
  assert.equal(pyIntOf("\u{1D7FF}".repeat(262144)), null); // 1 MiB of a 4-byte digit
  assert.equal(pyIntOf("٩".repeat(524288)), null);
  assert.ok(performance.now() - started < 200);
});

test("gzip: the first member only, its trailer checked, zlib's error texts (native zlib, as Python)", async () => {
  setNativeZlib({ // as server.mjs registers it
    inflateRaw(data, limit, window) {
      const r = zlib.inflateRawSync(data, { info: true, maxOutputLength: Math.max(1, limit),
        chunkSize: Math.max(64, window) });
      return { out: new Uint8Array(r.buffer.buffer, r.buffer.byteOffset, r.buffer.byteLength), consumed: r.engine.bytesWritten };
    },
    crc32: (bytes) => zlib.crc32(bytes),
  });
  const gz = (s) => new Uint8Array(zlib.gzipSync(Buffer.from(s)));
  const cat = (a, b) => Uint8Array.from([...a, ...b]);
  const text = async (body, max = 1000) => {
    try {
      return Buffer.from(await gunzipBounded(body, max)).toString();
    } catch (exc) {
      return `ERR ${exc.message}`;
    }
  };
  const first = gz("first");
  assert.equal(await text(cat(first, gz("second"))), "first");
  assert.equal(await text(cat(first, Buffer.from("junkjunkjunk"))), "first");
  const flip = (at) => {
    const b = first.slice();
    b[b.length + at] ^= 1;
    return b;
  };
  assert.equal(await text(flip(-8)), "ERR Error -3 while decompressing data: incorrect data check");
  assert.equal(await text(flip(-1)), "ERR Error -3 while decompressing data: incorrect length check");
  assert.equal(await text(first.slice(0, -3)), "ERR truncated gzip data");
  assert.equal(await text(first.slice(0, 12)), "ERR truncated gzip data");
  assert.equal(await text(gz("x".repeat(1001))), "ERR decompressed body exceeds 1000 bytes");
  assert.equal((await text(gz("x".repeat(1000)))).length, 1000);
  const badBlock = gz("hello world, hello world");
  badBlock[10] = 0xff;
  assert.equal(await text(badBlock), "ERR Error -3 while decompressing data: invalid block type");
  // Past the limit, Python stops decoding: a truncation or a bad block further on reads "exceeds" too.
  const header = gz("").subarray(0, 10);
  const stored = new Uint8Array(zlib.deflateRawSync(Buffer.from("ab".repeat(1500)), { level: 0 }));
  assert.equal(await text(cat(header, stored.subarray(0, 2500))), "ERR decompressed body exceeds 1000 bytes");
  const flushed = new Uint8Array(zlib.deflateRawSync(Buffer.from("x".repeat(1200)),
    { level: 0, finishFlush: zlib.constants.Z_SYNC_FLUSH }));
  assert.equal(await text(cat(header, cat(flushed.subarray(0, 1210), [0xff, 0xff, 0xff]))),
    "ERR decompressed body exceeds 1000 bytes");
});

test("migrate: a full schema without user_version is brought up to date (ADD COLUMN skipped when present)", () => {
  const db = new DatabaseSync(path.join(tmp(), "events.db"));
  for (const [, statements] of EVENTS_MIGRATIONS) for (const sql of statements) db.exec(sql); // as a D1 export
  assert.equal(db.prepare("PRAGMA user_version").get().user_version, 0);
  assert.equal(migrate(db, EVENTS_MIGRATIONS), EVENTS_MIGRATIONS[EVENTS_MIGRATIONS.length - 1][0]);
  db.close();
});

test("storage: a locked database costs a request at most a short wait, then is left alone for a while", async () => {
  const dir = tmp();
  const store = openStore(dir, "sqlite", "wal", "normal", 10000, true);
  const rec = () => new RequestRecord({ received_at: Date.now(), endpoint: "collect", method: "POST", site_id: "x",
    site_status: "ok", headers: new Map() });
  assert.equal(await store.writeRequest(rec(), NO_LIMITS), "stored");
  const locker = new DatabaseSync(path.join(dir, "events.db"));
  locker.exec("BEGIN IMMEDIATE"); // another process (a cron job) holding the write lock
  try {
    let ticks = 0;
    const timer = setInterval(() => (ticks += 1), 5); // other work of the process: it must go on meanwhile
    let started = performance.now();
    assert.equal(await store.writeRequest(rec(), NO_LIMITS), "spooled");
    clearInterval(timer);
    assert.ok(performance.now() - started < 1500, "busy_timeout_ms is capped on the request path");
    assert.ok(ticks >= 5, `the event loop kept turning while the lock was awaited (${ticks} ticks)`);
    started = performance.now();
    assert.equal(await store.writeRequest(rec(), NO_LIMITS), "spooled");
    assert.ok(performance.now() - started < 100, "no second wait while backing off");
  } finally {
    locker.exec("ROLLBACK");
    locker.close();
    store.close();
  }
});
