import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { preloadEntryPath } from "../src/desktop/main/runtime.js";

const projectRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

test("desktop build emits the preload file consumed by the main process", () => {
  const preloadPath = preloadEntryPath(join(projectRoot, "out", "main"));
  assert.equal(preloadPath, join(projectRoot, "out", "preload", "index.mjs"));
  assert.equal(existsSync(preloadPath), true, `missing built preload at ${preloadPath}`);
  assert.equal(
    existsSync(join(projectRoot, "out", "renderer", "index.html")),
    true,
    "missing built renderer entry",
  );
});
