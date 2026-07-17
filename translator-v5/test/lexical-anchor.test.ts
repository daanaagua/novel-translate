import assert from "node:assert/strict";
import test from "node:test";

import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
} from "@earendil-works/pi-ai";

import {
  collectRepeatedAnchorCandidates,
  LexicalAnchorer,
} from "../src/agents/lexical-anchorer.js";
import { PiRuntime } from "../src/agents/pi-runtime.js";
import type { StableTerm, V4Block } from "../src/domain/types.js";
import { BudgetLedger } from "../src/kernel/budget.js";

function block(text: string): V4Block {
  return {
    id: "block-1",
    legacyId: null,
    chapterId: "chapter",
    chapterTitle: "Fixture",
    globalIndex: 1,
    blockIndex: 0,
    sourceText: text,
    sourceHash: "hash",
    tokenCount: 20,
  };
}

const typhonTerm: StableTerm = {
  conceptId: "typhon",
  lexemeId: "typhon-lexeme",
  sourceForm: "Typhon",
  canonicalSource: "Typhon",
  target: "提丰",
  locked: true,
};

test("candidate extraction keeps repeated unknown titles but removes established terms", () => {
  const candidates = collectRepeatedAnchorCandidates([
    block(
      "The Conciliator faced Typhon. Later Typhon was called the enemy of the Conciliator.",
    ),
  ], [typhonTerm]);

  assert.deepEqual(candidates.map((candidate) => candidate.sourceForm), [
    "Conciliator",
  ]);
  assert.equal(candidates[0]?.contexts.length, 2);
});

test("Pi lexical anchor decisions become run-local stable terms", async () => {
  const candidates = collectRepeatedAnchorCandidates([
    block("The Conciliator spoke. Typhon opposed the Conciliator."),
  ], [typhonTerm]);
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall("submit_lexical_anchors", {
        anchors: [{
          sourceForm: "Conciliator",
          target: "调和者",
          mode: "stable",
          confidence: 0.95,
        }],
      }),
      { stopReason: "toolUse" },
    ),
  ]);

  const outcome = await new LexicalAnchorer(new PiRuntime()).run({
    candidates,
    stableTerms: [typhonTerm],
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(outcome.anchors.length, 1);
  assert.equal(outcome.terms[0]?.sourceForm, "Conciliator");
  assert.equal(outcome.terms[0]?.target, "调和者");
  assert.equal(outcome.terms[0]?.locked, true);
});
