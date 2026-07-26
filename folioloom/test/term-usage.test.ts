import assert from "node:assert/strict";
import test from "node:test";

import { conceptFromAnchor } from "../src/knowledge/lexical-concept.js";
import {
  expectedTermOccurrences,
  validateTermUsages,
  type TermUsageSubmission,
} from "../src/knowledge/term-usage.js";
import { getSourceLanguageProfile } from "../src/language/profiles.js";

const prokurist = conceptFromAnchor({
  sourceForm: "Prokurist",
  target: "主事",
  mode: "contextual",
  semanticClass: "role",
  confidence: 0.95,
});

const blocks = [{
  id: "block-0",
  sourceText: "Der Prokurist kam. Der Prokurist sprach.",
}, {
  id: "block-1",
  sourceText: "Gregor antwortete dem Prokurist.",
}];

function submission(
  occurrence: ReturnType<typeof expectedTermOccurrences>[number],
  targetSurface = "主事",
): TermUsageSubmission {
  return {
    occurrenceId: occurrence.occurrenceId,
    blockId: occurrence.blockId,
    conceptId: occurrence.conceptId,
    sourceForm: occurrence.sourceForm,
    sourceStart: occurrence.sourceStart,
    sourceEnd: occurrence.sourceEnd,
    discourseRole: "narrative",
    targetSurface,
  };
}

test("term usage rejects a disallowed target without hiding it behind missing receipts", () => {
  const expected = expectedTermOccurrences(
    blocks,
    [prokurist],
    getSourceLanguageProfile("de"),
  );
  assert.equal(expected.length, 3);

  assert.deepEqual(validateTermUsages(expected, [
    submission(expected[0]!, "秘书主任"),
  ], new Map([
    ["block-0", "秘书主任来了。他随后开口。"],
    ["block-1", "格里高尔作了回答。"],
  ])), [{
    code: "TERM_USAGE_TARGET_NOT_ALLOWED",
    occurrenceId: expected[0]!.occurrenceId,
  }]);
});

test("term usage accepts an allowed base realization and a short contextual surface", () => {
  const expected = expectedTermOccurrences(
    blocks,
    [prokurist],
    getSourceLanguageProfile("de"),
  );
  const exact = expected.map((occurrence) => submission(occurrence));
  assert.deepEqual(validateTermUsages(expected, exact, new Map([
    ["block-0", "主事来了。主事随后开口。"],
    ["block-1", "格里高尔回答了主事。"],
  ])), []);

  const contextual = expected.map((occurrence) =>
    submission(occurrence, "主事先生"));
  assert.deepEqual(validateTermUsages(expected, contextual, new Map([
    ["block-0", "主事先生来了。主事先生随后开口。"],
    ["block-1", "格里高尔回答了主事先生。"],
  ])), []);
});

test("term usage rejects forged offsets and source forms", () => {
  const expected = expectedTermOccurrences(
    blocks,
    [prokurist],
    getSourceLanguageProfile("de"),
  );
  assert.deepEqual(validateTermUsages(expected, [{
    ...submission(expected[0]!),
    sourceStart: expected[0]!.sourceStart + 1,
  }], new Map([["block-0", "主事来了。"]])), [{
    code: "TERM_USAGE_SOURCE_MISMATCH",
    occurrenceId: expected[0]!.occurrenceId,
  }]);
  assert.deepEqual(validateTermUsages(expected, [{
    ...submission(expected[0]!),
    sourceForm: "Direktor",
  }], new Map([["block-0", "主事来了。"]])), [{
    code: "TERM_USAGE_SOURCE_MISMATCH",
    occurrenceId: expected[0]!.occurrenceId,
  }]);
});

test("term usage rejects a surface absent from the translated block", () => {
  const expected = expectedTermOccurrences(
    blocks,
    [prokurist],
    getSourceLanguageProfile("de"),
  );
  assert.deepEqual(validateTermUsages(expected, [
    submission(expected[0]!),
  ], new Map([["block-0", "公司代表来了。"]])), [{
    code: "TERM_USAGE_TARGET_NOT_FOUND",
    occurrenceId: expected[0]!.occurrenceId,
  }]);
});

test("term usage reports omitted and duplicate occurrence receipts deterministically", () => {
  const expected = expectedTermOccurrences(
    blocks,
    [prokurist],
    getSourceLanguageProfile("de"),
  );
  assert.deepEqual(validateTermUsages(expected, [], new Map()), expected.map(
    (occurrence) => ({
      code: "TERM_USAGE_MISSING",
      occurrenceId: occurrence.occurrenceId,
    }),
  ));
  assert.deepEqual(validateTermUsages(expected, [
    submission(expected[0]!),
    submission(expected[0]!),
  ], new Map([["block-0", "主事来了。"]])), [{
    code: "TERM_USAGE_DUPLICATE",
    occurrenceId: expected[0]!.occurrenceId,
  }]);
});
