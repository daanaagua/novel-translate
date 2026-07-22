import assert from "node:assert/strict";
import test from "node:test";

import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
} from "@earendil-works/pi-ai";

import {
  collectRepeatedAnchorCandidates,
  collectWindowAnchorCandidates,
  LexicalAnchorer,
} from "../src/agents/lexical-anchorer.js";
import { PiRuntime } from "../src/agents/pi-runtime.js";
import type { StableTerm, V4Block } from "../src/domain/types.js";
import { BudgetLedger } from "../src/kernel/budget.js";
import { getSourceLanguageProfile } from "../src/language/profiles.js";

function block(text: string, index = 1): V4Block {
  return {
    id: `block-${index}`,
    legacyId: null,
    chapterId: "chapter",
    chapterTitle: "Fixture",
    globalIndex: index,
    blockIndex: index,
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

test("window anchor candidates use whole-book concordance without reconsidering contextual decisions", () => {
  const target = [block("Smoky arrived at Edgewood.", 0)];
  const corpus = [
    ...target,
    block("Later Smoky returned to Edgewood.", 1),
    block("Edgewood remained on no map.", 2),
  ];
  const candidates = collectWindowAnchorCandidates(target, corpus, [], ["Edgewood"]);
  assert.deepEqual(candidates.map((item) => item.sourceForm), ["Smoky"]);
  assert.equal(candidates[0]?.contexts.length, 2);
});

test("window anchor candidates delegate first occurrences to the source language profile", () => {
  const target = [block("Loukianos regarda Lucian.", 0)];
  const candidates = collectWindowAnchorCandidates(
    target,
    target,
    [],
    [],
    getSourceLanguageProfile("fr"),
  );
  assert.deepEqual(candidates.map((item) => item.sourceForm), ["Loukianos", "Lucian"]);
});

test("window anchor candidates use the Korean profile instead of falling back to undetermined text", () => {
  const source = "\ub77c\uc628 \uc7a5\uad70\uc740 \uc131\uc73c\ub85c \ud5a5\ud588\ub2e4. \ub77c\uc628 \uc7a5\uad70\uc740 \uc131\uc8fc\ub97c \ub9cc\ub0ac\ub2e4.";
  const candidates = collectWindowAnchorCandidates(
    [block(source, 0)],
    [block(source, 0)],
    [],
    [],
    getSourceLanguageProfile("ko"),
  );

  assert.ok(candidates.some((candidate) => candidate.sourceForm === "\ub77c\uc628"));
  assert.ok(candidates.length <= 24);
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

test("Pi lexical anchor evidence can confirm two surface forms as one entity", async () => {
  const candidates = collectWindowAnchorCandidates(
    [block("Loukianos, whom they called Lucian the Scoffer, laughed.")],
    [block("Loukianos, whom they called Lucian the Scoffer, laughed.")],
    [],
  );
  const faux = fauxProvider();
  faux.setResponses([fauxAssistantMessage(fauxToolCall("submit_lexical_anchors", {
    anchors: candidates.map((candidate) => ({
      sourceForm: candidate.sourceForm,
      target: "卢奇安",
      mode: "stable",
      confidence: 0.95,
    })),
    entityLinks: [{
      sourceForms: ["Loukianos", "Lucian"],
      proposedTarget: "卢奇安",
      evidenceKind: "explicit_naming",
      evidenceQuote: "Loukianos, whom they called Lucian the Scoffer, laughed.",
      confidence: 0.95,
    }],
  }), { stopReason: "toolUse" })]);

  const outcome = await new LexicalAnchorer(new PiRuntime()).run({
    candidates,
    stableTerms: [],
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(outcome.entityLinks[0]?.status, "confirmed");
  assert.equal(new Set(outcome.terms
    .filter((term) => ["Loukianos", "Lucian"].includes(term.sourceForm))
    .map((term) => term.conceptId)).size, 1);
});

test("entity aliases project explanatory phrases to a concise locked target", async () => {
  const source = "Loukianos, who was also known as Lucian the Scoffer, wrote.";
  const candidates = collectWindowAnchorCandidates([block(source)], [block(source)], []);
  const submission = (proposedTarget: string) => ({
    anchors: candidates.map((candidate) => ({
      sourceForm: candidate.sourceForm,
      target: candidate.sourceForm === "Scoffer" ? "嘲弄者" : "卢奇安",
      mode: "stable" as const,
      confidence: 0.95,
    })),
    entityLinks: [{
      sourceForms: ["Loukianos", "Lucian"],
      proposedTarget,
      evidenceKind: "explicit_naming" as const,
      evidenceQuote: source,
      confidence: 0.95,
    }],
  });
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(fauxToolCall(
      "submit_lexical_anchors",
      submission("卢奇安（嘲弄者卢奇安）"),
    ), { stopReason: "toolUse" }),
    fauxAssistantMessage(fauxToolCall(
      "submit_lexical_anchors",
      submission("卢奇安"),
    ), { stopReason: "toolUse" }),
  ]);

  const outcome = await new LexicalAnchorer(new PiRuntime()).run({
    candidates,
    stableTerms: [],
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    budget: new BudgetLedger(),
  });

  assert.equal(faux.state.callCount, 1);
  assert.equal(outcome.entityLinks[0]?.preferredTarget, "卢奇安");
  assert.ok(outcome.terms
    .filter((term) => ["Loukianos", "Lucian"].includes(term.sourceForm))
    .every((term) => term.target === "卢奇安"));
});
