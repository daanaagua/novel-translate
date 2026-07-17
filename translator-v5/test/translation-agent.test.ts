import assert from "node:assert/strict";
import test from "node:test";

import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
} from "@earendil-works/pi-ai";

import { PiRuntime } from "../src/agents/pi-runtime.js";
import {
  splitIntoChapterIslands,
  Translator,
} from "../src/agents/translator.js";
import type { ProvisionalSnapshot } from "../src/domain/provisional-snapshot.js";
import type { V4Block } from "../src/domain/types.js";
import { BudgetLedger } from "../src/kernel/budget.js";
import { CandidateCollector } from "../src/tools/candidate-collector.js";
import { TranslationValidator } from "../src/validators/translation-validator.js";

function chapterBlock(index: number, text: string): V4Block {
  return {
    id: `v06_ch08_00${index}`,
    legacyId: null,
    chapterId: "v06_ch08",
    chapterTitle: "The Face of the Autarch",
    globalIndex: 220 + index,
    blockIndex: index,
    sourceText: text,
    sourceHash: `hash-${index}`,
    tokenCount: Math.ceil(text.length / 4),
  };
}

function snapshot(): ProvisionalSnapshot {
  return {
    schemaVersion: "v5-provisional-1",
    protocolHash: "protocol",
    modelHash: "model",
    targetScope: {
      blockIds: ["v06_ch08_000", "v06_ch08_001"],
      globalIndexes: [220, 221],
    },
    coverage: { completePrefix: false, indexedGlobalIndexes: [200, 220, 221] },
    questions: [{
      questionId: "q-typhon-piaton",
      kind: "entity_relation",
      prompt: "How are Typhon and Piaton related?",
      subjectIds: ["typhon", "piaton"],
      channel: "translator_global",
      impact: "high",
    }],
    narrativeFacts: [],
    translatorFacts: [{
      questionId: "q-typhon-piaton",
      kind: "entity_relation",
      verdict: "shared body with distinct control",
      confidence: 0.9,
      evidenceIds: ["ev-1"],
      channel: "translator_global",
    }],
    unresolved: [],
    evidence: [{
      evidenceId: "ev-1",
      blockId: "block-200",
      globalIndex: 200,
      paragraphIndex: 0,
      sourceHash: "hash-evidence",
      channel: "translator_global",
    }],
    evidenceIds: ["ev-1"],
    sourceHashes: {},
  };
}

test("translation agent receives minimal context and may retrieve evidence", async () => {
  const blocks = [
    chapterBlock(0, "Typhon raised his head and looked at Severian."),
    chapterBlock(1, "Piaton's voice came from the same body."),
  ];
  const island = splitIntoChapterIslands(blocks)[0];
  assert.ok(island);
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall("retrieve_resolved_evidence", {
        questionIds: ["q-typhon-piaton"],
      }),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall("finalize_translation", {
        translations: [
          { blockId: "v06_ch08_000", text: "提丰抬起头，望向塞万里安。" },
          { blockId: "v06_ch08_001", text: "皮亚顿的声音从同一具身体里传来。" },
        ],
        notes: [],
      }),
      { stopReason: "toolUse" },
    ),
  ]);

  const outcome = await new Translator(new PiRuntime()).translateIsland({
    island,
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
    collector: new CandidateCollector(),
    stableTerms: [],
    snapshot: snapshot(),
    styleState: { register: "literary", dialogueQuotes: "curly" },
    previousActiveTail: "",
  });

  assert.deepEqual(outcome.usedResolutionIds, ["q-typhon-piaton"]);
  assert.equal(outcome.initialPrompt.includes("all narrative memories"), false);
  assert.equal(outcome.validation.valid, true);
  assert.equal(outcome.humanRequired, false);
});

test("deterministic validator rejects missing blocks and leaked system JSON", () => {
  const blocks = [
    chapterBlock(0, "Typhon raised his head and looked at Severian."),
    chapterBlock(1, "Piaton's voice came from the same body."),
  ];
  const result = new TranslationValidator().validate(blocks, {
    translations: [{
      blockId: "v06_ch08_000",
      text: '{"systemPrompt":"leaked"}',
    }],
    notes: [],
    repaired: false,
  });

  assert.equal(result.valid, false);
  assert.ok(result.failures.some((failure) => failure.code === "block_set_mismatch"));
  assert.ok(result.failures.some((failure) => failure.code === "system_json_leak"));
});

test("one repair pass can replace an invalid island candidate", async () => {
  const blocks = [
    chapterBlock(0, "Typhon raised his head and looked at Severian."),
    chapterBlock(1, "Piaton's voice came from the same body."),
  ];
  const island = splitIntoChapterIslands(blocks)[0];
  assert.ok(island);
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall("finalize_translation", {
        translations: [
          { blockId: "v06_ch08_000", text: "提丰抬起头。" },
        ],
        notes: [],
      }),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall("submit_repaired_translation", {
        translations: [
          { blockId: "v06_ch08_000", text: "提丰抬起头，望向塞万里安。" },
          { blockId: "v06_ch08_001", text: "皮亚顿的声音从同一具身体里传来。" },
        ],
        notes: ["补回遗漏文本块"],
      }),
      { stopReason: "toolUse" },
    ),
  ]);

  const outcome = await new Translator(new PiRuntime()).translateIsland({
    island,
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
    collector: new CandidateCollector(),
    stableTerms: [],
    snapshot: snapshot(),
    styleState: { register: "literary" },
    previousActiveTail: "",
  });

  assert.equal(outcome.repaired, true);
  assert.equal(outcome.validation.valid, true);
  assert.equal(outcome.humanRequired, false);
  assert.equal(outcome.candidate?.translations.length, 2);
});
