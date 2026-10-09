#!/usr/bin/env node
/**
 * Run the Tidesight Worker's fetch handler on Node, with D1 emulated on node:sqlite (no wrangler needed).
 * Used by the conformance suite and handy for local development.
 *
 *   node serve.mjs --port 8787 --config tidesight.json --data DIR [--host 127.0.0.1] [--base-path /prefix]
 *
 * DIR/events.db and DIR/replay.db stand in for the EVENTS and REPLAY databases. At start-up the files in
 * ../migrations/{events,replay}/ are applied the way `wrangler d1 migrations apply` would, and the number of the
 * last one applied is recorded as PRAGMA user_version, which is where Tidesight's Python tools read a SQLite
 * file's schema version. The socket peer is passed as CF-Connecting-IP, as Cloudflare does; the configuration
 * file's text becomes the TIDESIGHT_CONFIG variable.
 */

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { remoteAddress, sendError, sendResponse, toFetchRequest } from "../../node/http-adapter.mjs";
import worker from "../src/index.mjs";
import { D1Database } from "./d1-shim.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MIGRATIONS = path.join(HERE, "..", "migrations");

function parseArgs(argv) {
  const opts = { port: "8787", host: "127.0.0.1", config: "", data: "", basePath: "" };
  const names = { "--port": "port", "--host": "host", "--config": "config", "--data": "data", "--base-path": "basePath" };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (!(flag in names) || i + 1 >= argv.length) {
      process.stderr.write("usage: node serve.mjs --port P --config tidesight.json --data DIR [--host H] [--base-path /p]\n");
      process.exit(2);
    }
    opts[names[flag]] = argv[++i];
  }
  if (!opts.config || !opts.data) {
    process.stderr.write("serve.mjs: --config and --data are required\n");
    process.exit(2);
  }
  return opts;
}

/** Apply ../migrations/<name>/NNNN_*.sql in order, tracking the last one applied in PRAGMA user_version. */
function applyMigrations(db, dir) {
  const files = fs.readdirSync(dir).filter((f) => /^\d+_.*\.sql$/.test(f))
    .sort((a, b) => parseInt(a, 10) - parseInt(b, 10));
  const version = () => db.prepare("PRAGMA user_version").get().user_version;
  if (!files.length || version() >= parseInt(files[files.length - 1], 10)) return;
  db.exec("BEGIN IMMEDIATE");
  try {
    let current = version();
    for (const file of files) {
      const number = parseInt(file, 10);
      if (number <= current) continue;
      db.exec(fs.readFileSync(path.join(dir, file), "utf8"));
      db.exec(`PRAGMA user_version=${number}`);
      current = number;
    }
    db.exec("COMMIT");
  } catch (exc) {
    db.exec("ROLLBACK");
    throw exc;
  }
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const data = path.resolve(opts.data);
  fs.mkdirSync(data, { recursive: true });
  const events = new D1Database(path.join(data, "events.db"));
  const replay = new D1Database(path.join(data, "replay.db"));
  applyMigrations(events.db, path.join(MIGRATIONS, "events"));
  applyMigrations(replay.db, path.join(MIGRATIONS, "replay"));
  const env = {
    EVENTS: events,
    REPLAY: replay,
    TIDESIGHT_CONFIG: fs.readFileSync(opts.config, "utf8"),
    TIDESIGHT_BASE_PATH: opts.basePath,
  };
  const ctx = { waitUntil: (p) => Promise.resolve(p).catch((e) => console.error(e)), passThroughOnException() {} };

  const server = http.createServer(async (req, res) => {
    try {
      const peer = remoteAddress(req);
      const request = toFetchRequest(req, { extraHeaders: peer ? [["CF-Connecting-IP", peer]] : [] });
      const response = await worker.fetch(request, env, ctx);
      await sendResponse(req, res, response);
    } catch (exc) {
      console.error(exc);
      sendError(res);
    }
  });
  const shutdown = () => {
    server.close();
    events.close();
    replay.close();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
  server.listen(Number(opts.port), opts.host, () => {
    process.stdout.write(`Tidesight Worker (D1 shim) on http://${opts.host}:${server.address().port} (data: ${data})\n`);
  });
}

main();
