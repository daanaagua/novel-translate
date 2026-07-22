import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { prepareTranslationRequest } from "../src/agents/translation-request.js";
import {
  DEFAULT_TRANSLATION_KNOWLEDGE_MAX_BYTES,
  DEFAULT_TRANSLATION_KNOWLEDGE_MAX_ENTRIES,
  projectKnowledgeForTranslation,
} from "../src/knowledge/translation-knowledge-projection.js";
import type { PhysicalRequestPlan } from "../src/fullbook/types.js";
import { RequestBudgeter } from "../src/fullbook/request-budgeter.js";
import { canonicalJson } from "../src/knowledge/knowledge-store.js";
import { getSourceLanguageProfile } from "../src/language/profiles.js";
import { WeightedTokenEstimator } from "../src/source/token-estimator.js";
import type { LosslessBlock } from "../src/source/types.js";

function block(id: string, index: number, sourceText: string): LosslessBlock {
  return {
    id,
    sourceVersion: "source-v1",
    canonicalStart: index * 100,
    canonicalEnd: index * 100 + sourceText.length,
    sourceText,
    sourceHash: createHash("sha256").update(sourceText).digest("hex"),
    globalIndex: index,
    tokenCount: 10,
    structureId: null,
    structureTitle: null,
  };
}

function fixture() {
  const blocks = [
    block("block-0", 0, "Opening source paragraph."),
    block("block-1", 1, "Closing source paragraph."),
  ];
  const request: PhysicalRequestPlan = {
    requestId: "request-fixture",
    sourceTokens: 20,
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
  return {
    request,
    blocks,
    stableTerms: [{
      conceptId: "archon",
      lexemeId: "archon-lexeme",
      sourceForm: "Archon",
      canonicalSource: "Archon",
      target: "执政官",
      locked: true,
    }],
    snapshot: {
      id: "snapshot-fixture",
      revisions: [],
    },
    sourceLanguageProfile: getSourceLanguageProfile("en"),
    entityLinkWarnings: ["Alias evidence remains unresolved."],
    effectiveStyleByWindow: {
      "window-0": { text: "literary restraint" },
      "window-1": { text: "dialogue remains spare" },
    } as never,
  };
}

test("one request builder serializes all translator-visible projections and one tool schema", () => {
  const prepared = prepareTranslationRequest(fixture());

  assert.match(prepared.systemPrompt, /Translate the complete source text/u);
  assert.match(prepared.systemPrompt, /Simplified Chinese \(zh-Hans\)/u);
  assert.match(prepared.prompt, /KNOWLEDGE SNAPSHOT PROJECTION/u);
  assert.match(prepared.prompt, /Opening source paragraph/u);
  assert.match(prepared.prompt, /STABLE TERMS/u);
  assert.match(prepared.prompt, /UNRESOLVED ENTITY LINKS/u);
  assert.match(prepared.prompt, /EFFECTIVE STYLE BY WINDOW/u);
  assert.deepEqual(prepared.sections.map((section) => section.kind), [
    "request",
    "memory",
    "source",
    "terms",
    "style",
    "protocol",
  ]);
  assert.deepEqual(prepared.tools.map((tool) => tool.name), ["finalize_translation_batch"]);
  assert.match(prepared.serializedToolSchemas, /finalize_translation_batch/u);
  assert.match(prepared.serializedToolSchemas, /styleObservation/u);
  const requestSection = prepared.sections.find((section) => section.kind === "request");
  assert.equal(
    (requestSection?.jsonPayload as { targetLanguage?: string } | undefined)?.targetLanguage,
    "zh-Hans",
  );
});

test("framed text requests expose exact nonce markers and no translation tool schema", () => {
  const prepared = prepareTranslationRequest({
    ...fixture(),
    responseProtocol: "framed_text",
  });

  assert.deepEqual(prepared.tools, []);
  assert.equal(prepared.serializedToolSchemas, "[]");
  assert.ok(prepared.framedProtocol);
  assert.match(prepared.systemPrompt, /request-specific framed text protocol/u);
  assert.match(prepared.prompt, /Return no prose, Markdown, or code fences outside those frames/u);
  for (const frame of prepared.framedProtocol?.frames ?? []) {
    assert.match(prepared.prompt, new RegExp(frame.beginLine.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&"), "u"));
    assert.match(prepared.prompt, new RegExp(frame.endLine.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&"), "u"));
  }
});

test("model-visible source converts extraction scene markers without changing lossless blocks", () => {
  const base = fixture();
  const sourceBlocks = [block("block-layout", 0, "前场[[]]后场[[]]")];
  const request: PhysicalRequestPlan = {
    requestId: "request-layout",
    sourceTokens: 10,
    windows: [{
      windowId: "window-layout",
      ordinal: 0,
      chapterId: "chapter-0",
      chapterTitle: null,
      blockIds: ["block-layout"],
      globalIndexes: [0],
      sourceTokens: 10,
      sourceChars: sourceBlocks[0]!.sourceText.length,
      oversized: false,
    }],
  };
  const prepared = prepareTranslationRequest({ ...base, blocks: sourceBlocks, request });

  assert.doesNotMatch(prepared.prompt, /\[\[\]\]/u);
  assert.match(prepared.prompt, /前场\\n\\n后场/u);
  assert.equal(sourceBlocks[0]?.sourceText, "前场[[]]后场[[]]");
});

function revision(
  revisionId: string,
  normalizedSubject: string,
  kind: string,
  status: "active" | "needs_revalidate" | "contextual",
  fact: string,
  subjectForms: readonly string[] = [normalizedSubject],
) {
  const payload = { fact, subjectForms: [...subjectForms] };
  return {
    revisionId,
    revision: 1,
    normalizedSubject,
    kind,
    payload,
    alternatives: [payload],
    status,
    candidateIds: [`candidate-${revisionId}`],
    sourceWindowIds: ["window-prior"],
  };
}

test("translator wire knowledge projects matching entity facts, bounded global fallbacks, and no unrelated facts", () => {
  const sourceLanguageProfile = getSourceLanguageProfile("en");
  const base = fixture();
  const blocks = [
    block("block-0", 0, "Piaton stirred beneath Typhon's second head."),
    block("block-1", 1, "Typhon watched the traveler in silence."),
  ];
  const revisions = [
    revision(
      "r-piaton",
      "piaton",
      "entity_identity",
      "active",
      "Piaton controls the body's heart despite being unable to speak freely.",
      ["Piaton"],
    ),
    revision(
      "r-typhon",
      "typhon",
      "entity_relation",
      "needs_revalidate",
      "Typhon and Piaton may be separate minds sharing one body.",
      ["Typhon", "Piaton"],
    ),
    revision(
      "r-unrelated",
      "unrelated",
      "entity_identity",
      "active",
      "UNRELATED_FACT_MUST_NOT_REACH_THE_TRANSLATOR.",
      ["Elsewhere"],
    ),
    revision(
      "r-contextual",
      "contextual-person",
      "local_continuity",
      "contextual",
      "CONTEXTUAL_FACT_MUST_NOT_REACH_THE_TRANSLATOR.",
      ["AbsentPerson"],
    ),
    revision(
      "r-anchor",
      "archon",
      "lexical_anchor",
      "active",
      "Archon is an established office title.",
      ["Archon"],
    ),
  ];
  const originalRevisions = structuredClone(revisions);

  const projected = projectKnowledgeForTranslation(
    revisions,
    blocks.map((item) => item.sourceText),
    sourceLanguageProfile,
  );
  assert.deepEqual(projected.metadata, {
    total: 5,
    projected: 3,
    omitted: 2,
    maxEntries: DEFAULT_TRANSLATION_KNOWLEDGE_MAX_ENTRIES,
    maxSerializedBytes: DEFAULT_TRANSLATION_KNOWLEDGE_MAX_BYTES,
    serializedBytes: Buffer.byteLength(canonicalJson(projected), "utf8"),
  });
  assert.deepEqual(
    projected.revisions.map((item) => [item.normalizedSubject, item.scope]),
    [
      ["typhon", "source_matched"],
      ["piaton", "source_matched"],
      ["archon", "global_fallback"],
    ],
  );

  const prepared = prepareTranslationRequest({
    ...base,
    blocks,
    snapshot: { id: "snapshot-piaton", revisions },
    sourceLanguageProfile,
  });
  const memory = prepared.sections.find((section) => section.kind === "memory");
  assert.deepEqual(memory?.jsonPayload, projected);
  assert.match(prepared.prompt, /Piaton controls the body's heart/u);
  assert.doesNotMatch(prepared.prompt, /UNRELATED_FACT_MUST_NOT_REACH_THE_TRANSLATOR/u);
  assert.doesNotMatch(prepared.prompt, /CONTEXTUAL_FACT_MUST_NOT_REACH_THE_TRANSLATOR/u);
  assert.match(prepared.prompt, /KNOWLEDGE SNAPSHOT snapshot-piaton/u);
  assert.deepEqual(revisions, originalRevisions);
});

test("translator wire knowledge is entry- and canonical-byte-bounded for thousand-scale snapshots", () => {
  const sourceLanguageProfile = getSourceLanguageProfile("en");
  const sourceBlocks = [block("block-0", 0, "Piaton returned to the chamber.")];
  const request: PhysicalRequestPlan = {
    requestId: "request-large-knowledge",
    sourceTokens: 10,
    windows: [{
      windowId: "window-large-knowledge",
      ordinal: 0,
      chapterId: "chapter-0",
      chapterTitle: null,
      blockIds: ["block-0"],
      globalIndexes: [0],
      sourceTokens: 10,
      sourceChars: sourceBlocks[0]!.sourceText.length,
      oversized: false,
    }],
  };

  for (const total of [1_000, 5_900]) {
    const revisions = Array.from({ length: total }, (_, index) => revision(
      `r-${index.toString().padStart(5, "0")}`,
      `piaton-${index}`,
      "entity_identity",
      "active",
      `Piaton fact ${index}: a bounded fact that remains relevant to this request.`,
      ["Piaton"],
    ));
    const projected = projectKnowledgeForTranslation(
      revisions,
      sourceBlocks.map((item) => item.sourceText),
      sourceLanguageProfile,
    );
    assert.equal(projected.metadata.total, total);
    assert.equal(projected.metadata.projected, projected.revisions.length);
    assert.equal(projected.metadata.omitted, total - projected.revisions.length);
    assert.ok(projected.revisions.length <= DEFAULT_TRANSLATION_KNOWLEDGE_MAX_ENTRIES);
    assert.ok(projected.metadata.serializedBytes <= DEFAULT_TRANSLATION_KNOWLEDGE_MAX_BYTES);

    const assessment = new RequestBudgeter(new WeightedTokenEstimator(), {
      modelId: "deepseek-v4-flash",
      contextWindowTokens: 128_000,
      outputTokens: 8_000,
      reasoningReserveTokens: 0,
      safetyMarginTokens: 2_560,
    }).assess({
      request,
      blocks: sourceBlocks,
      stableTerms: [],
      snapshot: { id: `snapshot-${total}`, revisions },
      sourceLanguageProfile,
    });
    assert.equal(assessment.fits, true);
    assert.ok(assessment.totalReserved < 128_000);
  }
});

test("the prepared finalizer delegates execution without changing its serialized schema", async () => {
  let received: unknown;
  const prepared = prepareTranslationRequest(fixture(), {
    onFinalize: async (args) => {
      received = structuredClone(args);
      return { accepted: true };
    },
  });
  const payload = { windows: [{ windowId: "window-0", translations: [], notes: [] }] };
  const result = await prepared.tools[0]?.execute(payload, new AbortController().signal);

  assert.deepEqual(received, payload);
  assert.deepEqual(result, { accepted: true });
  assert.match(prepared.serializedToolSchemas, /finalize_translation_batch/u);
});
