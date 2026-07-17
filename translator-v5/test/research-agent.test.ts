import assert from "node:assert/strict";
import test from "node:test";

import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
} from "@earendil-works/pi-ai";

import { EvidenceResolver } from "../src/agents/evidence-resolver.js";
import { PiRuntime } from "../src/agents/pi-runtime.js";
import { QuestionScout } from "../src/agents/question-scout.js";
import type { V4Block } from "../src/domain/types.js";
import { EvidenceIndex } from "../src/index/evidence-index.js";
import { BudgetLedger } from "../src/kernel/budget.js";
import { CandidateCollector } from "../src/tools/candidate-collector.js";
import { ResearchTools } from "../src/tools/research-tools.js";

function sourceBlock(globalIndex: number, sourceText: string): V4Block {
  return {
    id: `block-${globalIndex}`,
    legacyId: null,
    chapterId: "typhon",
    chapterTitle: "Typhon",
    globalIndex,
    blockIndex: 0,
    sourceText,
    sourceHash: `hash-${globalIndex}`,
    tokenCount: Math.ceil(sourceText.length / 4),
  };
}

test("research agent refines Typhon/Piaton evidence without prefix scanning", async () => {
  const evidenceBlock = sourceBlock(
    10,
    "Typhon and Piaton shared one body. The head spoke with Piaton's voice.",
  );
  const targetBlock = sourceBlock(
    20,
    "Typhon raised his head and addressed Severian.",
  );
  const index = EvidenceIndex.fromBlocks([evidenceBlock, targetBlock]);
  const collector = new CandidateCollector();
  const budget = new BudgetLedger();
  const subjects = [
    { subjectId: "typhon", forms: ["Typhon"] },
    { subjectId: "piaton", forms: ["Piaton"] },
  ];
  const tools = new ResearchTools({
    budget,
    evidenceIndex: index,
    targetGlobalIndex: 20,
    targetBlockIndexes: [20],
    subjects,
    collector,
  });
  const scout = new QuestionScout({
    targetBlocks: [targetBlock],
    subjects,
    mandatoryQuestions: [],
  });
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall("submit_questions", {
        questions: [{
          questionId: "q-typhon-piaton",
          kind: "entity_relation",
          prompt: "How do Typhon and Piaton relate within the speaking body?",
          subjectIds: ["typhon", "piaton"],
          channel: "translator_global",
          impact: "high",
          mandatory: false,
        }],
      }),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall("search_cooccurrence", {
        subjectIds: ["typhon", "piaton"],
        cues: ["body", "head", "voice"],
        channel: "translator_global",
        limit: 4,
      }),
      { stopReason: "toolUse" },
    ),
    (context) => {
      const toolResult = context.messages.findLast(
        (message) => message.role === "toolResult",
      );
      assert.ok(toolResult && toolResult.role === "toolResult");
      const text = toolResult.content.find((item) => item.type === "text")?.text;
      assert.ok(text);
      const payload = JSON.parse(text) as {
        hits: Array<{ evidenceId: string }>;
      };
      assert.ok(payload.hits[0]?.evidenceId);
      return fauxAssistantMessage([
        fauxToolCall("submit_resolution", {
          questionId: "q-typhon-piaton",
          verdict: "shared body with distinct control and voice",
          confidence: 0.9,
          evidenceIds: [payload.hits[0].evidenceId],
          unresolved: "",
        }),
        fauxToolCall("finish_research", { unresolvedQuestionIds: [] }),
      ], { stopReason: "toolUse" });
    },
  ]);

  try {
    const outcome = await new EvidenceResolver(new PiRuntime()).run({
      scout,
      tools,
      collector,
      budget,
      model: faux.getModel(),
      streamFn: faux.provider.streamSimple.bind(faux.provider),
      targetBlocks: [targetBlock],
      protocolVersion: "v5-pilot-test",
    });

    assert.equal(outcome.snapshot.coverage.completePrefix, false);
    assert.ok(outcome.snapshot.evidenceIds.length > 0);
    assert.equal(outcome.snapshot.narrativeFacts.length, 0);
    assert.equal(outcome.snapshot.translatorFacts.length, 1);
    assert.equal(outcome.snapshot.unresolved.length, 0);
    assert.ok(outcome.metrics.offTargetEvidenceChars <= 12_000);
    assert.equal(outcome.run.modelCalls, 3);
    assert.deepEqual(outcome.run.toolNames, [
      "submit_questions",
      "search_cooccurrence",
      "submit_resolution",
      "finish_research",
    ]);
  } finally {
    index.close();
  }
});

test("mandatory questions survive scout submission and become unresolved when unanswered", async () => {
  const targetBlock = sourceBlock(20, "Typhon raised his head.");
  const index = EvidenceIndex.fromBlocks([targetBlock]);
  const collector = new CandidateCollector();
  const budget = new BudgetLedger();
  const subjects = [{ subjectId: "typhon", forms: ["Typhon"] }];
  const mandatory = QuestionScout.forcedQuestionsForUnresolvedNames(subjects);
  const scout = new QuestionScout({
    targetBlocks: [targetBlock],
    subjects,
    mandatoryQuestions: mandatory,
  });
  const tools = new ResearchTools({
    budget,
    evidenceIndex: index,
    targetGlobalIndex: 20,
    subjects,
    questions: scout.mandatoryQuestions(),
    collector,
  });
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall("submit_questions", { questions: [] }),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall("finish_research", {
        unresolvedQuestionIds: [mandatory[0]?.questionId],
      }),
      { stopReason: "toolUse" },
    ),
  ]);
  try {
    const outcome = await new EvidenceResolver(new PiRuntime()).run({
      scout,
      tools,
      collector,
      budget,
      model: faux.getModel(),
      streamFn: faux.provider.streamSimple.bind(faux.provider),
      targetBlocks: [targetBlock],
      protocolVersion: "v5-pilot-test",
    });
    assert.equal(outcome.snapshot.unresolved.length, 1);
    assert.equal(outcome.snapshot.unresolved[0]?.mandatory, true);
    assert.equal(
      outcome.snapshot.unresolved[0]?.questionId,
      mandatory[0]?.questionId,
    );
  } finally {
    index.close();
  }
});
