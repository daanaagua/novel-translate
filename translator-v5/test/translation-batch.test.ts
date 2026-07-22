import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
  type Context,
} from "@earendil-works/pi-ai";

import {
  runTranslationBatch,
  translationBatchSystemPrompt,
} from "../src/agents/translation-batch.js";
import { prepareTranslationRequest } from "../src/agents/translation-request.js";
import type { PhysicalRequestPlan } from "../src/fullbook/types.js";
import { BudgetLedger } from "../src/kernel/budget.js";
import { getSourceLanguageProfile } from "../src/language/profiles.js";
import type { LosslessBlock } from "../src/source/types.js";
import { createBookStyleConstitution, composeEffectiveStyle } from "../src/style/effective-style.js";
import { projectEffectiveStyle } from "../src/style/style-projection.js";

function block(id: string, index: number, text: string): LosslessBlock {
  return {
    id,
    sourceVersion: "source-v1",
    canonicalStart: index * 10,
    canonicalEnd: index * 10 + text.length,
    sourceText: text,
    sourceHash: createHash("sha256").update(text).digest("hex"),
    globalIndex: index,
    tokenCount: 2,
    structureId: null,
    structureTitle: null,
  };
}

const blocks = [block("block-0", 0, "Alpha."), block("block-1", 1, "Beta.")];
const request: PhysicalRequestPlan = {
  requestId: "request-0",
  sourceTokens: 4,
  windows: blocks.map((item, ordinal) => ({
    windowId: `window-${ordinal}`,
    ordinal,
    chapterId: "chapter-0",
    chapterTitle: null,
    blockIds: [item.id],
    globalIndexes: [ordinal],
    sourceTokens: item.tokenCount,
    sourceChars: item.sourceText.length,
    oversized: false,
  })),
};

function singleWindowRequest(sourceBlocks: readonly LosslessBlock[]): PhysicalRequestPlan {
  return {
    requestId: "request-canonical-id-fixture",
    sourceTokens: sourceBlocks.reduce((total, item) => total + item.tokenCount, 0),
    windows: [{
      windowId: "window-canonical-id-fixture",
      ordinal: 0,
      chapterId: "chapter-0",
      chapterTitle: null,
      blockIds: sourceBlocks.map((item) => item.id),
      globalIndexes: sourceBlocks.map((item) => item.globalIndex),
      sourceTokens: sourceBlocks.reduce((total, item) => total + item.tokenCount, 0),
      sourceChars: sourceBlocks.reduce((total, item) => total + item.sourceText.length, 0),
      oversized: false,
    }],
  };
}

function promptText(context: Context): string {
  const message = context.messages.findLast((item) => item.role === "user");
  assert.ok(message?.role === "user");
  return typeof message.content === "string"
    ? message.content
    : message.content.filter((item) => item.type === "text")
      .map((item) => item.text)
      .join("\n");
}

test("batch protocol states that user style requirements cannot override integrity rules", () => {
  const prompt = translationBatchSystemPrompt(getSourceLanguageProfile("en"));
  assert.match(prompt, /User style requirements may guide Chinese phrasing only/u);
  assert.match(
    prompt,
    /must never override source meaning, ambiguity, stable terminology, block boundaries, validation, or the typed-tool protocol/u,
  );
});

test("batch protocol names Korean sources rather than treating them as undetermined", () => {
  const prompt = translationBatchSystemPrompt(getSourceLanguageProfile("ko"));
  assert.match(prompt, /The source language is Korean \(ko\)/u);
  assert.match(prompt, /Simplified Chinese \(zh-Hans\)/u);
  assert.match(prompt, /Use simplified Chinese characters consistently/u);
});

test("batch runtime uses the same prepared prompt as complete-request budgeting", async () => {
  const prepared = prepareTranslationRequest({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
  });
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: request.windows.map((window) => ({
      windowId: window.windowId,
      translations: window.blockIds.map((blockId) => ({ blockId, text: `translated ${blockId}` })),
      notes: [],
    })) },
  ), { stopReason: "toolUse" })]);

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });
  const prompt = (result.run.messages[0] as {
    content?: Array<{ type: string; text?: string }>;
  }).content?.[0]?.text ?? "";

  assert.equal(prompt, prepared.prompt);
  assert.deepEqual(result.run.toolNames, prepared.tools.map((tool) => tool.name));
});

test("batch isolates one malformed logical window without discarding its valid sibling", async () => {
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [{
      windowId: "window-0",
      translations: [{ blockId: "block-0", text: "阿尔法。" }],
      notes: [],
    }, {
      windowId: "window-1",
      translations: [{ blockId: "block-1", text: "" }],
      notes: [],
    }] },
  ), { stopReason: "toolUse" })]);

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(faux.state.callCount, 1);
  assert.equal(result.windows[0]?.status, "completed");
  assert.equal(result.windows[1]?.status, "failed");
  assert.match(result.windows[1]?.error ?? "", /empty/i);
});

test("batch normalizes every forbidden double-quote glyph before initial validation", async () => {
  const quoteBlocks = [
    block(
      "block-0",
      0,
      "This deliberately long source passage gives the typography regression enough space.",
    ),
    blocks[1]!,
  ];
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [{
      windowId: "window-0",
      translations: [{
        blockId: "block-0",
        text: "他说：\"阿尔法\"。‛乙。”‟丙。”〝丁〞„戊。”",
      }],
      notes: [],
    }, {
      windowId: "window-1",
      translations: [{ blockId: "block-1", text: "贝塔。" }],
      notes: [],
    }] },
  ), { stopReason: "toolUse" })]);

  const result = await runTranslationBatch({
    request,
    blocks: quoteBlocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(faux.state.callCount, 1);
  assert.equal(result.windows[0]?.status, "completed");
  assert.equal(result.windows[0]?.translations[0]?.text, "他说：“阿尔法”。“乙。”“丙。”“丁”“戊。”");
  assert.equal(/["‛‟〝〞„]/u.test(result.windows[0]?.translations[0]?.text ?? ""), false);
});

test("batch normalizes traditional prose before validation and preserves locked targets", async () => {
  const sourceBlocks = [block("block-0", 0, "Dragon completed the training.")];
  const sourceRequest = singleWindowRequest(sourceBlocks);
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [{
      windowId: sourceRequest.windows[0]?.windowId,
      translations: [{ blockId: "block-0", text: "龍完成了黑殺隊的訓練。" }],
      notes: [],
    }] },
  ), { stopReason: "toolUse" })]);

  const result = await runTranslationBatch({
    request: sourceRequest,
    blocks: sourceBlocks,
    stableTerms: [{
      conceptId: "dragon",
      lexemeId: "dragon-lexeme",
      sourceForm: "Dragon",
      canonicalSource: "Dragon",
      target: "龍",
      locked: true,
    }],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(result.windows[0]?.status, "completed");
  assert.equal(result.windows[0]?.translations[0]?.text, "龍完成了黑杀队的训练。");
});

test("batch mechanically corrects one unknown canonical block-id character typo", async () => {
  const expected = block("block-1f85f23a483f9edef746", 0, "Alpha.");
  const mistyped = "block-1f85a23a483f9edef746";
  const canonicalRequest = singleWindowRequest([expected]);
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [{
      windowId: canonicalRequest.windows[0]!.windowId,
      translations: [{ blockId: mistyped, text: "阿尔法。" }],
      notes: [],
    }] },
  ), { stopReason: "toolUse" })]);

  const result = await runTranslationBatch({
    request: canonicalRequest,
    blocks: [expected],
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(faux.state.callCount, 1);
  assert.equal(result.windows[0]?.status, "completed");
  assert.equal(result.windows[0]?.translations[0]?.blockId, expected.id);
  assert.equal(result.windows[0]?.translations[0]?.text, "阿尔法。");
  assert.deepEqual(result.responseErrors, [
    "warning: mechanically corrected opaque blockId in window window-canonical-id-fixture at hex offset 5 (a -> f)",
  ]);
});

test("batch never corrects a submitted identifier that is a real other block", async () => {
  const expected = block("block-1f85f23a483f9edef746", 0, "Alpha.");
  const realOther = block("block-1f85a23a483f9edef746", 1, "Beta.");
  const canonicalRequest = singleWindowRequest([expected]);
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [{
      windowId: canonicalRequest.windows[0]!.windowId,
      translations: [{ blockId: realOther.id, text: "阿尔法。" }],
      notes: [],
    }] },
  ), { stopReason: "toolUse" })]);

  const result = await runTranslationBatch({
    request: canonicalRequest,
    blocks: [expected, realOther],
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(result.windows[0]?.status, "failed");
  assert.match(result.windows[0]?.error ?? "", /unknown blockId/i);
  assert.deepEqual(result.responseErrors, []);
});

test("batch rejects multi-character and ambiguous canonical block-id corrections", async () => {
  const expected = block("block-1f85f23a483f9edef746", 0, "Alpha.");
  const ambiguousOther = block("block-1f85a23a483f9edef747", 1, "Beta.");
  const canonicalRequest = singleWindowRequest([expected]);
  const cases = [
    {
      name: "a same-length non-hex identifier",
      submittedId: "block-1f85f23a483f9edef74g",
      inputBlocks: [expected],
    },
    {
      name: "two character differences",
      submittedId: "block-1f85a23a483f9edef747",
      inputBlocks: [expected],
    },
    {
      name: "two equally near real block identifiers",
      submittedId: "block-1f85a23a483f9edef746",
      inputBlocks: [expected, ambiguousOther],
    },
  ];

  for (const fixture of cases) {
    const faux = fauxProvider();
    faux.setResponses([fauxAssistantMessage(fauxToolCall(
      "finalize_translation_batch",
      { windows: [{
        windowId: canonicalRequest.windows[0]!.windowId,
        translations: [{ blockId: fixture.submittedId, text: "阿尔法。" }],
        notes: [],
      }] },
    ), { stopReason: "toolUse" })]);

    const result = await runTranslationBatch({
      request: canonicalRequest,
      blocks: fixture.inputBlocks,
      stableTerms: [],
      snapshot: { id: "snapshot-0", revisions: [] },
      model: faux.getModel(),
      streamFn: faux.provider.streamSimple.bind(faux.provider),
      budget: new BudgetLedger(),
    });

    assert.equal(result.windows[0]?.status, "failed", fixture.name);
    assert.match(result.windows[0]?.error ?? "", /unknown blockId/i, fixture.name);
    assert.deepEqual(result.responseErrors, [], fixture.name);
  }
});

test("batch prompt includes only the bounded current knowledge projection behind one sentinel", async () => {
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: request.windows.map((window) => ({
      windowId: window.windowId,
      translations: window.blockIds.map((blockId) => ({ blockId, text: `translated ${blockId}` })),
      notes: [],
    })) },
  ), { stopReason: "toolUse" })]);
  const revisions = [{
    revisionId: "knowledge-revision-1",
    kind: "character",
    normalizedSubject: "alpha",
    revision: 1,
    status: "active",
    payload: {
      fact: "Alpha remains the same character in this scene.",
      subjectForms: ["Alpha"],
    },
    alternatives: [{
      fact: "Alpha remains the same character in this scene.",
      subjectForms: ["Alpha"],
    }],
    candidateIds: ["candidate-alpha"],
    sourceWindowIds: ["window-prior"],
  }];

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  const prompt = (result.run.messages[0] as {
    content?: Array<{ type: string; text?: string }>;
  }).content?.[0]?.text ?? "";
  assert.equal(prompt.match(/KNOWLEDGE SNAPSHOT PROJECTION/g)?.length, 1);
  assert.match(prompt, /Alpha remains the same character/u);
  assert.doesNotMatch(prompt, /candidate-alpha|window-prior/u);
});

test("batch rejects unknown and duplicate outer window identities without erasing a valid window", async () => {
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [{
      windowId: "window-0",
      translations: [{ blockId: "block-0", text: "阿尔法。" }],
      notes: [],
    }, {
      windowId: "window-1",
      translations: [{ blockId: "block-1", text: "贝塔。" }],
      notes: [],
    }, {
      windowId: "window-1",
      translations: [{ blockId: "block-1", text: "重复贝塔。" }],
      notes: [],
    }, {
      windowId: "unknown-window",
      translations: [{ blockId: "block-1", text: "未知。" }],
      notes: [],
    }] },
  ), { stopReason: "toolUse" })]);

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(result.windows[0]?.status, "completed");
  assert.match(result.windows[1]?.error ?? "", /duplicate windowId/i);
  assert.deepEqual(result.responseErrors, ["unknown windowId: unknown-window"]);
  assert.deepEqual(result.run.toolNames, ["finalize_translation_batch"]);
});

test("batch reports a missing outer window only as that logical window failure", async () => {
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: [{
      windowId: "window-0",
      translations: [{ blockId: "block-0", text: "阿尔法。" }],
      notes: [],
    }] },
  ), { stopReason: "toolUse" })]);

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });
  assert.equal(result.windows[0]?.status, "completed");
  assert.match(result.windows[1]?.error ?? "", /missing window submission/i);
});

test("batch accepts only the first terminating submission in one model session", async () => {
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage([
    fauxToolCall("finalize_translation_batch", { windows: [{
      windowId: "window-0",
      translations: [{ blockId: "block-0", text: "阿尔法。" }],
      notes: [],
    }, {
      windowId: "window-1",
      translations: [{ blockId: "block-1", text: "贝塔。" }],
      notes: [],
    }] }),
    fauxToolCall("finalize_translation_batch", { windows: [] }),
  ], { stopReason: "toolUse" })]);
  const budget = new BudgetLedger();
  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget,
  });
  assert.deepEqual(result.windows.map((window) => window.status), ["completed", "completed"]);
  assert.ok(result.responseErrors.some((error) => /multiple terminating/i.test(error)));
  assert.equal(budget.snapshot().translationToolCalls, 1);
});

test("batch stops a malformed tool loop after one corrective turn", async () => {
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(fauxToolCall("shell", { command: "ignored" }), { stopReason: "toolUse" }),
    fauxAssistantMessage(fauxToolCall("shell", { command: "still ignored" }), { stopReason: "toolUse" }),
    fauxAssistantMessage(fauxToolCall("finalize_translation_batch", {
      windows: request.windows.map((window) => ({
        windowId: window.windowId,
        translations: window.blockIds.map((blockId) => ({ blockId, text: "不应抵达。" })),
        notes: [],
      })),
    }), { stopReason: "toolUse" }),
  ]);

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(result.run.modelCalls, 2);
  assert.equal(result.repairRuns.length, 0);
  assert.equal(faux.state.callCount, 2);
  assert.equal(result.run.turnLimitReached, true);
  assert.ok(result.windows.every((window) => window.status === "failed"));
});

test("batch projects bounded structured style and returns the same-call style observation", async () => {
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall(
    "finalize_translation_batch",
    { windows: request.windows.map((window) => ({
      windowId: window.windowId,
      translations: window.blockIds.map((blockId) => ({ blockId, text: `译文-${blockId}` })),
      notes: [],
      styleObservation: {
        voiceId: "narrator",
        activeRegister: "冷静克制",
        rhythm: "长短句交替",
        continuityNotes: ["不说明叙述者隐瞒的原因"],
      },
    })) },
  ), { stopReason: "toolUse" })]);
  const constitution = createBookStyleConstitution({ register: "准确、隽永" });
  const projections = Object.fromEntries(request.windows.map((window) => [
    window.windowId,
    projectEffectiveStyle(composeEffectiveStyle({
      constitution,
      voices: [{
        voiceId: "narrator",
        scope: "main_narrator",
        instruction: "克制回顾",
        confidence: 1,
      }],
      observations: [],
      currentOrdinal: window.ordinal,
      sourceText: blocks[window.ordinal]?.sourceText ?? "",
      defaultVoiceId: "narrator",
    })),
  ]));

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    effectiveStyleByWindow: projections,
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  const prompt = (result.run.messages[0] as {
    content?: Array<{ type: string; text?: string }>;
  }).content?.[0]?.text ?? "";
  assert.match(prompt, /EFFECTIVE STYLE BY WINDOW/);
  assert.match(prompt, /全书文体宪章/);
  assert.doesNotMatch(prompt, /PREVIOUS ACTIVE TAIL/);
  assert.equal(result.windows[0]?.styleObservation?.activeRegister, "冷静克制");
});

test("batch validation repairs only the invalid block once and preserves its valid sibling", async () => {
  const faux = fauxProvider();
  const repairPrompts: string[] = [];
  faux.setResponses([
    fauxAssistantMessage(fauxToolCall("finalize_translation_batch", {
      windows: [{
        windowId: "window-0",
        translations: [{ blockId: "block-0", text: "错误译文。" }],
        notes: [],
      }, {
        windowId: "window-1",
        translations: [{ blockId: "block-1", text: "贝塔。" }],
        notes: [],
      }],
    }), { stopReason: "toolUse" }),
    (context) => {
      repairPrompts.push(promptText(context));
      return fauxAssistantMessage(fauxToolCall("submit_repaired_translation", {
        translations: [{ blockId: "block-0", text: "阿尔法。" }],
        notes: [],
      }), { stopReason: "toolUse" });
    },
  ]);

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [{
      conceptId: "alpha",
      lexemeId: "alpha-lexeme",
      sourceForm: "Alpha",
      canonicalSource: "Alpha",
      target: "阿尔法",
      locked: true,
    }],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(faux.state.callCount, 2);
  assert.deepEqual(result.windows.map((window) => window.status), ["completed", "completed"]);
  assert.equal(result.windows[0]?.translations[0]?.text, "阿尔法。");
  assert.equal(result.windows[1]?.translations[0]?.text, "贝塔。");
  assert.match(repairPrompts[0] ?? "", /stable_term_mismatch/);
  assert.match(repairPrompts[0] ?? "", /block-0/);
  assert.doesNotMatch(repairPrompts[0] ?? "", /\[block-1\]/);
});

test("batch normalizes a targeted repair before its final validation", async () => {
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(fauxToolCall("finalize_translation_batch", {
      windows: [{
        windowId: "window-0",
        translations: [{ blockId: "block-0", text: "错误译文。" }],
        notes: [],
      }, {
        windowId: "window-1",
        translations: [{ blockId: "block-1", text: "贝塔。" }],
        notes: [],
      }],
    }), { stopReason: "toolUse" }),
    fauxAssistantMessage(fauxToolCall("submit_repaired_translation", {
      translations: [{ blockId: "block-0", text: "他说：\"阿尔法\"。" }],
      notes: [],
    }), { stopReason: "toolUse" }),
  ]);

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [{
      conceptId: "alpha",
      lexemeId: "alpha-lexeme",
      sourceForm: "Alpha",
      canonicalSource: "Alpha",
      target: "阿尔法",
      locked: true,
    }],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(faux.state.callCount, 2);
  assert.equal(result.windows[0]?.status, "completed");
  assert.equal(result.windows[0]?.translations[0]?.text, "他说：“阿尔法”。");
  assert.equal(/["‛‟〝〞„]/u.test(result.windows[0]?.translations[0]?.text ?? ""), false);
  assert.equal(result.windows[1]?.status, "completed");
});

test("batch uses the explicit escalation runtime only for targeted repair", async () => {
  const primary = fauxProvider();
  const escalation = fauxProvider();
  primary.setResponses([fauxAssistantMessage(fauxToolCall("finalize_translation_batch", {
    windows: [{
      windowId: "window-0",
      translations: [{ blockId: "block-0", text: "错误译文。" }],
      notes: [],
    }, {
      windowId: "window-1",
      translations: [{ blockId: "block-1", text: "贝塔。" }],
      notes: [],
    }],
  }), { stopReason: "toolUse" })]);
  escalation.setResponses([fauxAssistantMessage(fauxToolCall("submit_repaired_translation", {
    translations: [{ blockId: "block-0", text: "阿尔法。" }],
    notes: [],
  }), { stopReason: "toolUse" })]);

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [{
      conceptId: "alpha",
      lexemeId: "alpha-lexeme",
      sourceForm: "Alpha",
      canonicalSource: "Alpha",
      target: "阿尔法",
      locked: true,
    }],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: primary.getModel(),
    streamFn: primary.provider.streamSimple.bind(primary.provider),
    thinkingLevel: "off",
    repairRuntime: {
      model: escalation.getModel(),
      streamFn: escalation.provider.streamSimple.bind(escalation.provider),
      thinkingLevel: "high",
    },
    budget: new BudgetLedger(),
  } as never);

  assert.equal(primary.state.callCount, 1);
  assert.equal(escalation.state.callCount, 1);
  assert.equal(result.windows[0]?.translations[0]?.text, "阿尔法。");
});

test("one invalid repair fails only that logical window and never triggers a second repair", async () => {
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(fauxToolCall("finalize_translation_batch", {
      windows: [{
        windowId: "window-0",
        translations: [{ blockId: "block-0", text: "错误译文。" }],
        notes: [],
      }, {
        windowId: "window-1",
        translations: [{ blockId: "block-1", text: "贝塔。" }],
        notes: [],
      }],
    }), { stopReason: "toolUse" }),
    fauxAssistantMessage(fauxToolCall("submit_repaired_translation", {
      translations: [{ blockId: "block-0", text: "仍然错误。" }],
      notes: [],
    }), { stopReason: "toolUse" }),
  ]);

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [{
      conceptId: "alpha",
      lexemeId: "alpha-lexeme",
      sourceForm: "Alpha",
      canonicalSource: "Alpha",
      target: "阿尔法",
      locked: true,
    }],
    snapshot: { id: "snapshot-0", revisions: [] },
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(faux.state.callCount, 2);
  assert.equal(result.windows[0]?.status, "failed");
  assert.match(result.windows[0]?.error ?? "", /stable_term_mismatch/);
  assert.equal(result.windows[1]?.status, "completed");
});

test("framed text batch parses raw assistant prose without a tool call", async () => {
  const prepared = prepareTranslationRequest({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    responseProtocol: "framed_text",
  });
  const frames = prepared.framedProtocol?.frames ?? [];
  assert.equal(frames.length, 2);
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage([
    frames[0]!.beginLine,
    "阿尔法。",
    frames[0]!.endLine,
    frames[1]!.beginLine,
    "贝塔。",
    frames[1]!.endLine,
  ].join("\n"))]);
  const budget = new BudgetLedger();

  const result = await runTranslationBatch({
    request,
    blocks,
    stableTerms: [],
    snapshot: { id: "snapshot-0", revisions: [] },
    responseProtocol: "framed_text",
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget,
  });

  assert.equal(faux.state.callCount, 1);
  assert.deepEqual(result.run.toolNames, []);
  assert.equal(result.windows[0]?.translations[0]?.text, "阿尔法。");
  assert.equal(result.windows[1]?.translations[0]?.text, "贝塔。");
  assert.equal(budget.snapshot().translationToolCalls, 1);
});

test("Japanese mixed kanji-kana residue triggers one targeted repair", async () => {
  const sourceBlocks = [block("block-0", 0, "背後から拝み討ちを放った。")];
  const sourceRequest = singleWindowRequest(sourceBlocks);
  const prepared = prepareTranslationRequest({
    request: sourceRequest,
    blocks: sourceBlocks,
    stableTerms: [],
    snapshot: { id: "snapshot-ja", revisions: [] },
    sourceLanguageProfile: getSourceLanguageProfile("ja"),
    responseProtocol: "framed_text",
  });
  const frame = prepared.framedProtocol?.frames[0];
  assert.ok(frame);
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage([
      frame.beginLine,
      "他从背后使出一记拜み讨ち。",
      frame.endLine,
    ].join("\n")),
    fauxAssistantMessage(fauxToolCall("submit_repaired_translation", {
      translations: [{ blockId: "block-0", text: "他从背后使出一记拜式斩击。" }],
      notes: [],
    }), { stopReason: "toolUse" }),
  ]);

  const result = await runTranslationBatch({
    request: sourceRequest,
    blocks: sourceBlocks,
    stableTerms: [],
    snapshot: { id: "snapshot-ja", revisions: [] },
    sourceLanguageProfile: getSourceLanguageProfile("ja"),
    responseProtocol: "framed_text",
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(faux.state.callCount, 2);
  assert.equal(result.windows[0]?.status, "completed");
  assert.equal(result.windows[0]?.translations[0]?.text, "他从背后使出一记拜式斩击。");
});
