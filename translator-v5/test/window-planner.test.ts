import assert from "node:assert/strict";
import test from "node:test";

import type { V4Block } from "../src/domain/types.js";
import {
  nextConcurrency,
  planBookWindows,
} from "../src/fullbook/window-planner.js";

function block(
  globalIndex: number,
  chapterId: string,
  tokenCount: number,
): V4Block {
  return {
    id: `block-${globalIndex}`,
    legacyId: `legacy-${globalIndex}`,
    chapterId,
    chapterTitle: chapterId.toUpperCase(),
    globalIndex,
    blockIndex: globalIndex,
    sourceText: `Source ${globalIndex}`,
    sourceHash: `hash-${globalIndex}`,
    tokenCount,
  };
}

test("window plan is stable, chapter-bounded, and size-bounded", () => {
  const blocks = [
    block(0, "ch1", 4),
    block(1, "ch1", 4),
    block(2, "ch1", 4),
    block(3, "ch2", 20),
    block(4, "ch2", 3),
  ];
  const options = { maxSourceTokens: 10, maxBlocks: 2, protocolVersion: "v5-book-1" };
  const first = planBookWindows(blocks, options);
  const second = planBookWindows([...blocks].reverse(), options);

  assert.deepEqual(
    first.map((window) => window.blockIds),
    [["block-0", "block-1"], ["block-2"], ["block-3"], ["block-4"]],
  );
  assert.deepEqual(
    first.map((window) => window.chapterId),
    ["ch1", "ch1", "ch2", "ch2"],
  );
  assert.equal(first[2]?.oversized, true);
  assert.deepEqual(first.map((window) => window.windowId), second.map((window) => window.windowId));
});

test("window plan rejects duplicate or discontinuous global indexes", () => {
  assert.throws(
    () => planBookWindows([block(0, "ch1", 1), block(0, "ch1", 1)]),
    /duplicate global index/u,
  );
  assert.throws(
    () => planBookWindows([block(0, "ch1", 1), block(2, "ch1", 1)]),
    /continuous global indexes/u,
  );
});

test("production defaults keep model generations bounded", () => {
  const windows = planBookWindows([
    block(0, "ch1", 1_000),
    block(1, "ch1", 1_000),
    block(2, "ch1", 1_000),
    block(3, "ch1", 1_000),
  ]);

  assert.deepEqual(
    windows.map((window) => window.blockIds),
    [["block-0", "block-1"], ["block-2", "block-3"]],
  );
  assert.ok(windows.every((window) => window.blockIds.length <= 3));
});

test("adaptive concurrency warms up, accelerates, and backs off on risk", () => {
  const clean = {
    status: "completed" as const,
    modelCalls: 12,
    modelCallLimit: 20,
    repaired: false,
    deadlineExceeded: false,
  };
  assert.equal(nextConcurrency([], { warmupWindows: 2, maxConcurrency: 2 }), 1);
  assert.equal(nextConcurrency([clean], { warmupWindows: 2, maxConcurrency: 2 }), 1);
  assert.equal(nextConcurrency([clean, clean], { warmupWindows: 2, maxConcurrency: 2 }), 2);
  assert.equal(nextConcurrency([
    clean,
    clean,
    { ...clean, repaired: true },
  ], { warmupWindows: 2, maxConcurrency: 2 }), 1);
  assert.equal(nextConcurrency([
    clean,
    clean,
    { ...clean, modelCalls: 17 },
  ], { warmupWindows: 2, maxConcurrency: 2 }), 1);
});
