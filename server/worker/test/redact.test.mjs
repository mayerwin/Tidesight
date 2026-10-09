/**
 * URL redaction (the shared core's redact.mjs, used by the Worker and the Node connector) against the vectors
 * every implementation shares: server/python/tests/redact_vectors.json.
 *
 *   node --test server/worker/test/redact.test.mjs        (from the repository root)
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { globSpans, redactUrl } from "../../node/core/redact.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VECTORS = JSON.parse(fs.readFileSync(path.join(HERE, "..", "..", "python", "tests", "redact_vectors.json"), "utf8"));
// The main group, then groups with other patterns ("more"), each with its own redact_params and redact_paths.
const GROUPS = [VECTORS, ...(VECTORS.more || [])];

test("every shared vector", () => {
  let n = 0;
  for (const group of GROUPS) {
    for (const [input, expected] of group.cases) {
      assert.equal(redactUrl(input, group.redact_params, group.redact_paths), expected, `${group.about || ""}: ${input}`);
      n += 1;
    }
  }
  assert.ok(n > VECTORS.cases.length, "the extra groups ran");
});

test("no patterns leave a URL as it is", () => {
  assert.equal(redactUrl("https://u:p@ex.com/reset/x?token=1", [], []), "https://u:p@ex.com/reset/x?token=1");
});

test("glob spans: first occurrences, left to right, without overlap", () => {
  assert.deepEqual(globSpans("/a/*/b/*", "/a/1/b/2"), [[3, 4], [7, 8]]);
  assert.equal(globSpans("ab*ba", "aba"), null);
  assert.deepEqual(globSpans("X*Y*Z", "xAyBz"), [[1, 2], [3, 4]]);
});

test("linear time on a hostile URL", () => {
  const url = "https://ex.com/" + "a/".repeat(20000) + "?" + "x=1&".repeat(10000);
  const started = performance.now();
  redactUrl(url, ["*a*a*a*a*a*a*b"], ["/*a*a*a*a*a*a*c"]);
  assert.ok(performance.now() - started < 1000);
});
