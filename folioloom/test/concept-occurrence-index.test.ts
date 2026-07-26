import assert from "node:assert/strict";
import test from "node:test";

import { conceptFromAnchor } from "../src/knowledge/lexical-concept.js";
import {
  buildConceptOccurrenceIndex,
} from "../src/knowledge/concept-occurrence-index.js";
import { getSourceLanguageProfile } from "../src/language/profiles.js";
import type { SourceLanguageProfile } from "../src/language/types.js";

test("concept occurrence index scans and segments every block once for a large concept batch", () => {
  const concepts = Array.from({ length: 100 }, (_, index) =>
    conceptFromAnchor({
      sourceForm: `Term${index.toString().padStart(3, "0")}`,
      target: `术语${index}`,
      mode: "stable",
      semanticClass: "technical_term",
      confidence: 0.95,
    }));
  const blocks = Array.from({ length: 25 }, (_, index) => ({
    blockId: `block-${index}`,
    sourceText: [
      `Term${index.toString().padStart(3, "0")}`,
      `Term${(index + 25).toString().padStart(3, "0")}`,
      `Term${(index + 50).toString().padStart(3, "0")}`,
    ].join(" met "),
  }));
  const blockTexts = new Set(blocks.map((block) => block.sourceText));
  const base = getSourceLanguageProfile("en");
  let blockNormalizations = 0;
  let blockSegmentations = 0;
  const profile: SourceLanguageProfile = {
    ...base,
    normalizeSourceForm(text) {
      if (blockTexts.has(text)) blockNormalizations += 1;
      return base.normalizeSourceForm(text);
    },
    segment(text) {
      if (blockTexts.has(text)) blockSegmentations += 1;
      return base.segment(text);
    },
  };

  const occurrences = buildConceptOccurrenceIndex(blocks, concepts, profile);

  assert.equal(blockNormalizations, blocks.length);
  assert.equal(blockSegmentations, blocks.length);
  assert.equal(occurrences.length, 75);
  assert.deepEqual(occurrences[0]?.sourceSpans, [{
    start: 0,
    end: 7,
    sourceForm: "Term000",
  }]);
});

test("concept occurrence index groups repeated exact spans by concept and block", () => {
  const concept = conceptFromAnchor({
    sourceForm: "Prokurist",
    target: "主事",
    mode: "contextual",
    semanticClass: "role",
    confidence: 0.95,
  });
  const occurrences = buildConceptOccurrenceIndex([{
    blockId: "block-role",
    sourceText: "Der Prokurist sprach mit dem Prokurist.",
  }], [concept], getSourceLanguageProfile("de"));

  assert.deepEqual(occurrences, [{
    conceptId: concept.conceptId,
    blockId: "block-role",
    sourceSpans: [{
      start: 4,
      end: 13,
      sourceForm: "Prokurist",
    }, {
      start: 29,
      end: 38,
      sourceForm: "Prokurist",
    }],
  }]);
});
