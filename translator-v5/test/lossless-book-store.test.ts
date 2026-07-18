import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";

import type { BookWindowPlan } from "../src/fullbook/types.js";
import type { LosslessBlock } from "../src/source/types.js";
import { BookStore } from "../src/storage/book-store.js";
import {
  LosslessBookStore,
  type CertifiedSourceInput,
  type TranslationRunMeta,
  type WindowStageInput,
} from "../src/storage/lossless-book-store.js";

function fixturePath(): string {
  return join(mkdtempSync(join(tmpdir(), "v5-lossless-store-")), "book-v2.db");
}

function sourceInput(sourceVersion = "source-v1"): CertifiedSourceInput {
  return {
    sourceVersion,
    rawSha256: "raw-sha-1",
    canonicalSha256: "canonical-sha-1",
    canonicalChars: 11,
    coordinateUnit: "unicode_scalar",
    sourceFormat: "txt",
    encoding: "utf-8",
    extractor: "plain-text-v1",
    ranges: [{
      rangeId: "range-0",
      canonicalStart: 0,
      canonicalEnd: 11,
      originKind: "text",
      originRef: "original.txt",
      transformation: "newline-normalization",
    }],
  };
}

function blocks(sourceVersion = "source-v1"): LosslessBlock[] {
  return [{
    id: "block-a",
    sourceVersion,
    canonicalStart: 0,
    canonicalEnd: 6,
    sourceText: "Alpha.",
    sourceHash: "hash-a",
    globalIndex: 0,
    tokenCount: 2,
    structureId: null,
    structureTitle: null,
  }, {
    id: "block-b",
    sourceVersion,
    canonicalStart: 6,
    canonicalEnd: 11,
    sourceText: "Beta.",
    sourceHash: "hash-b",
    globalIndex: 1,
    tokenCount: 2,
    structureId: null,
    structureTitle: null,
  }];
}

function windowsTogether(): BookWindowPlan[] {
  return [{
    windowId: "window-0",
    ordinal: 0,
    chapterId: "chapter-0",
    chapterTitle: "One",
    blockIds: ["block-a", "block-b"],
    globalIndexes: [0, 1],
    sourceTokens: 4,
    sourceChars: 11,
    oversized: false,
  }];
}

function windowsApart(): BookWindowPlan[] {
  return [{
    ...windowsTogether()[0]!,
    blockIds: ["block-a"],
    globalIndexes: [0],
    sourceTokens: 2,
    sourceChars: 6,
  }, {
    windowId: "window-1",
    ordinal: 1,
    chapterId: "chapter-0",
    chapterTitle: "One",
    blockIds: ["block-b"],
    globalIndexes: [1],
    sourceTokens: 2,
    sourceChars: 5,
    oversized: false,
  }];
}

function runMeta(modelId: string, suffix: string): TranslationRunMeta {
  return {
    runId: `run-${suffix}`,
    sourceVersion: "source-v1",
    protocolVersion: "lossless-v5-1",
    modelId,
    initialSnapshotId: `snapshot-${suffix}`,
    metadata: { fixture: suffix },
  };
}

function initialize(
  store: LosslessBookStore,
  meta = runMeta("model-a", "a"),
  windows = windowsTogether(),
): string {
  store.registerSource(sourceInput());
  store.replaceDerivedPlan("source-v1", { blocks: blocks(), annotations: [] });
  const runId = store.createTranslationRun(meta);
  store.initializeWindowPlan(runId, windows);
  return runId;
}

function validStage(
  runId = "run-a",
  windowId = "window-0",
  snapshotId = "snapshot-a",
): WindowStageInput {
  return {
    runId,
    windowId,
    snapshotId,
    status: "completed",
    translations: [{ blockId: "block-a", sourceHash: "hash-a", text: "阿尔法。" }, {
      blockId: "block-b",
      sourceHash: "hash-b",
      text: "贝塔。",
    }],
    knowledgeCandidates: [{
      recordId: "knowledge-0",
      normalizedSubject: "alpha",
      kind: "term",
      payload: { target: "阿尔法" },
    }],
    styleTail: "阿尔法。贝塔。",
    budget: { modelCalls: 1 },
    warnings: [],
  };
}

test("schema v2 enables foreign keys and WAL and creates every audit table", () => {
  const path = fixturePath();
  const store = new LosslessBookStore(path);
  assert.deepEqual(store.databaseSettings(), { foreignKeys: true, journalMode: "wal" });
  store.close();
  const database = new DatabaseSync(path);
  assert.equal((database.prepare("PRAGMA journal_mode").get() as { journal_mode: string }).journal_mode, "wal");
  const names = (database.prepare(`
    SELECT name FROM sqlite_master WHERE type='table' ORDER BY name
  `).all() as unknown as Array<{ name: string }>).map((row) => row.name);
  for (const name of [
    "source_versions", "source_ranges", "structure_annotations", "logical_blocks",
    "translation_runs", "window_plans", "window_membership", "translations",
    "knowledge_records", "knowledge_snapshots", "migration_candidates",
    "recovery_runs", "events",
  ]) {
    assert.ok(names.includes(name), `missing table ${name}`);
  }
  database.close();
});

test("schema v2 refuses to migrate a legacy BookStore database in place", () => {
  const path = fixturePath();
  const legacy = new BookStore(path);
  legacy.close();
  assert.throws(() => new LosslessBookStore(path), /legacy.*new database/i);
  const database = new DatabaseSync(path);
  const v2Table = database.prepare(`
    SELECT name FROM sqlite_master WHERE type='table' AND name='source_versions'
  `).get();
  assert.equal(v2Table, undefined);
  database.close();
});

test("source and derived plan registration are idempotent but never overwrite mismatched data", () => {
  const store = new LosslessBookStore(fixturePath());
  assert.equal(store.registerSource(sourceInput()), "source-v1");
  assert.equal(store.registerSource(sourceInput()), "source-v1");
  assert.throws(() => store.registerSource({
    ...sourceInput(),
    canonicalSha256: "changed",
  }), /source version.*different source/i);

  store.replaceDerivedPlan("source-v1", { blocks: blocks(), annotations: [] });
  store.replaceDerivedPlan("source-v1", { blocks: blocks(), annotations: [] });
  assert.throws(() => store.replaceDerivedPlan("source-v1", {
    blocks: [{ ...blocks()[0]!, sourceHash: "changed" }, blocks()[1]!],
    annotations: [],
  }), /derived plan.*different/i);
  assert.throws(() => store.replaceDerivedPlan("source-v1", {
    blocks: blocks("source-v2"), annotations: [],
  }), /block.*source version/i);
  store.close();
});

test("window initialization requires complete unique continuous membership and rolls back failures", () => {
  const store = new LosslessBookStore(fixturePath());
  store.registerSource(sourceInput());
  store.replaceDerivedPlan("source-v1", { blocks: blocks(), annotations: [] });

  const invalidPlans: Array<[string, BookWindowPlan[], RegExp]> = [
    ["gap", [{ ...windowsApart()[0]!, ordinal: 1 }, windowsApart()[1]!], /ordinal.*continuous/i],
    ["missing", [windowsApart()[0]!], /complete.*membership/i],
    ["duplicate", [{ ...windowsTogether()[0]!, blockIds: ["block-a", "block-a"], globalIndexes: [0, 0] }], /duplicate.*block/i],
    ["unknown", [{ ...windowsTogether()[0]!, blockIds: ["block-a", "block-x"], globalIndexes: [0, 2] }], /unknown block/i],
    ["reversed", [{ ...windowsApart()[0]!, blockIds: ["block-b"], globalIndexes: [1], sourceChars: 5 }, {
      ...windowsApart()[1]!, blockIds: ["block-a"], globalIndexes: [0], sourceChars: 6,
    }], /source order/i],
  ];
  for (const [suffix, invalid, pattern] of invalidPlans) {
    const runId = store.createTranslationRun(runMeta("model-a", suffix));
    assert.throws(() => store.initializeWindowPlan(runId, invalid), pattern);
    assert.equal(store.auditRows(runId).windows.length, 0);
  }

  const runId = store.createTranslationRun(runMeta("model-a", "ok"));
  store.initializeWindowPlan(runId, windowsApart());
  assert.deepEqual(store.auditRows(runId).windows.map((item) => item.ordinal), [0, 1]);
  store.close();
});

test("database constraints reject cross-window membership and mismatched source hashes", () => {
  const path = fixturePath();
  const store = new LosslessBookStore(path);
  initialize(store, runMeta("model-a", "db"), windowsApart());
  store.registerSource(sourceInput("source-v2"));
  store.replaceDerivedPlan("source-v2", { blocks: blocks("source-v2"), annotations: [] });
  store.close();

  const database = new DatabaseSync(path);
  database.exec("PRAGMA foreign_keys=ON");
  assert.throws(() => database.prepare(`
    INSERT INTO window_membership(run_id, window_id, source_version, block_id, position)
    VALUES('run-db', 'window-1', 'source-v1', 'block-a', 1)
  `).run(), /unique constraint/i);
  assert.throws(() => database.prepare(`
    INSERT INTO translations(
      run_id, window_id, source_version, block_id, version, source_hash,
      text, result_status, stage_state, active, snapshot_id
    ) VALUES('run-db', 'window-0', 'source-v1', 'block-a', 1, 'wrong',
      '错误', 'completed', 'staged', 0, 'snapshot-db')
  `).run(), /foreign key constraint/i);
  assert.throws(() => database.prepare(`
    INSERT INTO translations(
      run_id, window_id, source_version, block_id, version, source_hash,
      text, result_status, stage_state, active, snapshot_id
    ) VALUES('run-db', 'window-0', 'source-v2', 'block-a', 1, 'hash-a',
      '错误', 'completed', 'staged', 0, 'snapshot-db')
  `).run(), /foreign key constraint/i);
  database.close();
});

test("claimWindow atomically changes only one pending window in its run", () => {
  const store = new LosslessBookStore(fixturePath());
  initialize(store, runMeta("model-a", "a"), windowsApart());
  initialize(store, runMeta("model-b", "b"), windowsApart());
  assert.equal(store.claimWindow("run-a", "window-0").attemptCount, 1);
  assert.throws(() => store.claimWindow("run-a", "window-0"), /not pending/i);
  assert.equal(store.claimWindow("run-b", "window-0").attemptCount, 1);
  assert.equal(store.auditRows("run-a").windows[0]?.status, "running");
  assert.equal(store.auditRows("run-b").windows[0]?.status, "running");
  store.close();
});

test("stage writes only inactive rows and promote commits the complete window atomically", () => {
  const store = new LosslessBookStore(fixturePath());
  initialize(store);
  store.claimWindow("run-a", "window-0");
  store.stageWindow(validStage());
  assert.equal(store.activeTranslations("run-a").length, 0);
  assert.equal(store.knowledgeHistory("run-a")[0]?.active, false);
  assert.equal(store.auditRows("run-a").windows[0]?.status, "staged");

  store.promoteStagedWindow("run-a", "window-0");
  assert.deepEqual(store.activeTranslations("run-a").map((item) => item.blockId), ["block-a", "block-b"]);
  assert.equal(store.knowledgeHistory("run-a")[0]?.active, true);
  assert.equal(store.auditRows("run-a").windows[0]?.status, "completed");
  store.close();
});

test("failed staging of a bad second block leaves no translation or knowledge row", () => {
  const store = new LosslessBookStore(fixturePath());
  initialize(store);
  store.claimWindow("run-a", "window-0");
  const invalid = validStage();
  invalid.translations[1] = { ...invalid.translations[1]!, sourceHash: "wrong" };
  assert.throws(() => store.stageWindow(invalid), /source hash/i);
  assert.equal(store.activeTranslations("run-a").length, 0);
  assert.equal(store.knowledgeHistory("run-a").length, 0);
  assert.equal(store.auditRows("run-a").windows[0]?.status, "running");
  store.close();
});

test("promotion revalidates every staged row and rolls back after database tampering", () => {
  const path = fixturePath();
  const store = new LosslessBookStore(path);
  initialize(store);
  store.claimWindow("run-a", "window-0");
  store.stageWindow(validStage());
  store.close();

  const database = new DatabaseSync(path);
  database.exec("PRAGMA foreign_keys=OFF");
  database.prepare(`UPDATE translations SET text='' WHERE run_id='run-a' AND block_id='block-b'`).run();
  database.close();

  const reopened = new LosslessBookStore(path);
  assert.throws(() => reopened.promoteStagedWindow("run-a", "window-0"), /empty.*translation/i);
  assert.equal(reopened.activeTranslations("run-a").length, 0);
  assert.equal(reopened.knowledgeHistory("run-a")[0]?.active, false);
  assert.equal(reopened.auditRows("run-a").windows[0]?.status, "staged");
  reopened.close();
});

test("store rejects cross-run windows and snapshots while allowing isolated active translations", () => {
  const store = new LosslessBookStore(fixturePath());
  initialize(store, runMeta("model-a", "a"));
  initialize(store, runMeta("model-b", "b"));

  store.claimWindow("run-a", "window-0");
  assert.throws(() => store.stageWindow(validStage("run-b", "window-0", "snapshot-a")), /snapshot.*run/i);
  assert.throws(() => store.stageWindow(validStage("run-a", "missing", "snapshot-a")), /run.*window/i);
  store.stageWindow(validStage("run-a", "window-0", "snapshot-a"));
  store.promoteStagedWindow("run-a", "window-0");

  store.claimWindow("run-b", "window-0");
  store.stageWindow({
    ...validStage("run-b", "window-0", "snapshot-b"),
    knowledgeCandidates: [],
  });
  store.promoteStagedWindow("run-b", "window-0");
  assert.equal(store.activeTranslations("run-a").length, 2);
  assert.equal(store.activeTranslations("run-b").length, 2);
  assert.equal(store.auditRows("run-a").modelId, "model-a");
  assert.equal(store.auditRows("run-b").modelId, "model-b");
  store.close();
});

test("failWindow is run-scoped and records stable retry and terminal state", () => {
  const store = new LosslessBookStore(fixturePath());
  initialize(store, runMeta("model-a", "a"), windowsApart());
  store.claimWindow("run-a", "window-0");
  assert.throws(() => store.failWindow("run-b", "window-0", {
    error: "wrong run", retry: true, budget: {}, warnings: [],
  }), /run.*window/i);
  store.failWindow("run-a", "window-0", {
    error: "temporary\nnetwork failure",
    retry: true,
    budget: { modelCalls: 1 },
    warnings: ["retry"],
  });
  assert.equal(store.auditRows("run-a").windows[0]?.status, "pending");
  assert.equal(store.auditRows("run-a").windows[0]?.lastError, "temporary\nnetwork failure");
  store.claimWindow("run-a", "window-0");
  store.failWindow("run-a", "window-0", {
    error: "terminal", retry: false, budget: {}, warnings: [],
  });
  assert.equal(store.auditRows("run-a").windows[0]?.status, "human_required");
  store.close();
});

test("public inputs reject unsafe integers, empty identifiers and non-serializable JSON", () => {
  const store = new LosslessBookStore(fixturePath());
  assert.throws(() => store.registerSource({ ...sourceInput(), canonicalChars: Number.MAX_SAFE_INTEGER + 1 }), /safe integer/i);
  assert.throws(() => store.registerSource({ ...sourceInput(), sourceVersion: " " }), /sourceVersion.*nonempty/i);
  store.registerSource(sourceInput());
  store.replaceDerivedPlan("source-v1", { blocks: blocks(), annotations: [] });
  const cyclic: Record<string, unknown> = {};
  cyclic.self = cyclic;
  assert.throws(() => store.createTranslationRun({
    ...runMeta("model-a", "json"), metadata: cyclic,
  }), /JSON-serializable/i);
  store.close();
});
