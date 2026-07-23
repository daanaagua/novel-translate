import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";

import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
  type Context,
} from "@earendil-works/pi-ai";

import {
  BookRequestCapacityError,
  runBook,
  windowOptionsForRunMode,
} from "../src/fullbook/book-runner.js";
import { BookContext } from "../src/fullbook/book-context.js";
import { loadGlossary } from "../src/glossary/glossary-profile.js";
import { planBookWindows } from "../src/fullbook/window-planner.js";
import { buildLosslessBlocks } from "../src/source/block-builder.js";
import { SourceLedger } from "../src/source/source-ledger.js";
import { annotateStructure } from "../src/source/structure-annotator.js";
import { BookStore } from "../src/storage/book-store.js";
import { LosslessBookStore } from "../src/storage/lossless-book-store.js";
import { createKnowledgeSnapshot } from "../src/knowledge/snapshot.js";
import { scalarLength } from "../src/source/types.js";
import {
  WeightedTokenEstimator,
  type UsageObservation,
} from "../src/source/token-estimator.js";

class TrackingTokenEstimator extends WeightedTokenEstimator {
  readonly observations: UsageObservation[] = [];

  override observeUsage(sample: UsageObservation): void {
    this.observations.push(sample);
    super.observeUsage(sample);
  }
}

function losslessFixture(source: string, sourceLanguage = "en") {
  const directory = mkdtempSync(join(tmpdir(), "v5-lossless-runner-"));
  const rawPath = join(directory, "original.txt");
  const canonicalPath = join(directory, "source.txt");
  const manifestPath = join(directory, "source_manifest.json");
  const raw = Buffer.from(source, "utf8");
  const hash = createHash("sha256").update(raw).digest("hex");
  writeFileSync(rawPath, raw);
  writeFileSync(canonicalPath, raw);
  writeFileSync(manifestPath, JSON.stringify({
    schema_version: "v5-source-ledger-1",
    coordinate_unit: "unicode_scalar",
    raw_path: "original.txt",
    raw_size: raw.length,
    raw_sha256: hash,
    source_format: ".txt",
    encoding: "utf-8",
    extractor: "plain-text-v1",
    sourceLanguage,
    canonical_path: "source.txt",
    canonical_chars: scalarLength(source),
    canonical_sha256: hash,
    canonical_segments: [{
      canonical_start: 0,
      canonical_end: scalarLength(source),
      origin_kind: "decoded_bytes",
      origin_ref: "original.txt",
      raw_start: 0,
      raw_end: raw.length,
      transformation: "decode+newline-normalize",
    }],
    excluded_raw_ranges: [],
  }), "utf8");
  const ledger = SourceLedger.open(manifestPath);
  const blocks = buildLosslessBlocks(
    ledger,
    annotateStructure(ledger, ledger.sourceVersion),
    { sourceVersion: ledger.sourceVersion },
  );
  const windows = planBookWindows(blocks, {
    maxBlocks: 1,
    maxSourceTokens: 100,
    protocolVersion: "lossless-v5-1",
  });
  const faux = fauxProvider();
  return {
    canonicalPath,
    faux,
    submission: {
      windows: windows.map((window) => ({
        windowId: window.windowId,
        translations: window.blockIds.map((blockId) => ({
          blockId,
          text: "这是完整的中文译文。",
        })),
        notes: [],
      })),
    },
    options: {
      manifestPath,
      storePath: join(directory, "book-v2.db"),
      runMeta: { runId: "run-lossless", protocolVersion: "lossless-v5-1" },
      model: faux.getModel(),
      streamFn: faux.provider.streamSimple.bind(faux.provider),
      windowOptions: { maxBlocks: 1, maxSourceTokens: 100 },
      tinyWindowTokens: 32,
      maxRequestTokens: 100,
      maxWindowsPerRequest: 4,
      maxConcurrency: 2,
      hardDeadlineMs: 30_000,
    },
  };
}

test("fast mode uses larger bounded windows unless the caller supplies tighter limits", () => {
  assert.deepEqual(windowOptionsForRunMode("fast"), {
    targetSourceTokens: 3_200,
    maxSourceTokens: 4_800,
    maxBlocks: 4,
  });
  assert.deepEqual(windowOptionsForRunMode("fast", { maxSourceTokens: 900 }), {
    targetSourceTokens: 900,
    maxSourceTokens: 900,
    maxBlocks: 4,
  });
  assert.deepEqual(windowOptionsForRunMode("quality", {}), {});
});

test("fast mode keeps its default physical request limit aligned with a legal 4,800-token window", async () => {
  const fixture = losslessFixture([
    "가".repeat(2_000),
    "나".repeat(2_000),
    "다".repeat(2_000),
  ].join("\n\n"));
  fixture.faux.setResponses(Array.from({ length: 8 }, () => losslessBatchResponse));
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);

  const result = await runBook({
    ...fixture.options,
    model,
    streamFn,
    windowOptions: {},
    maxRequestTokens: undefined,
    maxConcurrency: 4,
    runtimeSet: {
      mode: "fast",
      primary: { model, streamFn, effort: "off", thinkingLevel: "off" },
      escalation: { model, streamFn, effort: "high", thinkingLevel: "high" },
    },
  } as never);

  assert.equal(result.status.humanRequiredWindows, 0);
  assert.equal(result.status.pendingWindows, 0);
  assert.equal(result.status.completedWindows + result.status.warningWindows, result.status.totalWindows);
});

test("clean fast waves prepare two scheduler horizons without exceeding runtime concurrency", async () => {
  const fixture = losslessFixture([
    "BOOK ONE", "CHAPTER ONE", "BOOK TWO", "CHAPTER TWO",
    "BOOK THREE", "CHAPTER THREE", "BOOK FOUR", "CHAPTER FOUR",
  ].join("\n\n"));
  assert.ok(fixture.submission.windows.length >= 4);
  fixture.faux.setResponses(Array.from(
    { length: fixture.submission.windows.length },
    () => losslessBatchResponse,
  ));
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);

  const result = await runBook({
    ...fixture.options,
    model,
    streamFn,
    tinyWindowTokens: 1,
    maxWindowsPerRequest: 1,
    maxConcurrency: 2,
    runtimeSet: {
      mode: "fast",
      primary: { model, streamFn, effort: "off", thinkingLevel: "off" },
      escalation: { model, streamFn, effort: "high", thinkingLevel: "high" },
    },
  } as never);

  assert.equal(result.waves[0]?.windowIds.length, 4);
  assert.ok(result.waves.every((wave) => wave.windowIds.length <= 4));
  assert.equal(result.status.completedWindows, result.status.totalWindows);
});

test("fast waves fall back to one scheduler horizon after a retry", async () => {
  const fixture = losslessFixture([
    "BOOK ONE", "CHAPTER ONE", "BOOK TWO", "CHAPTER TWO",
    "BOOK THREE", "CHAPTER THREE", "BOOK FOUR", "CHAPTER FOUR",
  ].join("\n\n"));
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);
  fixture.faux.setResponses([
    fauxAssistantMessage(fauxToolCall("finalize_translation_batch", { windows: [] }), {
      stopReason: "toolUse",
    }),
    ...Array.from({ length: fixture.submission.windows.length + 2 }, () => losslessBatchResponse),
  ]);

  const result = await runBook({
    ...fixture.options,
    model,
    streamFn,
    tinyWindowTokens: 1,
    maxWindowsPerRequest: 1,
    maxConcurrency: 2,
    runtimeSet: {
      mode: "fast",
      primary: { model, streamFn, effort: "off", thinkingLevel: "off" },
      escalation: { model, streamFn, effort: "high", thinkingLevel: "high" },
    },
  } as never);

  assert.deepEqual(result.waves.map((wave) => wave.windowIds.length), [4, 2, 2]);
  assert.equal(result.status.completedWindows, result.status.totalWindows);
});

test("a missing framed submission is retried as smaller lossless block fragments", async () => {
  const fixture = losslessFixture([
    "the ".repeat(1_700),
    "and ".repeat(1_700),
  ].join("\n\n"));
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);
  fixture.faux.setResponses([
    fauxAssistantMessage("I could not submit the structured result."),
    ...Array.from({ length: 10 }, () => losslessBatchResponse),
  ]);

  const result = await runBook({
    ...fixture.options,
    model,
    streamFn,
    windowOptions: { maxBlocks: 2, maxSourceTokens: 4_000 },
    maxRequestTokens: 4_000,
    maxConcurrency: 1,
    maxWindowsPerRequest: 1,
    runtimeSet: {
      mode: "fast",
      primary: { model, streamFn, effort: "off", thinkingLevel: "off" },
      escalation: { model, streamFn, effort: "high", thinkingLevel: "high" },
    },
  } as never);

  assert.equal(result.status.humanRequiredWindows, 0);
  assert.equal(result.status.completedWindows + result.status.warningWindows, result.status.totalWindows);
  assert.ok(result.windows.some((window) => window.blockIds.length >= 2));
  assert.ok(result.windows.every((window) => window.attemptCount === 1));
  assert.ok(fixture.faux.state.callCount >= 3);
  assert.ok(fixture.faux.state.callCount <= 6);
});

test("a degenerate multi-block translation is retried as isolated block fragments", async () => {
  const fixture = losslessFixture([
    "Alpha ".repeat(300),
    "Alpha ".repeat(300),
  ].join("[[]]"));
  const observedBlockGroups: string[][] = [];
  const degenerateFramedResponse = (context: Context) => {
    const prompt = userText(context);
    const windows = promptBatchWindows(context);
    observedBlockGroups.push(windows.flatMap((window) =>
      window.blocks.map((item) => item.blockId)));
    const promptLines = prompt.split(/\r?\n/gu).map((line) =>
      line.replace(/^\d+\.\s+/u, "").trimStart());
    const lines = windows.flatMap((window) => window.blocks.flatMap((item) => {
      const begin = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:BEGIN:${item.blockId}@@`));
      const end = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:END:${item.blockId}@@`));
      assert.ok(begin && end);
      return [begin, "摘要。", end];
    }));
    return fauxAssistantMessage(lines.join("\n"));
  };
  const degenerateRepairResponse = (context: Context) => {
    const ids = [...new Set([...userText(context).matchAll(/\[(block-[0-9a-f]+)\]/gu)]
      .map((match) => match[1])
      .filter((value): value is string => value !== undefined))];
    return fauxAssistantMessage(fauxToolCall("submit_repaired_translation", {
      translations: ids.map((blockId) => ({ blockId, text: "摘要。" })),
      notes: [],
    }), { stopReason: "toolUse" });
  };
  const validResponse = (context: Context) => {
    observedBlockGroups.push(promptBatchWindows(context).flatMap((window) =>
      window.blocks.map((item) => item.blockId)));
    return losslessBatchResponse(context);
  };
  fixture.faux.setResponses([
    degenerateFramedResponse,
    degenerateRepairResponse,
    validResponse,
    validResponse,
  ]);
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);

  const result = await runBook({
    ...fixture.options,
    model,
    streamFn,
    windowOptions: { maxBlocks: 2, maxSourceTokens: 4_000 },
    maxRequestTokens: 4_000,
    maxConcurrency: 1,
    maxWindowsPerRequest: 1,
    runtimeSet: {
      mode: "fast",
      primary: { model, streamFn, effort: "off", thinkingLevel: "off" },
      escalation: { model, streamFn, effort: "high", thinkingLevel: "high" },
    },
  } as never);

  assert.equal(result.status.humanRequiredWindows, 0);
  assert.equal(result.status.completedWindows, 1);
  assert.equal(result.windows[0]?.attemptCount, 1, JSON.stringify({
    windows: result.windows.map((window) => ({
      status: window.status,
      attemptCount: window.attemptCount,
      lastError: window.lastError,
    })),
    observedBlockGroups,
  }));
  assert.deepEqual(observedBlockGroups.map((items) => items.length), [2, 1, 1]);
});

test("a copied source-script multi-block translation is isolated before the outer retry", async () => {
  const fixture = losslessFixture([
    "\ubb35\ud5a5\uc740 \ucc9c\uc9c0\ubb38\uc73c\ub85c \uac78\uc5b4\uac14\ub2e4. ".repeat(50),
    "\uc124\uc57d\ubcbd\uc740 \uc625\uad00\ud328\uc5d0\uac8c \uc870\uc6a9\ud788 \ub2f5\ud588\ub2e4. ".repeat(50),
  ].join("[[]]"), "ko");
  const observedBlockGroups: string[][] = [];
  const copiedById = new Map<string, string>();
  const copiedResponse = (context: Context) => {
    const prompt = userText(context);
    const windows = promptBatchWindows(context);
    observedBlockGroups.push(windows.flatMap((window) => window.blocks.map((item) => item.blockId)));
    const promptLines = prompt.split(/\r?\n/gu).map((line) =>
      line.replace(/^\d+\.\s+/u, "").trimStart());
    const lines = windows.flatMap((window) => window.blocks.flatMap((item) => {
      assert.ok(item.sourceText);
      copiedById.set(item.blockId, item.sourceText);
      const begin = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:BEGIN:${item.blockId}@@`));
      const end = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:END:${item.blockId}@@`));
      assert.ok(begin && end);
      return [begin, item.sourceText, end];
    }));
    return fauxAssistantMessage(lines.join("\n"));
  };
  const copiedRepair = (context: Context) => {
    const ids = [...new Set([...userText(context).matchAll(/\[(block-[0-9a-f]+)\]/gu)]
      .map((match) => match[1])
      .filter((value): value is string => value !== undefined))];
    return fauxAssistantMessage(fauxToolCall("submit_repaired_translation", {
      translations: ids.map((blockId) => ({
        blockId,
        text: copiedById.get(blockId) ?? "\ubb35\ud5a5\uc740 \uadf8\ub300\ub85c \uc11c \uc788\uc5c8\ub2e4.",
      })),
      notes: [],
    }), { stopReason: "toolUse" });
  };
  const validResponse = (context: Context) => {
    const prompt = userText(context);
    const windows = promptBatchWindows(context);
    observedBlockGroups.push(windows.flatMap((window) =>
      window.blocks.map((item) => item.blockId)));
    const promptLines = prompt.split(/\r?\n/gu).map((line) =>
      line.replace(/^\d+\.\s+/u, "").trimStart());
    const lines = windows.flatMap((window) => window.blocks.flatMap((item) => {
      assert.ok(item.sourceText);
      const begin = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:BEGIN:${item.blockId}@@`));
      const end = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:END:${item.blockId}@@`));
      assert.ok(begin && end);
      const sentence = item.sourceText.includes("\ubb35\ud5a5")
        ? "墨香沿着山路走向天地门，叙事保留了沿途细节与行动因果。"
        : "薛若碧低声回答玉冠霸，译文完整保留了人物关系与现场变化。";
      const text = sentence.repeat(Math.ceil(Array.from(item.sourceText).length * 0.72 / Array.from(sentence).length));
      return [begin, text, end];
    }));
    return fauxAssistantMessage(lines.join("\n"));
  };
  fixture.faux.setResponses([
    copiedResponse,
    copiedRepair,
    ...Array.from({ length: 10 }, () => validResponse),
  ]);
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);

  const result = await runBook({
    ...fixture.options,
    model,
    streamFn,
    windowOptions: { maxBlocks: 2, maxSourceTokens: 4_000 },
    maxRequestTokens: 4_000,
    maxConcurrency: 1,
    maxWindowsPerRequest: 1,
    runtimeSet: {
      mode: "fast",
      primary: { model, streamFn, effort: "off", thinkingLevel: "off" },
      escalation: { model, streamFn, effort: "high", thinkingLevel: "high" },
    },
  } as never);

  assert.equal(result.status.humanRequiredWindows, 0, JSON.stringify({
    observedBlockGroups,
    callCount: fixture.faux.state.callCount,
    windows: result.windows.map((window) => ({
      status: window.status,
      attemptCount: window.attemptCount,
      lastError: window.lastError,
    })),
  }));
  assert.equal(result.windows[0]?.attemptCount, 1, JSON.stringify(result.windows));
  assert.deepEqual(observedBlockGroups.map((items) => items.length), [2, 1, 1]);
});

function createFixture(path: string, blockCount: number): void {
  const database = new DatabaseSync(path);
  database.exec(`
    CREATE TABLE blocks (
      id TEXT PRIMARY KEY, legacy_id TEXT, chapter_id TEXT, chapter_title TEXT,
      block_index INTEGER, global_index INTEGER, source_text TEXT,
      source_hash TEXT, token_count INTEGER
    );
    CREATE TABLE concepts (
      id TEXT PRIMARY KEY, canonical_source TEXT, default_target TEXT,
      working_target TEXT, verified_target TEXT, status TEXT, locked INTEGER,
      retired_version INTEGER
    );
    CREATE TABLE lexemes (
      id TEXT PRIMARY KEY, canonical_form TEXT, default_target TEXT,
      working_target TEXT, verified_target TEXT, status TEXT, locked INTEGER,
      retired_version INTEGER
    );
    CREATE TABLE concept_lexemes (
      concept_id TEXT, lexeme_id TEXT, status TEXT, retired_version INTEGER
    );
    CREATE TABLE source_forms (lexeme_id TEXT, form TEXT);
  `);
  const insert = database.prepare(`
    INSERT INTO blocks VALUES(?, ?, 'ch1', 'One', ?, ?, ?, ?, 12)
  `);
  for (let index = 0; index < blockCount; index += 1) {
    insert.run(
      `block-${index}`,
      `legacy-${index}`,
      index,
      index,
      `The person moved slowly through room ${index}.`,
      `hash-${index}`,
    );
  }
  database.close();
}

function userText(context: Context): string {
  const message = context.messages.findLast((item) => item.role === "user");
  assert.ok(message && message.role === "user");
  return typeof message.content === "string"
    ? message.content
    : message.content
      .filter((item) => item.type === "text")
      .map((item) => item.text)
      .join("\n");
}

function dynamicResponse(context: Context) {
  const prompt = userText(context);
  if (prompt.includes("Submit zero to four additional questions")) {
    return fauxAssistantMessage(
      fauxToolCall("submit_questions", { questions: [] }),
      { stopReason: "toolUse" },
    );
  }
  const ids = [...prompt.matchAll(/\[(block-\d+)\]/gu)]
    .map((match) => match[1])
    .filter((value): value is string => value !== undefined);
  assert.ok(ids.length > 0, prompt.slice(0, 300));
  return fauxAssistantMessage(fauxToolCall("finalize_translation", {
    translations: ids.map((blockId) => ({
      blockId,
      text: "这是对应文本块的完整中文译文，保留了原文全部信息。",
    })),
    notes: [],
  }), { stopReason: "toolUse" });
}

function losslessBatchResponse(context: Context) {
  const prompt = userText(context);
  const match = /WINDOWS\n\n(\[[\s\S]*?\])\n\nSTABLE TERMS/u.exec(prompt);
  assert.ok(match?.[1], prompt.slice(0, 500));
  const windows = JSON.parse(match[1]) as Array<{
    windowId: string;
    blocks: Array<{ blockId: string; sourceText: string }>;
  }>;
  const termMatch = /STABLE TERMS\n\n(\[[\s\S]*?\])\n\nUNRESOLVED ENTITY LINKS/u.exec(prompt);
  const terms = JSON.parse(termMatch?.[1] ?? "[]") as Array<{
    sourceForm: string;
    target: string;
  }>;
  const submission = {
    windows: windows.map((window) => ({
      windowId: window.windowId,
      translations: window.blocks.map((block) => {
        const prefix = [...block.blockId.slice(-8)].map((digit) => ({
          "0": "零", "1": "一", "2": "二", "3": "三",
          "4": "四", "5": "五", "6": "六", "7": "七",
          "8": "八", "9": "九", a: "甲", b: "乙", c: "丙",
          d: "丁", e: "戊", f: "己",
        })[digit] ?? "庚").join("");
        const relevantTargets = terms.filter((term) => block.sourceText.toLocaleLowerCase()
          .includes(term.sourceForm.toLocaleLowerCase()))
          .map((term) => term.target).join("、");
        const paragraphs = block.sourceText
          .split(/(?:\r?\n)[\t ]*(?:\r?\n)+/u)
          .filter((paragraph) => paragraph.trim().length > 0);
        return {
          blockId: block.blockId,
          text: paragraphs.map((paragraph, index) =>
            `${index === 0 ? `${prefix}${relevantTargets}` : "续"}${"完整译文".repeat(
              Math.max(1, Math.ceil([...paragraph].length / 12)),
            )}。`).join("\n\n"),
        };
      }),
      notes: [],
    })),
  };
  if (prompt.includes("EXACT FRAME PAIRS")) {
    const promptLines = prompt.split(/\r?\n/gu).map((line) =>
      line.replace(/^\d+\.\s+/u, "").trimStart());
    const responseLines = submission.windows.flatMap((window) =>
      window.translations.flatMap((translation) => {
        const begin = promptLines.find((line) =>
          line.startsWith("@@FOLIOLOOM:")
          && line.endsWith(`:BEGIN:${translation.blockId}@@`));
        const end = promptLines.find((line) =>
          line.startsWith("@@FOLIOLOOM:")
          && line.endsWith(`:END:${translation.blockId}@@`));
        assert.ok(begin && end, `missing frame markers for ${translation.blockId}`);
        return [begin, translation.text, end];
      }));
    return fauxAssistantMessage(responseLines.join("\n"));
  }
  return fauxAssistantMessage(fauxToolCall("finalize_translation_batch", submission), {
    stopReason: "toolUse",
  });
}

function promptBatchWindows(context: Context): Array<{
  windowId: string;
  blocks: Array<{ blockId: string; sourceText?: string }>;
}> {
  const prompt = userText(context);
  const match = /WINDOWS\n\n(\[[\s\S]*?\])\n\nSTABLE TERMS/u.exec(prompt);
  assert.ok(match?.[1], prompt.slice(0, 500));
  return JSON.parse(match[1]) as Array<{
    windowId: string;
    blocks: Array<{ blockId: string; sourceText?: string }>;
  }>;
}

function lexicalAnchorResponse(
  context: Context,
  entityLink?: {
    sourceForms: string[];
    proposedTarget: string;
    evidenceQuote: string;
  },
) {
  const prompt = userText(context);
  const match = /SOURCE-LANGUAGE FORMS AND COMPACT CONCORDANCE\n\n(\[[\s\S]*?\])\n\nESTABLISHED TERMS/u.exec(prompt);
  assert.ok(match?.[1], prompt.slice(0, 500));
  const candidates = JSON.parse(match[1]) as Array<{ sourceForm: string }>;
  return fauxAssistantMessage(fauxToolCall("submit_lexical_anchors", {
    anchors: candidates.map((candidate) => ({
      sourceForm: candidate.sourceForm,
      target: entityLink?.sourceForms.includes(candidate.sourceForm)
        ? entityLink.proposedTarget
        : `译-${candidate.sourceForm}`,
      mode: "stable",
      semanticClass: entityLink?.sourceForms.includes(candidate.sourceForm)
        ? "proper_name"
        : "technical_term",
      confidence: 0.95,
    })),
    entityLinks: entityLink === undefined ? [] : [{
      ...entityLink,
      evidenceKind: "explicit_naming",
      confidence: 0.98,
    }],
  }), { stopReason: "toolUse" });
}

function lexicalPreferredFallbackResponse(
  context: Context,
  targets: Readonly<Record<string, string>>,
) {
  const prompt = userText(context);
  const begin = /^@@FOLIOLOOM:LEXICAL-PREFERRED:[a-f0-9]+:BEGIN@@$/mu.exec(prompt)?.[0];
  const end = /^@@FOLIOLOOM:LEXICAL-PREFERRED:[a-f0-9]+:END@@$/mu.exec(prompt)?.[0];
  assert.ok(begin, prompt.slice(0, 1_000));
  assert.ok(end, prompt.slice(0, 1_000));
  return fauxAssistantMessage([
    begin,
    JSON.stringify(Object.entries(targets).map(([sourceForm, target]) => ({
      sourceForm,
      target,
      semanticClass: "proper_name",
      confidence: 0.95,
    }))),
    end,
  ].join("\n"));
}

test("completed waves remember contextual anchor decisions and free later slots for names", async () => {
  const diffuse = [
    "보이지", "정점", "있던", "빨리", "얘기", "제자", "교주님", "하지",
    "그들", "음식", "주십시오", "사부님", "선배님", "지나자", "아니", "보여",
  ];
  const commonText = diffuse.map((form) => `${form}는 ${form}가`).join(". ");
  const namedText = `${commonText}. 진양은 진양이 진양을 만났다. 옥관패는 옥관패가 옥관패를 들었다.`;
  const fixture = losslessFixture([
    commonText,
    commonText,
    namedText,
    namedText,
  ].join("[[]]"), "ko");
  const anchorWaves: string[][] = [];
  const response = (context: Context) => {
    const prompt = userText(context);
    if (prompt.includes("SOURCE-LANGUAGE FORMS AND COMPACT CONCORDANCE")) {
      const match = /SOURCE-LANGUAGE FORMS AND COMPACT CONCORDANCE\n\n(\[[\s\S]*?\])\n\nESTABLISHED TERMS/u.exec(prompt);
      assert.ok(match?.[1]);
      const candidates = JSON.parse(match[1]) as Array<{ sourceForm: string }>;
      anchorWaves.push(candidates.map((candidate) => candidate.sourceForm));
      return fauxAssistantMessage(fauxToolCall("submit_lexical_anchors", {
        anchors: candidates.map((candidate) => ({
          sourceForm: candidate.sourceForm,
          target: "",
          mode: "contextual",
          semanticClass: "ordinary_word",
          confidence: 0.99,
        })),
        entityLinks: [],
      }), { stopReason: "toolUse" });
    }
    return losslessBatchResponse(context);
  };
  fixture.faux.setResponses(Array.from({ length: 12 }, () => response));
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);

  const result = await runBook({
    ...fixture.options,
    model,
    streamFn,
    windowOptions: { maxBlocks: 1, maxSourceTokens: 1_000 },
    maxRequestTokens: 1_000,
    maxWindowsPerRequest: 1,
    maxConcurrency: 1,
    runtimeSet: {
      mode: "fast",
      primary: { model, streamFn, effort: "off", thinkingLevel: "off" },
      escalation: { model, streamFn, effort: "high", thinkingLevel: "high" },
    },
  } as never);

  assert.equal(result.status.humanRequiredWindows, 0);
  assert.ok(anchorWaves.length >= 2, JSON.stringify(anchorWaves));
  assert.ok(anchorWaves[1]?.includes("진양"), JSON.stringify(anchorWaves));
  assert.ok(anchorWaves[1]?.includes("옥관패"), JSON.stringify(anchorWaves));
  assert.ok(anchorWaves[1]?.every((form) => !anchorWaves[0]?.includes(form)), JSON.stringify(anchorWaves));
});

test("a stable anchor below the projection threshold is reconsidered in the next wave", async () => {
  const source = "Smoky met Edgewood. Smoky left Edgewood.";
  const fixture = losslessFixture(`${source}[[]]${source}`);
  const anchorWaves: string[][] = [];
  const response = (context: Context) => {
    const prompt = userText(context);
    if (prompt.includes("SOURCE-LANGUAGE FORMS AND COMPACT CONCORDANCE")) {
      const match = /SOURCE-LANGUAGE FORMS AND COMPACT CONCORDANCE\n\n(\[[\s\S]*?\])\n\nESTABLISHED TERMS/u.exec(prompt);
      assert.ok(match?.[1]);
      const candidates = JSON.parse(match[1]) as Array<{ sourceForm: string }>;
      anchorWaves.push(candidates.map((candidate) => candidate.sourceForm));
      return fauxAssistantMessage(fauxToolCall("submit_lexical_anchors", {
        anchors: candidates.map((candidate) => ({
          sourceForm: candidate.sourceForm,
          target: `\u8bd1-${candidate.sourceForm}`,
          mode: "stable",
          semanticClass: "proper_name",
          confidence: 0.79,
        })),
        entityLinks: [],
      }), { stopReason: "toolUse" });
    }
    return losslessBatchResponse(context);
  };
  fixture.faux.setResponses(Array.from({ length: 8 }, () => response));
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);

  const runOptions = {
    ...fixture.options,
    model,
    streamFn,
    windowOptions: { maxBlocks: 1, maxSourceTokens: 1_000 },
    maxRequestTokens: 1_000,
    maxWindowsPerRequest: 1,
    maxConcurrency: 1,
  };
  const first = await runBook({ ...runOptions, maxWindows: 1 } as never);
  assert.equal(first.status.completedWindows, 1);
  const result = await runBook(runOptions as never);

  assert.equal(result.status.completedWindows, 2);
  assert.equal(anchorWaves.length, 2, JSON.stringify(anchorWaves));
  assert.ok(anchorWaves[0]?.every((form) => anchorWaves[1]?.includes(form)));
});

test("adjacent physical requests cannot commit the same ungrounded long translation", async () => {
  const fixture = losslessFixture([
    "alpha river stone wind ".repeat(24),
    "beta forest cloud rain ".repeat(24),
  ].join("[[]]"));
  let translationCall = 0;
  const response = (context: Context) => {
    translationCall += 1;
    const prompt = userText(context);
    const windows = promptBatchWindows(context);
    const promptLines = prompt.split(/\r?\n/gu).map((line) =>
      line.replace(/^\d+\.\s+/u, "").trimStart());
    const responseLines = windows.flatMap((window) => window.blocks.flatMap((item) => {
      const begin = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:BEGIN:${item.blockId}@@`));
      const end = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:END:${item.blockId}@@`));
      assert.ok(begin && end);
      const text = translationCall <= 2
        ? "这是跨请求重复的同一段错误译文。".repeat(22)
        : translationCall === 3
          ? "晨光越过群山，旅人沿着石径缓缓走向远方。".repeat(18)
          : "夜潮拍打礁岸，渔船收起风帆驶入寂静港湾。".repeat(18);
      return [begin, text, end];
    }));
    return fauxAssistantMessage(responseLines.join("\n"));
  };
  fixture.faux.setResponses(Array.from({ length: 8 }, () => response));
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);

  const result = await runBook({
    ...fixture.options,
    model,
    streamFn,
    windowOptions: { maxBlocks: 1, maxSourceTokens: 1_000 },
    maxRequestTokens: 1_000,
    maxWindowsPerRequest: 1,
    maxConcurrency: 1,
    runtimeSet: {
      mode: "fast",
      primary: { model, streamFn, effort: "off", thinkingLevel: "off" },
      escalation: { model, streamFn, effort: "high", thinkingLevel: "high" },
    },
  } as never);

  assert.equal(result.status.humanRequiredWindows, 0, JSON.stringify({
    translationCall,
    windows: result.windows.map((window) => ({
      status: window.status,
      attemptCount: window.attemptCount,
      lastError: window.lastError,
    })),
  }));
  assert.ok(translationCall >= 3, `expected a boundary retry, got ${translationCall} calls`);
  const store = new LosslessBookStore(fixture.options.storePath);
  try {
    assert.equal(new Set(store.activeTranslations("run-lossless").map((item) => item.text)).size, 2);
  } finally {
    store.close();
  }
});

test("cross-window overlap after repair is isolated at immutable window boundaries", async () => {
  const fixture = losslessFixture([
    "alpha river stone wind ".repeat(24),
    "beta forest cloud rain ".repeat(24),
  ].join("[[]]"));
  const observedBlockGroups: string[][] = [];
  const duplicateResponse = (context: Context) => {
    const prompt = userText(context);
    const windows = promptBatchWindows(context);
    observedBlockGroups.push(windows.flatMap((window) =>
      window.blocks.map((item) => item.blockId)));
    const promptLines = prompt.split(/\r?\n/gu).map((line) =>
      line.replace(/^\d+\.\s+/u, "").trimStart());
    return fauxAssistantMessage(windows.flatMap((window) => window.blocks.flatMap((item) => {
      const begin = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:BEGIN:${item.blockId}@@`));
      const end = promptLines.find((line) =>
        line.startsWith("@@FOLIOLOOM:") && line.endsWith(`:END:${item.blockId}@@`));
      assert.ok(begin && end);
      return [begin, "这是跨窗口重复的同一段错误译文。".repeat(22), end];
    })).join("\n"));
  };
  const duplicateRepair = (context: Context) => {
    const ids = [...new Set([...userText(context).matchAll(/\[(block-[0-9a-f]+)\]/gu)]
      .map((match) => match[1])
      .filter((value): value is string => value !== undefined))];
    return fauxAssistantMessage(fauxToolCall("submit_repaired_translation", {
      translations: ids.map((blockId) => ({
        blockId,
        text: "这是跨窗口重复的同一段错误译文。".repeat(22),
      })),
      notes: [],
    }), { stopReason: "toolUse" });
  };
  const validResponse = (context: Context) => {
    observedBlockGroups.push(promptBatchWindows(context).flatMap((window) =>
      window.blocks.map((item) => item.blockId)));
    return losslessBatchResponse(context);
  };
  fixture.faux.setResponses([
    duplicateResponse,
    duplicateRepair,
    validResponse,
    validResponse,
  ]);
  const model = fixture.faux.getModel();
  const streamFn = fixture.faux.provider.streamSimple.bind(fixture.faux.provider);

  const result = await runBook({
    ...fixture.options,
    model,
    streamFn,
    windowOptions: { maxBlocks: 1, maxSourceTokens: 1_000 },
    tinyWindowTokens: 1_000,
    maxRequestTokens: 2_000,
    maxWindowsPerRequest: 2,
    maxConcurrency: 1,
    runtimeSet: {
      mode: "fast",
      primary: { model, streamFn, effort: "off", thinkingLevel: "off" },
      escalation: { model, streamFn, effort: "high", thinkingLevel: "high" },
    },
  } as never);

  assert.equal(result.status.humanRequiredWindows, 0, JSON.stringify(result.windows));
  assert.deepEqual(observedBlockGroups.map((items) => items.length), [2, 1, 1]);
});

test("wave anchor runs once and every physical request receives the same preferred alias targets", async () => {
  const fixture = losslessFixture(
    "Loukianos, whom they called Lucian the Scoffer, laughed.\n\nBOOK ONE",
  );
  const translationPrompts: string[] = [];
  fixture.faux.setResponses([
    (context) => lexicalAnchorResponse(context, {
      sourceForms: ["Loukianos", "Lucian"],
      proposedTarget: "卢基阿诺斯",
      evidenceQuote: "Loukianos, whom they called Lucian the Scoffer, laughed.",
    }),
    (context) => {
      translationPrompts.push(userText(context));
      return losslessBatchResponse(context);
    },
    (context) => {
      translationPrompts.push(userText(context));
      return losslessBatchResponse(context);
    },
  ]);

  const result = await runBook({
    ...fixture.options,
    tinyWindowTokens: 1,
    maxWindowsPerRequest: 1,
    maxConcurrency: 2,
  } as never);

  assert.equal(fixture.faux.state.callCount, 3);
  assert.equal(result.status.modelCalls, 3);
  assert.equal(translationPrompts.length, 2);
  const projectedTerms = translationPrompts.map((prompt) => {
    const match = /STABLE TERMS\n\n(\[[\s\S]*?\])\n\nUNRESOLVED ENTITY LINKS/u.exec(prompt);
    assert.ok(match?.[1]);
    return JSON.parse(match[1]) as Array<{
      sourceForm: string;
      target: string;
      conceptId: string;
      locked: boolean;
    }>;
  });
  assert.deepEqual(projectedTerms[0], projectedTerms[1]);
  const aliases = projectedTerms[0]!.filter((term) =>
    term.sourceForm === "Loukianos" || term.sourceForm === "Lucian");
  assert.equal(aliases.length, 2);
  assert.equal(new Set(aliases.map((term) => term.target)).size, 1);
  assert.equal(new Set(aliases.map((term) => term.conceptId)).size, 2);
  assert.ok(aliases.every((term) => term.locked === false));

  const store = new LosslessBookStore(fixture.options.storePath);
  const revisions = store.knowledgeRevisions("run-lossless");
  store.close();
  assert.equal(revisions.filter((revision) => revision.kind === "entity_alias_link").length, 1);
});

test("one source-authored Hanja candidate reaches translation without a model anchor round trip", async () => {
  const fixture = losslessFixture(
    "\uc625\uad00\ud328(\u7389\u51a0\u8987)\uac00 \uc654\ub2e4. \uc625\uad00\ud328\uac00 \ub2e4\uc2dc \uc654\ub2e4.",
    "ko",
  );
  fixture.faux.setResponses([(context) => {
    const prompt = userText(context);
    assert.doesNotMatch(prompt, /SOURCE-LANGUAGE FORMS AND COMPACT CONCORDANCE/u);
    const match = /STABLE TERMS\n\n(\[[\s\S]*?\])\n\nUNRESOLVED ENTITY LINKS/u.exec(prompt);
    assert.ok(match?.[1]);
    const terms = JSON.parse(match[1]) as Array<{
      sourceForm: string;
      target: string;
      policy: string;
    }>;
    assert.deepEqual(
      terms.filter((term) => term.sourceForm === "\uc625\uad00\ud328")
        .map((term) => [term.target, term.policy]),
      [["\u7389\u51a0\u9738", "preferred"]],
    );
    return losslessBatchResponse(context);
  }]);

  const result = await runBook(fixture.options as never);

  assert.equal(result.status.completedWindows, 1);
  assert.equal(fixture.faux.state.callCount, 1);
});

test("failed wave promotes no anchor knowledge and resume reuses its cached anchor decision", async () => {
  const fixture = losslessFixture(
    "Loukianos, whom they called Lucian the Scoffer, laughed.",
  );
  fixture.faux.setResponses([
    (context) => lexicalAnchorResponse(context, {
      sourceForms: ["Loukianos", "Lucian"],
      proposedTarget: "卢基阿诺斯",
      evidenceQuote: "Loukianos, whom they called Lucian the Scoffer, laughed.",
    }),
    fauxAssistantMessage([], {
      stopReason: "error",
      errorMessage: "503: fixture provider unavailable",
    }),
  ]);

  await assert.rejects(runBook(fixture.options as never), /provider unavailable/i);
  const failedStore = new LosslessBookStore(fixture.options.storePath);
  assert.deepEqual(failedStore.knowledgeRevisions("run-lossless"), []);
  failedStore.close();

  const resumedProvider = fauxProvider();
  resumedProvider.setResponses([losslessBatchResponse]);
  const resumed = await runBook({
    ...fixture.options,
    model: resumedProvider.getModel(),
    streamFn: resumedProvider.provider.streamSimple.bind(resumedProvider.provider),
  } as never);

  assert.equal(resumedProvider.state.callCount, 1);
  assert.equal(resumed.status.completedWindows, 1);
  const recoveredStore = new LosslessBookStore(fixture.options.storePath);
  assert.equal(
    recoveredStore.knowledgeRevisions("run-lossless")
      .filter((revision) => revision.kind === "entity_alias_link").length,
    1,
  );
  recoveredStore.close();
});

test("two tiny logical windows use one physical model session and commit independently", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE");
  fixture.faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    fixture.submission,
  ), { stopReason: "toolUse" })]);

  const result = await runBook(fixture.options as never);

  assert.equal(fixture.faux.state.callCount, 1);
  assert.equal(result.status.completedWindows, 2);
  assert.equal(result.status.modelCalls, 1);
  const store = new LosslessBookStore(fixture.options.storePath);
  try {
    assert.deepEqual(store.listTranslationRuns()[0]?.metadata, {
      sourceLanguageProfile: {
        id: "en",
        version: "source-language-profile-5",
        compatibilityMode: false,
      },
      sourceAnomalies: {
        schema: "v5-source-anomaly-1",
        counts: {
          CONTROL_CHARACTER: 0,
          EXTREME_LONG_LINE: 0,
          REPEATED_FRONTMATTER_LINE: 0,
          REPLACEMENT_CHARACTER: 0,
          SPACED_HYPHENATION: 0,
        },
        findings: [],
      },
      translationRuntime: {
        mode: "quality",
        primary: { modelId: "faux-1" },
        escalation: { modelId: "faux-1" },
      },
    });
    const scheduler = store.latestSchedulerSnapshot("run-lossless");
    assert.ok(scheduler);
    assert.equal(scheduler.inFlight, 0);
    assert.equal(scheduler.inFlightTokens, 0);
  } finally {
    store.close();
  }
});

test("failed lossless doctor blocks every model call", async () => {
  const fixture = losslessFixture("Alpha.");
  writeFileSync(fixture.canonicalPath, "Corrupt.", "utf8");

  await assert.rejects(runBook(fixture.options as never), /HASH_MISMATCH/);
  assert.equal(fixture.faux.state.callCount, 0);
});

test("lossless runner feeds single-call provider usage back into model-scoped calibration", async () => {
  const fixture = losslessFixture("A plain sentence without named entities.");
  fixture.faux.setResponses([losslessBatchResponse]);
  const estimator = new TrackingTokenEstimator();

  const result = await runBook({
    ...fixture.options,
    tokenEstimator: estimator,
  } as never);

  assert.equal(result.status.completedWindows + result.status.warningWindows, 1);
  assert.equal(estimator.observations.length, 1);
  assert.equal(estimator.observations[0]?.modelId, fixture.faux.getModel().id);
  assert.equal(estimator.observations[0]?.profile.id, "en");
  assert.ok((estimator.observations[0]?.actualInputTokens ?? 0) > 0);
});

test("complete request admission rejects a tiny context before any provider call", async () => {
  const fixture = losslessFixture("The bell rings above the empty court.");
  const model = {
    ...fixture.faux.getModel(),
    contextWindow: 256,
    maxTokens: 128,
  };
  await assert.rejects(
    runBook({ ...fixture.options, model } as never),
    (error: unknown) => error instanceof BookRequestCapacityError,
  );
  assert.equal(fixture.faux.state.callCount, 0);
});

test("preflight splits one oversized logical window only at immutable block boundaries", async () => {
  const fixture = losslessFixture(`${"word ".repeat(3_000)}.`);
  const observedBlockGroups: string[][] = [];
  const observedRequestIds: string[] = [];
  fixture.faux.setResponses(Array.from({ length: 2 }, () => (context: Context) => {
    const prompt = userText(context);
    observedRequestIds.push(/PHYSICAL REQUEST ([^\n]+)/u.exec(prompt)?.[1] ?? "");
    observedBlockGroups.push(promptBatchWindows(context).flatMap((window) =>
      window.blocks.map((block) => block.blockId)));
    return losslessBatchResponse(context);
  }));
  const tokenEstimator = {
    estimateText(text: string) {
      return {
        tokens: text.startsWith("WINDOWS\n\n")
          ? (text.match(/"blockId"/gu)?.length ?? 0) * 900
          : 1,
        uncertainty: 0,
      };
    },
    estimateJson() {
      return { tokens: 0, uncertainty: 0 };
    },
    observeUsage() {},
  };
  const model = {
    ...fixture.faux.getModel(),
    contextWindow: 2_500,
    maxTokens: 128,
  };

  const result = await runBook({
    ...fixture.options,
    model,
    tokenEstimator,
    maxWindows: 1,
    maxRequestTokens: 5_000,
    maxInFlightTokens: 2_500,
    windowOptions: {
      maxBlocks: 4,
      targetSourceTokens: 5_000,
      maxSourceTokens: 5_000,
    },
  } as never);

  assert.equal(result.status.totalWindows, 1);
  assert.equal(result.status.completedWindows, 1);
  assert.equal(fixture.faux.state.callCount, 2);
  assert.deepEqual(observedBlockGroups.map((blocks) => blocks.length), [1, 2]);
  assert.equal(new Set(observedRequestIds).size, 2);
  const database = new DatabaseSync(fixture.options.storePath);
  try {
    const events = (database.prepare(`
      SELECT kind, COUNT(*) AS count FROM events
      WHERE run_id='run-lossless' AND kind IN ('window_claimed', 'window_staged', 'window_promoted')
      GROUP BY kind
    `).all() as Array<{ kind: string; count: number }>).map((row) => ({ ...row }));
    assert.deepEqual(events, [
      { kind: "window_claimed", count: 1 },
      { kind: "window_promoted", count: 1 },
      { kind: "window_staged", count: 1 },
    ]);
  } finally {
    database.close();
  }
  const store = new LosslessBookStore(fixture.options.storePath);
  try {
    const state = store.auditState("run-lossless");
    const expected = state.memberships.map((membership) => membership.blockId);
    assert.deepEqual(observedBlockGroups.flat(), expected);
    assert.deepEqual(state.translations.map((translation) => translation.blockId), expected);
  } finally {
    store.close();
  }
});

test("runtime context overflow splits a physical batch before escalating its thinking", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE");
  const primary = fauxProvider();
  const escalation = fauxProvider();
  const observedBlockGroups: string[][] = [];
  const observedRequestIds: string[] = [];
  primary.setResponses([
    (context: Context) => {
      const prompt = userText(context);
      observedRequestIds.push(/PHYSICAL REQUEST ([^\n]+)/u.exec(prompt)?.[1] ?? "");
      observedBlockGroups.push(promptBatchWindows(context).flatMap((window) =>
        window.blocks.map((block) => block.blockId)));
      return fauxAssistantMessage([], {
        stopReason: "error",
        errorMessage: "input exceeds the context window",
      });
    },
    (context: Context) => {
      const prompt = userText(context);
      observedRequestIds.push(/PHYSICAL REQUEST ([^\n]+)/u.exec(prompt)?.[1] ?? "");
      observedBlockGroups.push(promptBatchWindows(context).flatMap((window) =>
        window.blocks.map((block) => block.blockId)));
      return losslessBatchResponse(context);
    },
    (context: Context) => {
      const prompt = userText(context);
      observedRequestIds.push(/PHYSICAL REQUEST ([^\n]+)/u.exec(prompt)?.[1] ?? "");
      observedBlockGroups.push(promptBatchWindows(context).flatMap((window) =>
        window.blocks.map((block) => block.blockId)));
      return losslessBatchResponse(context);
    },
  ]);
  const primaryModel = primary.getModel();
  const primaryStream = primary.provider.streamSimple.bind(primary.provider);

  const result = await runBook({
    ...fixture.options,
    model: primaryModel,
    streamFn: primaryStream,
    maxAttempts: 1,
    runtimeSet: {
      mode: "fast",
      primary: {
        model: primaryModel,
        streamFn: primaryStream,
        effort: "off",
        thinkingLevel: "off",
      },
      escalation: {
        model: escalation.getModel(),
        streamFn: escalation.provider.streamSimple.bind(escalation.provider),
        effort: "high",
        thinkingLevel: "high",
      },
    },
  } as never);

  assert.equal(result.status.completedWindows, 2);
  assert.equal(primary.state.callCount, 3);
  assert.equal(escalation.state.callCount, 0);
  assert.deepEqual(observedBlockGroups.map((blocks) => blocks.length), [2, 1, 1]);
  assert.equal(new Set(observedRequestIds).size, 3);
  const database = new DatabaseSync(fixture.options.storePath);
  try {
    const events = (database.prepare(`
      SELECT kind, COUNT(*) AS count FROM events
      WHERE run_id='run-lossless' AND kind IN ('window_claimed', 'window_staged', 'window_promoted')
      GROUP BY kind
    `).all() as Array<{ kind: string; count: number }>).map((row) => ({ ...row }));
    assert.deepEqual(events, [
      { kind: "window_claimed", count: 2 },
      { kind: "window_promoted", count: 2 },
      { kind: "window_staged", count: 2 },
    ]);
  } finally {
    database.close();
  }
});

test("a runtime context overflow for one indivisible block has a capacity error", async () => {
  const fixture = losslessFixture("the only quiet line.");
  fixture.faux.setResponses([fauxAssistantMessage([], {
    stopReason: "error",
    errorMessage: "input exceeds the context window",
  })]);

  await assert.rejects(
    runBook(fixture.options as never),
    (error: unknown) => error instanceof BookRequestCapacityError,
  );
  assert.equal(fixture.faux.state.callCount, 1);
});

test("lossless book aborts before selecting a new wave and reaches no provider call", async () => {
  const fixture = losslessFixture("The bell rings above the empty court.");
  const controller = new AbortController();
  controller.abort(new Error("test requested abort"));

  await assert.rejects(
    runBook({ ...fixture.options, signal: controller.signal } as never),
    /test requested abort/i,
  );
  assert.equal(fixture.faux.state.callCount, 0);
});

test("lossless book forwards cancellation into its active Pi request and commits no translation", async () => {
  const fixture = losslessFixture("The bell rings above the empty court.");
  const controller = new AbortController();
  let release!: () => void;
  const releaseProvider = new Promise<void>((resolveProvider) => { release = resolveProvider; });
  let entered!: () => void;
  const enteredProvider = new Promise<void>((resolveEntered) => { entered = resolveEntered; });
  let providerSignal: AbortSignal | undefined;
  fixture.faux.setResponses([async (context, options) => {
    providerSignal = options?.signal;
    entered();
    await releaseProvider;
    return losslessBatchResponse(context);
  }]);

  const running = runBook({ ...fixture.options, signal: controller.signal } as never);
  await enteredProvider;
  controller.abort(new Error("midflight abort"));
  assert.equal(providerSignal?.aborted, true);
  release();

  await assert.rejects(running, /midflight abort/i);
  const store = new LosslessBookStore(fixture.options.storePath);
  try {
    const status = store.statusSummary("run-lossless");
    assert.equal(status.completedWindows, 0);
    assert.equal(status.runningWindows, 1);
    assert.deepEqual(store.auditState("run-lossless").translations, []);
  } finally {
    store.close();
  }
});

test("lossless book forwards cancellation into its lexical-anchor Pi request", async () => {
  const fixture = losslessFixture("Loukianos, whom they called Lucian the Scoffer, laughed.");
  const controller = new AbortController();
  let release!: () => void;
  const releaseProvider = new Promise<void>((resolveProvider) => { release = resolveProvider; });
  let entered!: () => void;
  const enteredProvider = new Promise<void>((resolveEntered) => { entered = resolveEntered; });
  let providerSignal: AbortSignal | undefined;
  fixture.faux.setResponses([async (context, options) => {
    providerSignal = options?.signal;
    entered();
    await releaseProvider;
    return lexicalAnchorResponse(context, {
      sourceForms: ["Loukianos", "Lucian"],
      proposedTarget: "卢基阿诺斯",
      evidenceQuote: "Loukianos, whom they called Lucian the Scoffer, laughed.",
    });
  }]);

  const running = runBook({ ...fixture.options, signal: controller.signal } as never);
  await enteredProvider;
  controller.abort(new Error("anchor abort"));
  assert.equal(providerSignal?.aborted, true);
  release();

  await assert.rejects(running, /anchor abort/i);
  const store = new LosslessBookStore(fixture.options.storePath);
  try {
    assert.equal(store.statusSummary("run-lossless").pendingWindows, 1);
    assert.deepEqual(store.auditState("run-lossless").translations, []);
  } finally {
    store.close();
  }
});

test("lossless runner injects only glossary terms relevant to each physical request", async () => {
  const fixture = losslessFixture("BOOK ONE\n\nTyphon spoke.\n\nCHAPTER ONE\n\nSeverian listened.");
  const glossaryPath = join(dirname(fixture.canonicalPath), "glossary.json");
  writeFileSync(glossaryPath, JSON.stringify({
    Typhon: "提丰",
    Severian: "塞万里安",
  }), "utf8");
  const context = BookContext.openLossless({ manifestPath: fixture.options.manifestPath });
  const glossary = loadGlossary({
    glossaryPath,
    blocks: context.losslessBlocks,
    profile: context.languageProfile,
  });
  context.close();
  const prompts: string[] = [];
  fixture.faux.setResponses([
    (request) => {
      prompts.push(userText(request));
      return losslessBatchResponse(request);
    },
    (request) => {
      prompts.push(userText(request));
      return losslessBatchResponse(request);
    },
  ]);

  const result = await runBook({
    ...fixture.options,
    glossary,
    tinyWindowTokens: 1,
    maxWindowsPerRequest: 1,
    maxConcurrency: 1,
  } as never);

  assert.equal(result.status.completedWindows, 2, JSON.stringify({
    status: result.status,
    windows: result.windows.map((window) => ({
      id: window.windowId,
      status: window.status,
    })),
    promptCount: prompts.length,
  }));
  assert.match(prompts[0] ?? "", /"sourceForm":"Typhon"/u);
  assert.doesNotMatch(prompts[0] ?? "", /"sourceForm":"Severian"/u);
  assert.match(prompts[1] ?? "", /"sourceForm":"Severian"/u);
  assert.doesNotMatch(prompts[1] ?? "", /"sourceForm":"Typhon"/u);
});

test("one malformed typed window preserves its valid sibling while framed fallback repairs only the failure", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE");
  const malformed = structuredClone(fixture.submission);
  malformed.windows[1]!.translations[0]!.text = "";
  fixture.faux.setResponses([
    fauxAssistantMessage(fauxToolCall(
      "finalize_translation_batch",
      malformed,
    ), { stopReason: "toolUse" }),
    losslessBatchResponse,
  ]);

  const result = await runBook({ ...fixture.options, maxAttempts: 1 } as never);

  assert.equal(fixture.faux.state.callCount, 2);
  assert.equal(result.status.completedWindows, 2);
  assert.equal(result.status.humanRequiredWindows, 0);
  assert.deepEqual(result.windows.map((window) => window.status), [
    "completed",
    "completed",
  ]);
  const store = new LosslessBookStore(fixture.options.storePath);
  assert.deepEqual(store.styleObservations("run-lossless").map((item) => item.ordinal), [0, 1]);
  store.close();
});

test("lossless provider errors stay retryable and never become human incidents", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE");
  fixture.faux.setResponses([fauxAssistantMessage([], {
    stopReason: "error",
    errorMessage: "503: fixture provider unavailable",
  })]);

  await assert.rejects(runBook(fixture.options as never), /provider unavailable/i);
  const store = new LosslessBookStore(fixture.options.storePath);
  const status = store.statusSummary("run-lossless");
  store.close();
  assert.equal(status.humanRequiredWindows, 0);
  assert.equal(status.pendingWindows, 2);
});

test("fast mode retries an invalid physical request with only the escalation runtime", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE");
  const primary = fauxProvider();
  const escalation = fauxProvider();
  primary.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [] },
  ), { stopReason: "toolUse" })]);
  escalation.setResponses([losslessBatchResponse]);
  const primaryModel = primary.getModel();
  const primaryStream = primary.provider.streamSimple.bind(primary.provider);

  const result = await runBook({
    ...fixture.options,
    model: primaryModel,
    streamFn: primaryStream,
    runtimeSet: {
      mode: "fast",
      primary: {
        model: primaryModel,
        streamFn: primaryStream,
        effort: "off",
        thinkingLevel: "off",
      },
      escalation: {
        model: escalation.getModel(),
        streamFn: escalation.provider.streamSimple.bind(escalation.provider),
        effort: "high",
        thinkingLevel: "high",
      },
    },
  } as never);

  assert.equal(primary.state.callCount, 1);
  assert.equal(escalation.state.callCount, 1);
  assert.equal(result.status.completedWindows, 2);
});

test("quality mode falls back from a malformed typed payload to framed text before human review", async () => {
  const fixture = losslessFixture("One complete source paragraph without named entities.");
  const primary = fauxProvider();
  primary.setResponses([
    fauxAssistantMessage("I failed to call the finalizer."),
    losslessBatchResponse,
  ]);
  const primaryModel = primary.getModel();
  const primaryStream = primary.provider.streamSimple.bind(primary.provider);
  const runtime = {
    model: primaryModel,
    streamFn: primaryStream,
    effort: "high" as const,
    thinkingLevel: "high" as const,
  };

  const result = await runBook({
    ...fixture.options,
    model: primaryModel,
    streamFn: primaryStream,
    maxAttempts: 1,
    runtimeSet: {
      mode: "quality",
      primary: runtime,
      escalation: runtime,
    },
  } as never);

  assert.equal(primary.state.callCount, 2);
  assert.equal(result.status.humanRequiredWindows, 0);
  assert.equal(result.status.completedWindows, result.status.totalWindows);
});

test("fast mode resolves lexical anchors with one framed primary call and leaves high effort for escalation", async () => {
  const fixture = losslessFixture(
    "Loukianos, whom they called Lucian the Scoffer, laughed.\n\nBOOK ONE",
  );
  const primary = fauxProvider();
  const escalation = fauxProvider();
  primary.setResponses([
    (context) => lexicalPreferredFallbackResponse(context, {
      Loukianos: "卢基阿诺斯",
      Lucian: "卢基安",
    }),
    losslessBatchResponse,
    losslessBatchResponse,
  ]);
  const primaryModel = primary.getModel();
  const primaryStream = primary.provider.streamSimple.bind(primary.provider);

  const result = await runBook({
    ...fixture.options,
    model: primaryModel,
    streamFn: primaryStream,
    tinyWindowTokens: 1,
    maxWindowsPerRequest: 1,
    runtimeSet: {
      mode: "fast",
      primary: {
        model: primaryModel,
        streamFn: primaryStream,
        effort: "off",
        thinkingLevel: "off",
      },
      escalation: {
        model: escalation.getModel(),
        streamFn: escalation.provider.streamSimple.bind(escalation.provider),
        effort: "high",
        thinkingLevel: "high",
      },
    },
  } as never);

  assert.equal(primary.state.callCount, 3);
  assert.equal(escalation.state.callCount, 0);
  assert.equal(result.status.completedWindows, 2);
});

test("fast mode escalates only a failed framed lexical call and resumes translation on the primary runtime", async () => {
  const fixture = losslessFixture(
    "Loukianos, whom they called Lucian the Scoffer, laughed.\n\nBOOK ONE",
  );
  const primary = fauxProvider();
  const escalation = fauxProvider();
  primary.setResponses([
    fauxAssistantMessage([], {
      stopReason: "error",
      errorMessage: "503: primary anchor unavailable",
    }),
    losslessBatchResponse,
    losslessBatchResponse,
  ]);
  escalation.setResponses([(context) => lexicalAnchorResponse(context, {
    sourceForms: ["Loukianos", "Lucian"],
    proposedTarget: "卢基阿诺斯",
    evidenceQuote: "Loukianos, whom they called Lucian the Scoffer, laughed.",
  })]);
  const primaryModel = primary.getModel();
  const primaryStream = primary.provider.streamSimple.bind(primary.provider);

  const result = await runBook({
    ...fixture.options,
    model: primaryModel,
    streamFn: primaryStream,
    tinyWindowTokens: 1,
    maxWindowsPerRequest: 1,
    runtimeSet: {
      mode: "fast",
      primary: {
        model: primaryModel,
        streamFn: primaryStream,
        effort: "off",
        thinkingLevel: "off",
      },
      escalation: {
        model: escalation.getModel(),
        streamFn: escalation.provider.streamSimple.bind(escalation.provider),
        effort: "high",
        thinkingLevel: "high",
      },
    },
  } as never);

  assert.equal(primary.state.callCount, 3);
  assert.equal(escalation.state.callCount, 1);
  assert.equal(result.status.completedWindows, 2);
});

test("a framed preferred fallback preserves Korean names when structured anchor calls fail", async () => {
  const fixture = losslessFixture(
    "용천익 당주가 묵향을 만났다. 용천익은 묵향에게 다시 말했다.",
    "ko",
  );
  fixture.faux.setResponses([
    fauxAssistantMessage("The required structured tool call is unavailable."),
    (context) => lexicalPreferredFallbackResponse(context, {
      용천익: "龙天翼",
      묵향: "墨香",
    }),
    (context) => {
      const prompt = userText(context);
      const match = /STABLE TERMS\n\n(\[[\s\S]*?\])\n\nUNRESOLVED ENTITY LINKS/u.exec(prompt);
      assert.ok(match?.[1]);
      const terms = JSON.parse(match[1]) as Array<{
        sourceForm: string;
        target: string;
        policy: string;
        locked: boolean;
      }>;
      assert.deepEqual(
        terms.filter((term) => term.sourceForm === "용천익" || term.sourceForm === "묵향")
          .map((term) => [term.sourceForm, term.target, term.policy, term.locked])
          .sort((left, right) => String(left[0]).localeCompare(String(right[0]))),
        [
          ["묵향", "墨香", "preferred", false],
          ["용천익", "龙天翼", "preferred", false],
        ],
      );
      return losslessBatchResponse(context);
    },
  ]);

  const result = await runBook(fixture.options as never);

  assert.equal(fixture.faux.state.callCount, 3);
  assert.equal(result.status.humanRequiredWindows, 0);
  assert.equal(result.status.completedWindows, result.status.totalWindows);
});

test("lossless runner resumes the same isolated run and promotes the remaining ordinal", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE");
  fixture.faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [fixture.submission.windows[0]] },
  ), { stopReason: "toolUse" })]);
  const first = await runBook({ ...fixture.options, maxWindows: 1 } as never);
  assert.equal(first.status.completedWindows, 1);
  assert.equal(first.status.pendingWindows, 1);

  const resumedProvider = fauxProvider();
  resumedProvider.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [fixture.submission.windows[1]] },
  ), { stopReason: "toolUse" })]);
  const resumed = await runBook({
    ...fixture.options,
    model: resumedProvider.getModel(),
    streamFn: resumedProvider.provider.streamSimple.bind(resumedProvider.provider),
  } as never);
  assert.equal(resumedProvider.state.callCount, 1);
  assert.equal(resumed.processedWindows, 1);
  assert.equal(resumed.status.completedWindows, 2);
  assert.deepEqual(resumed.windows.map((window) => window.status), ["completed", "completed"]);
});

test("lossless resume synchronizes newer book knowledge before the next wave", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE\n\nArchon returned.");
  fixture.faux.setResponses([losslessBatchResponse]);
  await runBook({ ...fixture.options, maxWindows: 1 } as never);

  const editor = new LosslessBookStore(fixture.options.storePath);
  try {
    const sourceVersion = editor.listTranslationRuns()
      .find((run) => run.runId === "run-lossless")!.sourceVersion;
    const editorRunId = "run-catalog-editor";
    const initialSnapshot = createKnowledgeSnapshot(editorRunId, []);
    editor.createTranslationRun({
      runId: editorRunId,
      sourceVersion,
      protocolVersion: "lossless-v5-1",
      modelId: "catalog-editor",
      initialSnapshotId: initialSnapshot.id,
      initialSnapshot,
    });
    const state = editor.knowledgeState(editorRunId);
    editor.commitKnowledgeCommands({
      requestId: "catalog-editor-archon",
      runId: editorRunId,
      expectedGeneration: state.generation,
      expectedSnapshotId: state.snapshotId,
      commands: [{
        type: "upsert",
        objectType: "term",
        normalizedSubject: "archon",
        kind: "lexical_anchor",
        expectedRevision: null,
        expectedScopeRevision: null,
        fieldPatch: {
          sourceForm: "Archon",
          target: "阁下",
          locked: true,
          policy: "locked",
        },
        ownedFields: ["/target", "/locked", "/policy"],
        scope: "book",
        evidence: [],
        origin: "manual",
      }],
    });
  } finally {
    editor.close();
  }

  const resumedProvider = fauxProvider();
  resumedProvider.setResponses([losslessBatchResponse]);
  await runBook({
    ...fixture.options,
    model: resumedProvider.getModel(),
    streamFn: resumedProvider.provider.streamSimple.bind(resumedProvider.provider),
  } as never);

  const reopened = new LosslessBookStore(fixture.options.storePath);
  try {
    const archon = reopened.latestKnowledgeSnapshot("run-lossless").revisions
      .find((revision) => revision.normalizedSubject === "archon");
    assert.equal((archon?.payload as { target?: string })?.target, "阁下");
    assert.equal(
      reopened.knowledgeState("run-lossless").appliedBookGeneration,
      1,
    );
  } finally {
    reopened.close();
  }
});

function commitManualKnowledge(
  storePath: string,
  objectType: "term" | "style",
  normalizedSubject: string,
  kind: string,
  fieldPatch: Record<string, string | boolean>,
): void {
  const store = new LosslessBookStore(storePath);
  try {
    const state = store.knowledgeState("run-lossless");
    store.commitKnowledgeCommands({
      requestId: `manual-${objectType}-${normalizedSubject}`,
      runId: "run-lossless",
      expectedGeneration: state.generation,
      expectedSnapshotId: state.snapshotId,
      commands: [{
        type: "upsert",
        objectType,
        normalizedSubject,
        kind,
        expectedRevision: null,
        expectedScopeRevision: null,
        fieldPatch,
        ownedFields: Object.keys(fieldPatch).map((field) => `/${field}`),
        scope: "book",
        evidence: [],
        origin: "manual",
      }],
    });
  } finally {
    store.close();
  }
}

test("lossless resume preserves a manually locked knowledge term in stable terms", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE\n\narchon greeted the steward.");
  fixture.faux.setResponses([losslessBatchResponse]);
  await runBook({ ...fixture.options, maxWindows: 1 } as never);
  commitManualKnowledge(
    fixture.options.storePath,
    "term",
    "archon",
    "lexical_anchor",
    {
      sourceForm: "Archon",
      canonicalSource: "Archon",
      target: "阁下",
      locked: true,
      policy: "locked",
      note: "人物面前的直接呼告",
    },
  );
  commitManualKnowledge(
    fixture.options.storePath,
    "term",
    "steward",
    "lexical_anchor",
    {
      sourceForm: "steward",
      canonicalSource: "steward",
      target: "总管",
      locked: false,
      policy: "preferred",
      note: "叙述中的默认职业称谓",
    },
  );

  const resumedProvider = fauxProvider();
  let resumedPrompt = "";
  resumedProvider.setResponses([(context) => {
    resumedPrompt = userText(context);
    return losslessBatchResponse(context);
  }]);
  const resumed = await runBook({
    ...fixture.options,
    model: resumedProvider.getModel(),
    streamFn: resumedProvider.provider.streamSimple.bind(resumedProvider.provider),
  } as never);

  assert.equal(resumed.status.completedWindows, resumed.status.totalWindows);
  const termMatch = /STABLE TERMS\n\n(\[[\s\S]*?\])\n\nUNRESOLVED ENTITY LINKS/u.exec(resumedPrompt);
  assert.ok(termMatch?.[1]);
  const terms = JSON.parse(termMatch[1]) as Array<{
    conceptId: string;
    lexemeId: string;
    sourceForm: string;
    canonicalSource: string;
    target: string;
    policy?: string;
    locked: boolean;
    note?: string;
    origin?: string;
  }>;
  const term = terms.find((candidate) => candidate.sourceForm === "Archon");
  assert.ok(term);
  assert.equal(term.conceptId, "user-archon");
  assert.match(term.lexemeId, /^user-/u);
  assert.equal(term.canonicalSource, "Archon");
  assert.equal(term.target, "阁下");
  assert.equal(term.locked, true);
  assert.equal(term.policy, "locked");
  assert.equal(term.note, "人物面前的直接呼告");
  assert.equal(term.origin, "knowledge");
  const preferred = terms.find((candidate) => candidate.sourceForm === "steward");
  assert.ok(preferred);
  assert.equal(preferred.target, "总管");
  assert.equal(preferred.locked, false);
  assert.equal(preferred.policy, "preferred");
  assert.equal(preferred.note, "叙述中的默认职业称谓");
});

test("lossless resume merges persisted style over caller style for the next wave", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE\n\nthe orbit was narrow.");
  fixture.faux.setResponses([losslessBatchResponse]);
  await runBook({
    ...fixture.options,
    maxWindows: 1,
    styleState: { technicalProse: "调用方原始要求" },
  } as never);
  commitManualKnowledge(
    fixture.options.storePath,
    "style",
    "book-style",
    "style_directive",
    { technicalProse: "先交代概念关系，再保持术语精确" },
  );

  const resumedProvider = fauxProvider();
  let resumedPrompt = "";
  resumedProvider.setResponses([(context) => {
    resumedPrompt = userText(context);
    return losslessBatchResponse(context);
  }]);
  const resumed = await runBook({
    ...fixture.options,
    model: resumedProvider.getModel(),
    streamFn: resumedProvider.provider.streamSimple.bind(resumedProvider.provider),
    styleState: { technicalProse: "调用方原始要求" },
  } as never);

  assert.equal(resumed.status.completedWindows, resumed.status.totalWindows);
  assert.match(resumedPrompt, /先交代概念关系，再保持术语精确/u);
  assert.doesNotMatch(resumedPrompt, /调用方原始要求/u);
});

test("lossless resume uses bounded structured style evidence instead of a raw prior tail", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE");
  const longTranslation = "克制样例。";
  const firstWindow = structuredClone(fixture.submission.windows[0]!);
  firstWindow.translations[0]!.text = longTranslation;
  (firstWindow as typeof firstWindow & { styleObservation: unknown }).styleObservation = {
    voiceId: "narrator",
    activeRegister: "冷静克制",
    rhythm: "长句舒展，短句收束",
    continuityNotes: ["不要解释叙述者的保留"],
  };
  fixture.faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [firstWindow] },
  ), { stopReason: "toolUse" })]);
  const first = await runBook({ ...fixture.options, maxWindows: 1 } as never);
  assert.equal(first.status.completedWindows, 1);

  const resumedProvider = fauxProvider();
  resumedProvider.setResponses([(context) => {
    const prompt = userText(context);
    assert.match(prompt, /EFFECTIVE STYLE BY WINDOW/);
    assert.match(prompt, /全书文体宪章/);
    assert.match(prompt, /冷静克制/);
    assert.doesNotMatch(prompt, /PREVIOUS ACTIVE TAIL/);
    return losslessBatchResponse(context);
  }]);
  const resumed = await runBook({
    ...fixture.options,
    model: resumedProvider.getModel(),
    streamFn: resumedProvider.provider.streamSimple.bind(resumedProvider.provider),
  } as never);

  assert.equal(resumed.status.completedWindows, 2);
  const store = new LosslessBookStore(fixture.options.storePath);
  const observations = store.styleObservations("run-lossless");
  store.close();
  assert.equal(observations.length, 2);
  assert.deepEqual(observations.map((item) => item.ordinal), [0, 1]);
});

test("lossless runner hydrates full knowledge history when resuming from a revision-two projection", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE\n\nCHAPTER ONE");
  assert.equal(fixture.submission.windows.length, 3);
  const responseWithMemory = (target: string) => (context: Context) => {
    const prompt = userText(context);
    const match = /WINDOWS\n\n(\[[\s\S]*?\])\n\nSTABLE TERMS/u.exec(prompt);
    assert.ok(match?.[1]);
    const windows = JSON.parse(match[1]) as Array<{
      windowId: string;
      blocks: Array<{ blockId: string }>;
    }>;
    return fauxAssistantMessage(fauxToolCall("finalize_translation_batch", {
      windows: windows.map((window) => ({
        windowId: window.windowId,
        translations: window.blocks.map((block) => ({
          blockId: block.blockId,
          text: `译文-${target}`,
        })),
        notes: [],
        memoryCandidates: [{
          kind: "term",
          subjectForms: ["Alpha"],
          fact: target,
          confidence: 0.9,
        }],
      })),
    }), { stopReason: "toolUse" });
  };
  fixture.faux.setResponses([
    responseWithMemory("甲"),
    responseWithMemory("乙"),
  ]);
  const first = await runBook({
    ...fixture.options,
    maxWindows: 2,
    maxConcurrency: 1,
    maxWindowsPerRequest: 1,
    tinyWindowTokens: 1,
  } as never);
  assert.equal(first.status.completedWindows, 2);
  const afterTwo = new LosslessBookStore(fixture.options.storePath);
  assert.deepEqual(afterTwo.knowledgeRevisions("run-lossless").map((revision) => revision.revision), [1, 2]);
  assert.equal(afterTwo.latestKnowledgeSnapshot("run-lossless").revisions[0]?.revision, 2);
  afterTwo.close();

  const resumedProvider = fauxProvider();
  resumedProvider.setResponses([(context) => {
    const prompt = userText(context);
    const projectionMatch = /KNOWLEDGE SNAPSHOT PROJECTION\n\n(\{[\s\S]*\})\n\nWINDOWS/u.exec(prompt);
    assert.ok(projectionMatch?.[1]);
    const projection = JSON.parse(projectionMatch[1]) as {
      metadata: { total: number; projected: number; omitted: number };
      revisions: Array<{ normalizedSubject: string; revision: number; status: string }>;
    };
    assert.deepEqual(projection.metadata, {
      ...projection.metadata,
      total: 1,
      projected: 1,
      omitted: 0,
    });
    assert.deepEqual(projection.revisions.map((revision) => [
      revision.normalizedSubject,
      revision.revision,
      revision.status,
    ]), [
      ["alpha", 2, "needs_revalidate"],
    ]);
    return responseWithMemory("乙")(context);
  }]);
  const resumed = await runBook({
    ...fixture.options,
    model: resumedProvider.getModel(),
    streamFn: resumedProvider.provider.streamSimple.bind(resumedProvider.provider),
    maxConcurrency: 1,
    maxWindowsPerRequest: 1,
    tinyWindowTokens: 1,
  } as never);
  assert.equal(resumed.status.completedWindows, 3);
  const recovered = new LosslessBookStore(fixture.options.storePath);
  assert.deepEqual(
    recovered.knowledgeRevisions("run-lossless").map((revision) => revision.revision),
    [1, 2, 3],
  );
  recovered.close();
});

test("reverse physical completion still promotes lossless windows in ordinal order", async () => {
  const fixture = losslessFixture("EDGEWOOD\n\nBOOK ONE");
  const completionOrder: number[] = [];
  fixture.faux.setResponses([
    async (context) => {
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 25));
      completionOrder.push(0);
      return losslessBatchResponse(context);
    },
    async (context) => {
      completionOrder.push(1);
      return losslessBatchResponse(context);
    },
  ]);
  const result = await runBook({
    ...fixture.options,
    tinyWindowTokens: 1,
    maxWindowsPerRequest: 1,
    maxConcurrency: 2,
  } as never);

  assert.deepEqual(completionOrder, [1, 0]);
  assert.equal(result.status.completedWindows, 2);
  const database = new DatabaseSync(fixture.options.storePath);
  const promoted = (database.prepare(`
    SELECT payload_json FROM events WHERE kind='window_promoted' ORDER BY sequence
  `).all() as unknown as Array<{ payload_json: string }>).map((row) =>
    (JSON.parse(row.payload_json) as { windowId: string }).windowId);
  database.close();
  assert.deepEqual(promoted, result.windows.map((window) => window.windowId));
  const styleStore = new LosslessBookStore(fixture.options.storePath);
  assert.deepEqual(styleStore.styleObservations("run-lossless").map((item) => item.ordinal), [0, 1]);
  styleStore.close();
});

test("book runner warms up, runs a parallel wave, and resumes without model calls", async () => {
  const directory = mkdtempSync(join(tmpdir(), "v5-book-runner-"));
  const sourceDb = join(directory, "source.db");
  const storePath = join(directory, "state.db");
  createFixture(sourceDb, 4);
  const faux = fauxProvider();
  faux.setResponses(Array.from({ length: 4 }, () => dynamicResponse));
  const first = await runBook({
    dbPath: sourceDb,
    storePath,
    outputDir: join(directory, "output"),
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    windowOptions: { maxBlocks: 1, maxSourceTokens: 100 },
    warmupWindows: 2,
    maxConcurrency: 2,
    maxAttempts: 2,
    hardDeadlineMs: 30_000,
  });
  assert.deepEqual(first.waves.map((wave) => wave.concurrency), [1, 1, 2]);
  assert.equal(first.status.completedWindows, 4);
  assert.equal(first.status.modelCalls, 4);

  const noCalls = fauxProvider();
  noCalls.setResponses([]);
  const resumed = await runBook({
    dbPath: sourceDb,
    storePath,
    outputDir: join(directory, "output"),
    model: noCalls.getModel(),
    streamFn: noCalls.provider.streamSimple.bind(noCalls.provider),
    windowOptions: { maxBlocks: 1, maxSourceTokens: 100 },
    warmupWindows: 2,
    maxConcurrency: 2,
    maxAttempts: 2,
    hardDeadlineMs: 30_000,
  });
  assert.equal(resumed.processedWindows, 0);
  assert.equal(resumed.status.modelCalls, 4);
});

test("book runner retries a no-submit window with a fresh per-window budget", async () => {
  const directory = mkdtempSync(join(tmpdir(), "v5-book-retry-"));
  const sourceDb = join(directory, "source.db");
  const storePath = join(directory, "state.db");
  createFixture(sourceDb, 1);
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage("No translation was submitted."),
    dynamicResponse,
  ]);
  const result = await runBook({
    dbPath: sourceDb,
    storePath,
    outputDir: join(directory, "output"),
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    windowOptions: { maxBlocks: 1, maxSourceTokens: 100 },
    warmupWindows: 0,
    maxConcurrency: 1,
    maxAttempts: 2,
    hardDeadlineMs: 30_000,
  });
  const store = new BookStore(storePath);
  const window = store.window(store.pendingWindows()[0]?.windowId ?? "")
    ?? result.windows[0];
  assert.equal(result.status.completedWindows, 1);
  assert.equal(window?.attemptCount, 2);
  assert.equal(result.status.modelCalls, 2);
  store.close();
});

test("book runner aborts external provider failures without creating a human task", async () => {
  const directory = mkdtempSync(join(tmpdir(), "v5-book-provider-failure-"));
  const sourceDb = join(directory, "source.db");
  const storePath = join(directory, "state.db");
  createFixture(sourceDb, 1);
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage([], {
      stopReason: "error",
      errorMessage: "401: authentication failed for fixture credential",
    }),
  ]);

  await assert.rejects(
    runBook({
      dbPath: sourceDb,
      storePath,
      outputDir: join(directory, "output"),
      model: faux.getModel(),
      streamFn: faux.provider.streamSimple.bind(faux.provider),
      windowOptions: { maxBlocks: 1, maxSourceTokens: 100 },
      maxAttempts: 2,
      hardDeadlineMs: 30_000,
    }),
    /authentication failed/i,
  );
  const store = new BookStore(storePath);
  assert.equal(store.statusSummary().humanRequiredWindows, 0);
  assert.equal(store.statusSummary().pendingWindows, 1);
  store.close();
});
