import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { prepareTranslationRequest } from "../src/agents/translation-request.js";
import type { PhysicalRequestPlan } from "../src/fullbook/types.js";
import { getSourceLanguageProfile } from "../src/language/profiles.js";
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
      revisions: [{ id: "memory-1", fact: "A remembered fact." }],
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
  assert.match(prepared.prompt, /KNOWLEDGE SNAPSHOT REVISIONS/u);
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
