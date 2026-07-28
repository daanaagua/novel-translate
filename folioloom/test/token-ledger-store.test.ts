import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import type { BookWindowPlan } from "../src/fullbook/types.js";
import { blockId } from "../src/source/block-builder.js";
import type { LosslessBlock } from "../src/source/types.js";
import {
  LosslessBookStore,
  type CertifiedSourceInput,
  type TranslationRunMeta,
} from "../src/storage/lossless-book-store.js";

const CANONICAL_SOURCE = "Alpha.Beta.";

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function fixturePath(): string {
  return join(mkdtempSync(join(tmpdir(), "folioloom-token-ledger-store-")), "book.db");
}

function sourceInput(): CertifiedSourceInput {
  return {
    sourceVersion: "source-v1",
    rawSha256: sha256(CANONICAL_SOURCE),
    canonicalSha256: sha256(CANONICAL_SOURCE),
    canonicalChars: 11,
    coordinateUnit: "unicode_scalar",
    sourceFormat: "txt",
    encoding: "utf-8",
    extractor: "plain-text-v1",
    sourceLanguage: "en",
    sourceLanguageProfileVersion: "source-language-profile-1",
    sourceLanguageCompatibilityMode: false,
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

function blocks(): LosslessBlock[] {
  const firstText = "Alpha.";
  const secondText = "Beta.";
  return [{
    id: blockId("source-v1", 0, 6, firstText),
    sourceVersion: "source-v1",
    canonicalStart: 0,
    canonicalEnd: 6,
    sourceText: firstText,
    sourceHash: sha256(firstText),
    globalIndex: 0,
    tokenCount: 2,
    structureId: null,
    structureTitle: null,
  }, {
    id: blockId("source-v1", 6, 11, secondText),
    sourceVersion: "source-v1",
    canonicalStart: 6,
    canonicalEnd: 11,
    sourceText: secondText,
    sourceHash: sha256(secondText),
    globalIndex: 1,
    tokenCount: 2,
    structureId: null,
    structureTitle: null,
  }];
}

function windows(): BookWindowPlan[] {
  const [first, second] = blocks();
  return [{
    windowId: "window-0",
    ordinal: 0,
    chapterId: "chapter-0",
    chapterTitle: "One",
    blockIds: [first!.id, second!.id],
    globalIndexes: [0, 1],
    sourceTokens: 4,
    sourceChars: 11,
    oversized: false,
  }];
}

function runMeta(): TranslationRunMeta {
  return {
    runId: "run-ledger",
    sourceVersion: "source-v1",
    protocolVersion: "lossless-v5-1",
    modelId: "model-a",
    initialSnapshotId: "snapshot-a",
    metadata: { fixture: "ledger" },
  };
}

function openRunStore(): { store: LosslessBookStore; runId: string; path: string } {
  const path = fixturePath();
  const store = new LosslessBookStore(path);
  store.registerSource(sourceInput());
  store.replaceDerivedPlan("source-v1", { blocks: blocks(), annotations: [] });
  const meta = runMeta();
  const runId = store.createTranslationRun(meta);
  store.initializeWindowPlan(runId, windows());
  return { store, runId, path };
}

const LEDGER_INIT = {
  mode: "active" as const,
  profile: "balanced" as const,
  tokenIncreaseCap: 0.1,
};

test("loadSchedulerMetrics is undefined without ledger events", () => {
  const { store, runId } = openRunStore();
  try {
    assert.equal(store.loadSchedulerMetrics(runId), undefined);
  } finally {
    store.close();
  }
});

test("append and load token ledger events round-trip through replay", () => {
  const { store, runId, path } = openRunStore();
  try {
    store.appendTokenLedgerEvent(runId, {
      type: "baseline_added",
      taskIds: ["task-a"],
      baselineTokens: 1000,
      source: "translate_horizon",
      reason: "wave",
    });
    store.appendTokenLedgerEvent(runId, {
      type: "reserved",
      requestId: "req-1",
      purpose: "translate",
      taskIds: ["task-a"],
      predictedTokens: 200,
      attempt: 0,
    });
    store.appendTokenLedgerEvent(runId, {
      type: "settled",
      requestId: "req-1",
      actualTokens: 250,
      usageComplete: true,
      outcome: "success",
    });
    store.appendTokenLedgerEvent(runId, {
      type: "counters_patched",
      patch: {
        decisions: 1,
        planningStatus: "optimal",
        predictedTokens: 200,
        actualWallTimeMs: 500,
      },
    });

    const ledger = store.loadTokenLedger(runId, LEDGER_INIT);
    assert.equal(ledger.state().baselineTokens, 1000);
    assert.equal(ledger.state().spentTokens, 250);
    assert.equal(ledger.state().reservedTokens, 0);
    assert.equal(ledger.state().decisions, 1);

    store.close();
    const reopened = new LosslessBookStore(path);
    try {
      const again = reopened.loadTokenLedger(runId, LEDGER_INIT);
      assert.equal(again.state().spentTokens, 250);
      assert.equal(again.state().allowedTokens, 1100);
    } finally {
      reopened.close();
    }
  } finally {
    try {
      store.close();
    } catch {
      // already closed after reopen path
    }
  }
});

test("projection save and load returns full scheduler report", () => {
  const { store, runId } = openRunStore();
  try {
    store.appendTokenLedgerEvent(runId, {
      type: "baseline_added",
      taskIds: ["task-a"],
      baselineTokens: 500,
      source: "translate_horizon",
      reason: "wave",
    });
    const ledger = store.loadTokenLedger(runId, LEDGER_INIT);
    ledger.apply({
      type: "counters_patched",
      patch: {
        decisions: 3,
        fallbacks: 1,
        planningStatus: "bounded",
        predictedTokens: 400,
        predictedWallTimeMs: 1200,
        actualWallTimeMs: 1100,
        baselineWallTimeMs: 1500,
      },
    });
    const report = ledger.toSchedulerRunReport();
    store.saveSchedulerRunProjection(runId, report);

    const loaded = store.loadSchedulerMetrics(runId);
    assert.ok(loaded);
    assert.equal(loaded.baselineTokens, 500);
    assert.equal(loaded.allowedTokens, 550);
    assert.equal(loaded.decisions, 3);
    assert.equal(loaded.planningStatus, "bounded");
    assert.equal(loaded.mode, "active");
    assert.equal(loaded.profile, "balanced");
  } finally {
    store.close();
  }
});

test("loadSchedulerMetrics rebuilds from ledger when projection missing", () => {
  const { store, runId } = openRunStore();
  try {
    store.appendTokenLedgerEvent(runId, {
      type: "baseline_added",
      taskIds: ["task-b"],
      baselineTokens: 800,
      source: "revalidate",
      reason: "rv",
    });
    store.appendTokenLedgerEvent(runId, {
      type: "reserved",
      requestId: "req-b",
      purpose: "revalidate",
      taskIds: ["task-b"],
      predictedTokens: 100,
      attempt: 0,
    });
    store.appendTokenLedgerEvent(runId, {
      type: "settled",
      requestId: "req-b",
      actualTokens: 130,
      usageComplete: true,
      outcome: "success",
    });

    const loaded = store.loadSchedulerMetrics(runId, LEDGER_INIT);
    assert.ok(loaded);
    assert.equal(loaded.baselineTokens, 800);
    assert.equal(loaded.actualTokens, 130);
    assert.equal(loaded.allowedTokens, 880);
  } finally {
    store.close();
  }
});
