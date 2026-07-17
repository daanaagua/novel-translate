import assert from "node:assert/strict";
import test from "node:test";

import { parseArgs } from "../src/cli.js";

test("CLI parses full-book preflight, run, status, and export commands", () => {
  assert.equal(parseArgs([
    "book", "preflight", "--db", "source.db",
  ]).command, "book-preflight");
  const run = parseArgs([
    "book", "run",
    "--db", "source.db",
    "--store", "state.db",
    "--config", "config.yaml",
    "--output", "output",
    "--max-windows", "3",
    "--max-concurrency", "2",
  ]);
  assert.equal(run.command, "book-run");
  assert.equal(run.maxWindows, 3);
  assert.equal(run.maxConcurrency, 2);
  assert.equal(parseArgs([
    "book", "status", "--store", "state.db",
  ]).command, "book-status");
  assert.equal(parseArgs([
    "book", "export", "--store", "state.db", "--output", "output",
    "--allow-incomplete",
  ]).command, "book-export");
});
