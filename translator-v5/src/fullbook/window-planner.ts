import { createHash } from "node:crypto";

import type { V4Block } from "../domain/types.js";
import type {
  AdaptiveConcurrencyOptions,
  BookWindowPlan,
  WindowExecutionSummary,
} from "./types.js";

export interface WindowPlanOptions {
  maxSourceTokens?: number;
  maxBlocks?: number;
  protocolVersion?: string;
}

const DEFAULT_MAX_SOURCE_TOKENS = 2_600;
const DEFAULT_MAX_BLOCKS = 3;
const DEFAULT_PROTOCOL_VERSION = "v5-book-3";

function positiveInteger(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError(`${name} must be a positive safe integer`);
  }
  return value;
}

function stableWindowId(
  blocks: readonly V4Block[],
  protocolVersion: string,
): string {
  const hash = createHash("sha256");
  hash.update(protocolVersion);
  for (const block of blocks) {
    hash.update("\0");
    hash.update(block.id);
    hash.update("\0");
    hash.update(block.sourceHash);
  }
  return `window-${hash.digest("hex").slice(0, 20)}`;
}

export function planBookWindows(
  sourceBlocks: readonly V4Block[],
  options: WindowPlanOptions = {},
): BookWindowPlan[] {
  const maxSourceTokens = positiveInteger(
    options.maxSourceTokens ?? DEFAULT_MAX_SOURCE_TOKENS,
    "maxSourceTokens",
  );
  const maxBlocks = positiveInteger(options.maxBlocks ?? DEFAULT_MAX_BLOCKS, "maxBlocks");
  const protocolVersion = options.protocolVersion ?? DEFAULT_PROTOCOL_VERSION;
  if (protocolVersion.trim().length === 0) {
    throw new TypeError("protocolVersion must not be empty");
  }
  const blocks = [...sourceBlocks].sort((left, right) =>
    left.globalIndex - right.globalIndex
    || left.blockIndex - right.blockIndex
    || left.id.localeCompare(right.id));
  const seen = new Set<number>();
  for (let index = 0; index < blocks.length; index += 1) {
    const block = blocks[index] as V4Block;
    if (seen.has(block.globalIndex)) {
      throw new Error(`duplicate global index: ${block.globalIndex}`);
    }
    seen.add(block.globalIndex);
    if (index > 0
      && block.globalIndex !== (blocks[index - 1] as V4Block).globalIndex + 1) {
      throw new Error("book blocks must use continuous global indexes");
    }
  }

  const grouped: V4Block[][] = [];
  let current: V4Block[] = [];
  let currentTokens = 0;
  for (const block of blocks) {
    const chapterId = block.chapterId ?? `chapter-at-${block.globalIndex}`;
    const currentChapterId = current.length === 0
      ? chapterId
      : current[0]?.chapterId ?? `chapter-at-${current[0]?.globalIndex ?? 0}`;
    const wouldOverflow = current.length > 0 && (
      chapterId !== currentChapterId
      || current.length >= maxBlocks
      || currentTokens + block.tokenCount > maxSourceTokens
    );
    if (wouldOverflow) {
      grouped.push(current);
      current = [];
      currentTokens = 0;
    }
    current.push({ ...block });
    currentTokens += block.tokenCount;
  }
  if (current.length > 0) {
    grouped.push(current);
  }

  return grouped.map((windowBlocks, ordinal) => {
    const first = windowBlocks[0] as V4Block;
    const sourceTokens = windowBlocks.reduce(
      (total, block) => total + block.tokenCount,
      0,
    );
    return {
      windowId: stableWindowId(windowBlocks, protocolVersion),
      ordinal,
      chapterId: first.chapterId ?? `chapter-at-${first.globalIndex}`,
      chapterTitle: first.chapterTitle,
      blockIds: windowBlocks.map((block) => block.id),
      globalIndexes: windowBlocks.map((block) => block.globalIndex),
      sourceTokens,
      sourceChars: windowBlocks.reduce(
        (total, block) => total + block.sourceText.length,
        0,
      ),
      oversized: windowBlocks.length === 1 && sourceTokens > maxSourceTokens,
    };
  });
}

export function nextConcurrency(
  history: readonly WindowExecutionSummary[],
  options: AdaptiveConcurrencyOptions,
): number {
  const warmupWindows = Math.max(0, Math.trunc(options.warmupWindows));
  const maxConcurrency = positiveInteger(options.maxConcurrency, "maxConcurrency");
  const riskRatio = options.budgetRiskRatio ?? 0.8;
  if (!Number.isFinite(riskRatio) || riskRatio <= 0 || riskRatio > 1) {
    throw new TypeError("budgetRiskRatio must be in (0, 1]");
  }
  if (history.length < warmupWindows) {
    return 1;
  }
  const latest = history.at(-1);
  if (latest === undefined) {
    return 1;
  }
  const budgetRatio = latest.modelCallLimit === 0
    ? 1
    : latest.modelCalls / latest.modelCallLimit;
  if (latest.status !== "completed"
    || latest.repaired
    || latest.deadlineExceeded
    || budgetRatio > riskRatio) {
    return 1;
  }
  return maxConcurrency;
}
