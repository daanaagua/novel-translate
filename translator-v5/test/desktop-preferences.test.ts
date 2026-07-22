import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { DesktopPreferences } from "../src/desktop/desktop-preferences.js";

test("recent project preferences keep only supported absolute fields", () => {
  const directory = mkdtempSync(join(tmpdir(), "folioloom-desktop-preferences-"));
  const path = join(directory, "preferences.json");
  const manifestPath = join(directory, "source_manifest.json");
  const storePath = join(directory, "book.db");
  try {
    const preferences = new DesktopPreferences(path);
    assert.equal(preferences.load(), undefined);

    preferences.save({
      manifestPath,
      storePath,
      runId: "run-desktop",
      glossaryPath: join(directory, "glossary.json"),
    });

    assert.deepEqual(preferences.load(), { manifestPath, storePath, runId: "run-desktop" });
    const payload = JSON.parse(readFileSync(path, "utf8")) as { recent: Record<string, unknown> };
    assert.deepEqual(Object.keys(payload.recent).sort(), ["manifestPath", "runId", "storePath"]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("recent project preferences ignore corrupt or unsupported files", () => {
  const directory = mkdtempSync(join(tmpdir(), "folioloom-desktop-preferences-"));
  const path = join(directory, "preferences.json");
  try {
    const preferences = new DesktopPreferences(path);
    writeFileSync(path, "not JSON", "utf8");
    assert.equal(preferences.load(), undefined);

    writeFileSync(path, JSON.stringify({ schema: "other", recent: { manifestPath: "C:\\book.json" } }), "utf8");
    assert.equal(preferences.load(), undefined);

    writeFileSync(path, JSON.stringify({
      schema: "folioloom-desktop-preferences-1",
      recent: { manifestPath: "relative/source_manifest.json" },
    }), "utf8");
    assert.equal(preferences.load(), undefined);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
