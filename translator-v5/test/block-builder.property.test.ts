import assert from "node:assert/strict";
import test from "node:test";

import { auditSourceCoverage } from "../src/source/auditor.js";
import { buildLosslessBlocks } from "../src/source/block-builder.js";
import { annotateStructure } from "../src/source/structure-annotator.js";
import { scalarLength, scalarSlice } from "../src/source/types.js";

function xorshift32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return state >>> 0;
  };
}

function generatedSource(sample: number): string {
  const random = xorshift32(0x9e37_79b9 ^ sample);
  const newline = ["\n", "\r\n", "\r"][random() % 3] as string;
  const volume = `Book ${(random() % 3) + 1}`;
  const chapter = random() % 5 === 0 ? "Chapter" : "Chapter I";
  const unicode = ["烟雾😀", "naïve café", "Ωρίων", "𐍈music"][
    random() % 4
  ] as string;
  const tinyCount = (random() % 8) + 1;
  const tiny = Array.from(
    { length: tinyCount },
    (_unused, index) => `${index % 2 === 0 ? chapter : "Chapter I"}${newline}${unicode} ${index}.`,
  );
  const long = sample % 20 === 0
    ? `${newline}${newline}${"Long paragraph 😀. ".repeat(300)}`
    : "";
  return [volume, ...tiny, volume].join(`${newline}${newline}`) + long;
}

test("property: 240 fixed-seed books have unique scalar blocks with exact reconstruction", () => {
  for (let sample = 0; sample < 240; sample += 1) {
    const source = generatedSource(sample);
    const sourceVersion = `source-${sample}`;
    const annotations = annotateStructure(source, sourceVersion);
    const blocks = buildLosslessBlocks(source, annotations, {
      maxSourceTokens: (sample % 31) + 1,
      sourceVersion,
    });
    const report = auditSourceCoverage(source, blocks, { sourceVersion });

    assert.equal(report.ok, true, `sample ${sample}: ${JSON.stringify(report.incidents)}`);
    assert.equal(blocks.map((block) => block.sourceText).join(""), source);
    assert.equal(new Set(blocks.map((block) => block.id)).size, blocks.length);
    assert.equal(blocks[0]?.canonicalStart, 0);
    assert.equal(blocks.at(-1)?.canonicalEnd, scalarLength(source));
    for (let index = 0; index < blocks.length; index += 1) {
      const block = blocks[index]!;
      assert.equal(block.globalIndex, index);
      assert.equal(
        block.sourceText,
        scalarSlice(source, block.canonicalStart, block.canonicalEnd),
      );
      if (index > 0) {
        assert.equal(block.canonicalStart, blocks[index - 1]?.canonicalEnd);
      }
    }
  }
});

test("property: emoji ranges stay Unicode-scalar while slices reconstruct exact UTF-16 text", () => {
  const source = "Chapter I\n\nA😀B.\n\nChapter I\n\nC𐍈D.";
  const annotations = annotateStructure(source, "emoji-source");
  const blocks = buildLosslessBlocks(source, annotations, {
    maxSourceTokens: 2,
    sourceVersion: "emoji-source",
  });

  assert.equal(blocks.at(-1)?.canonicalEnd, scalarLength(source));
  assert.notEqual(scalarLength(source), source.length);
  assert.equal(blocks.map((block) => block.sourceText).join(""), source);
  assert.equal(auditSourceCoverage(source, blocks, { sourceVersion: "emoji-source" }).ok, true);
});

test("property: block ids do not depend on structure titles", () => {
  const source = "Chapter I\n\nAlpha.\n\nChapter I\n\nBeta.";
  const annotations = annotateStructure(source, "stable-source");
  const renamed = annotations.map((annotation) => ({
    ...annotation,
    title: `renamed-${annotation.title}`,
  }));
  const options = { maxSourceTokens: 4, sourceVersion: "stable-source" };

  assert.deepEqual(
    buildLosslessBlocks(source, annotations, options).map((block) => block.id),
    buildLosslessBlocks(source, renamed, options).map((block) => block.id),
  );
});
