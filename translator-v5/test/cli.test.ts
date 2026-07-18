import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";

import { main, parseArgs, resolveRunSelection } from "../src/cli.js";
import { BookContext } from "../src/fullbook/book-context.js";
import { planBookWindows } from "../src/fullbook/window-planner.js";
import { createKnowledgeSnapshot } from "../src/knowledge/snapshot.js";
import { bookArtifactFileNames } from "../src/report.js";
import { LosslessBookStore } from "../src/storage/lossless-book-store.js";

function sourceManifest(source: string): string {
  const directory = mkdtempSync(join(tmpdir(), "v5-cli-doctor-"));
  const payload = Buffer.from(source, "utf8");
  const hash = createHash("sha256").update(payload).digest("hex");
  writeFileSync(join(directory, "original.txt"), payload);
  writeFileSync(join(directory, "source.txt"), payload);
  const manifest = join(directory, "source_manifest.json");
  writeFileSync(manifest, JSON.stringify({
    schema_version: "v5-source-ledger-1",
    coordinate_unit: "unicode_scalar",
    raw_path: "original.txt",
    raw_size: payload.length,
    raw_sha256: hash,
    source_format: ".txt",
    encoding: "utf-8",
    extractor: "plain-text-v1",
    canonical_path: "source.txt",
    canonical_chars: [...source].length,
    canonical_sha256: hash,
    canonical_segments: [{
      canonical_start: 0,
      canonical_end: [...source].length,
      origin_kind: "decoded_bytes",
      origin_ref: "original.txt",
      raw_start: 0,
      raw_end: payload.length,
      transformation: "decode+newline-normalize",
    }],
    excluded_raw_ranges: [],
  }), "utf8");
  return manifest;
}

function auditStore(manifestPath: string, runId = "run-audit"): string {
  const storePath = join(dirname(manifestPath), "book.db");
  const context = BookContext.openLossless({ manifestPath });
  const store = new LosslessBookStore(storePath);
  try {
    store.registerSource(context.certifiedSource!);
    store.replaceDerivedPlan(context.sourceLedger.sourceVersion, {
      annotations: context.annotations,
      blocks: context.losslessBlocks,
    });
    const initialSnapshot = createKnowledgeSnapshot(runId, []);
    store.createTranslationRun({
      runId,
      sourceVersion: context.sourceLedger.sourceVersion,
      protocolVersion: "lossless-v5-1",
      modelId: "test-model",
      initialSnapshotId: initialSnapshot.id,
      initialSnapshot,
      metadata: { fixture: true },
    });
    store.initializeWindowPlan(runId, planBookWindows(context.losslessBlocks, {
      protocolVersion: "lossless-v5-1",
    }));
  } finally {
    store.close();
    context.close();
  }
  return storePath;
}

function addRun(storePath: string, manifestPath: string, runId: string): void {
  const context = BookContext.openLossless({ manifestPath });
  const store = new LosslessBookStore(storePath);
  try {
    store.registerSource(context.certifiedSource!);
    store.replaceDerivedPlan(context.sourceLedger.sourceVersion, {
      annotations: context.annotations,
      blocks: context.losslessBlocks,
    });
    const initialSnapshot = createKnowledgeSnapshot(runId, []);
    store.createTranslationRun({
      runId,
      sourceVersion: context.sourceLedger.sourceVersion,
      protocolVersion: "lossless-v5-1",
      modelId: "test-model",
      initialSnapshotId: initialSnapshot.id,
      initialSnapshot,
    });
    store.initializeWindowPlan(runId, planBookWindows(context.losslessBlocks, {
      protocolVersion: "lossless-v5-1",
    }));
  } finally {
    store.close();
    context.close();
  }
}

test("CLI parses lossless doctor without model configuration", () => {
  assert.deepEqual(parseArgs([
    "book", "doctor", "--manifest", "source_manifest.json",
  ]), {
    command: "book-doctor",
    manifest: resolve("source_manifest.json"),
  });
});

test("CLI parses lossless audit without model configuration", () => {
  assert.deepEqual(parseArgs([
    "book", "audit", "--store", "book.db", "--run", "run-1",
  ]), {
    command: "book-audit",
    store: resolve("book.db"),
    runId: "run-1",
  });
});

test("CLI parses formal lossless run and optional run selection", () => {
  const run = parseArgs([
    "book", "run",
    "--manifest", "source_manifest.json",
    "--v4-db", "legacy.db",
    "--store", "state.db",
    "--config", "config.yaml",
    "--run", "run-1",
    "--max-windows", "3",
  ]);
  assert.equal(run.command, "book-run");
  assert.equal(run.manifest, resolve("source_manifest.json"));
  assert.equal(run.legacyV4Db, resolve("legacy.db"));
  assert.equal(run.store, resolve("state.db"));
  assert.equal(run.runId, "run-1");
  assert.equal(run.maxWindows, 3);
  assert.equal(parseArgs([
    "book", "status", "--store", "state.db", "--run", "run-1",
  ]).runId, "run-1");
  assert.equal(parseArgs([
    "book", "export", "--store", "state.db", "--output", "out", "--run", "run-1",
  ]).runId, "run-1");
});

test("CLI rejects duplicate, missing-value, and unknown flags", () => {
  assert.throws(
    () => parseArgs([
      "book", "doctor", "--manifest", "one.json", "--manifest", "two.json",
    ]),
    /duplicate flag: --manifest/,
  );
  assert.throws(
    () => parseArgs(["book", "doctor", "--manifest"]),
    /missing value for --manifest/,
  );
  assert.throws(
    () => parseArgs(["book", "doctor", "--manifest", "source.json", "--config", "x"]),
    /unknown flag for book doctor: --config/,
  );
});

test("legacy preview parsing remains compatible", () => {
  const preview = parseArgs([
    "preview", "--db", "source.db", "--config", "config.yaml",
    "--output", "out", "--global-index", "3-4", "--preflight-only",
  ]);
  assert.equal(preview.command, "preview");
  assert.deepEqual(preview.globalIndexes, [3, 4]);
  assert.equal(preview.preflightOnly, true);
});

test("run omission creates with zero, resumes one unfinished run, and rejects many", () => {
  const manifest = sourceManifest("Selection source.");
  const storePath = join(dirname(manifest), "selection.db");
  let store = new LosslessBookStore(storePath);
  assert.equal(resolveRunSelection(store, undefined, "run"), undefined);
  assert.throws(
    () => resolveRunSelection(store, undefined, "read"),
    /requires --run when the store contains 0 candidate runs/,
  );
  store.close();

  addRun(storePath, manifest, "run-one");
  store = new LosslessBookStore(storePath);
  assert.equal(resolveRunSelection(store, undefined, "run"), "run-one");
  assert.equal(resolveRunSelection(store, undefined, "read"), "run-one");
  store.close();

  addRun(storePath, manifest, "run-two");
  store = new LosslessBookStore(storePath);
  assert.throws(
    () => resolveRunSelection(store, undefined, "run"),
    /multiple unfinished runs.*--run/,
  );
  assert.throws(
    () => resolveRunSelection(store, undefined, "read"),
    /requires --run when the store contains 2 candidate runs/,
  );
  assert.equal(resolveRunSelection(store, "run-two", "read"), "run-two");
  store.close();
});

test("incomplete book artifacts use explicit partial names", () => {
  const partial = bookArtifactFileNames(false);
  assert.ok(Object.values(partial).every((name) => name.includes(".partial.")));
  const complete = bookArtifactFileNames(true);
  assert.ok(Object.values(complete).every((name) => !name.includes(".partial.")));
});

test("book doctor audits the lossless pipeline without constructing a provider", async () => {
  const output: string[] = [];
  let providerConstructions = 0;
  const originalLog = console.log;
  console.log = (...values: unknown[]) => output.push(values.join(" "));
  try {
    await main(["book", "doctor", "--manifest", sourceManifest("Alpha.\n\nBeta.")], {
      createModel: () => {
        providerConstructions += 1;
        throw new Error("provider must not be constructed");
      },
    });
  } finally {
    console.log = originalLog;
  }
  assert.equal(providerConstructions, 0);
  const report = JSON.parse(output.at(-1) ?? "") as Record<string, unknown>;
  assert.equal(report.schema, "v5-book-doctor-1");
  assert.equal(report.sourceChars, 13);
  assert.equal(report.coveredChars, 13);
  assert.deepEqual(report.incidentCodes, []);
  assert.equal(report.modelCallsAllowed, false);
  assert.ok(Number(report.blockCount) > 0);
  assert.ok(Number(report.windowCount) > 0);
});

test("book doctor manifest failures exit nonzero with structured stable errors", () => {
  const manifest = sourceManifest("Hash protected.");
  writeFileSync(join(dirname(manifest), "source.txt"), "Hash tampered!", "utf8");
  const result = spawnSync(process.execPath, [
    "--import", "tsx", "src/cli.ts", "book", "doctor", "--manifest", manifest,
  ], {
    cwd: resolve("."),
    encoding: "utf8",
  });
  assert.equal(result.status, 1);
  const payloadText = result.stderr.split(/\r?\n/u)
    .find((line) => line.startsWith('{"schema":"v5-book-cli-error-1"'));
  assert.ok(payloadText);
  const payload = JSON.parse(payloadText) as Record<string, unknown>;
  assert.equal(payload.code, "CANONICAL_HASH_MISMATCH");
});

test("book audit recomputes persisted integrity and missing blocks without a provider", async () => {
  const manifest = sourceManifest("Alpha.\n\nBeta.");
  const storePath = auditStore(manifest);
  const database = new DatabaseSync(storePath);
  database.prepare("UPDATE logical_blocks SET source_text=? WHERE global_index=0")
    .run("Tampered source");
  database.close();
  const output: string[] = [];
  let providerConstructions = 0;
  const originalLog = console.log;
  console.log = (...values: unknown[]) => output.push(values.join(" "));
  try {
    await main(["book", "audit", "--store", storePath, "--run", "run-audit"], {
      createModel: () => {
        providerConstructions += 1;
        throw new Error("provider must not be constructed");
      },
    });
  } finally {
    console.log = originalLog;
  }
  assert.equal(providerConstructions, 0);
  const report = JSON.parse(output.at(-1) ?? "") as Record<string, unknown>;
  assert.equal(report.schema, "v5-book-store-audit-1");
  assert.equal(report.runId, "run-audit");
  assert.equal(report.protocolVersion, "lossless-v5-1");
  assert.equal(report.modelId, "test-model");
  assert.equal(report.complete, false);
  assert.ok(Number(report.missingBlockCount) > 0);
  assert.ok((report.incidentCodes as string[]).includes("SOURCE_HASH_MISMATCH"));
});

test("lossless export writes partial names, missing IDs, and run metadata", async () => {
  const manifest = sourceManifest("Partial source.");
  const storePath = auditStore(manifest, "run-partial");
  const outputDirectory = join(dirname(manifest), "artifacts");
  const output: string[] = [];
  const originalLog = console.log;
  console.log = (...values: unknown[]) => output.push(values.join(" "));
  try {
    await main([
      "book", "export", "--store", storePath, "--run", "run-partial",
      "--output", outputDirectory, "--allow-incomplete",
    ]);
  } finally {
    console.log = originalLog;
  }
  const paths = JSON.parse(output.at(-1) ?? "") as Record<string, string>;
  assert.ok(Object.values(paths).every((path) => path.includes(".partial.")));
  const audit = JSON.parse(readFileSync(paths.audit!, "utf8")) as Record<string, unknown>;
  assert.equal(audit.complete, false);
  assert.ok(Number(audit.missingBlockCount) > 0);
  assert.ok(Array.isArray(audit.missingBlockIds));
  assert.equal((audit.runMetadata as Record<string, unknown>).fixture, true);
});

test("CLI parses full-book preflight, run, status, and export commands", () => {
  assert.equal(parseArgs([
    "book", "preflight", "--db", "source.db",
  ]).command, "book-preflight");
  const run = parseArgs([
    "book", "run",
    "--manifest", "source_manifest.json",
    "--store", "state.db",
    "--config", "config.yaml",
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
