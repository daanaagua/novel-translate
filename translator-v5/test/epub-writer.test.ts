import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

import { XMLParser } from "fast-xml-parser";

import {
  writeLosslessBookEpub,
} from "../src/export/epub-writer.js";
import {
  readStoredZipEntries,
  writeStoredZip,
} from "../src/export/stored-zip.js";
import { BookContext } from "../src/fullbook/book-context.js";
import { planBookWindows } from "../src/fullbook/window-planner.js";
import { createKnowledgeSnapshot } from "../src/knowledge/snapshot.js";
import { LosslessBookStore } from "../src/storage/lossless-book-store.js";

function sourceManifest(source: string): string {
  const directory = mkdtempSync(join(tmpdir(), "folioloom-epub-writer-"));
  const payload = Buffer.from(source, "utf8");
  const hash = createHash("sha256").update(payload).digest("hex");
  writeFileSync(join(directory, "original.txt"), payload);
  writeFileSync(join(directory, "source.txt"), payload);
  const manifest = join(directory, "source_manifest.json");
  writeFileSync(manifest, JSON.stringify({
    schema_version: "v5-source-ledger-1",
    coordinate_unit: "unicode_scalar",
    raw_path: "original.txt",
    raw_size: payload.length,
    raw_sha256: hash,
    source_format: ".txt",
    encoding: "utf-8",
    extractor: "plain-text-v1",
    sourceLanguage: "en",
    canonical_path: "source.txt",
    canonical_chars: [...source].length,
    canonical_sha256: hash,
    canonical_segments: [{
      canonical_start: 0,
      canonical_end: [...source].length,
      origin_kind: "decoded_bytes",
      origin_ref: "original.txt",
      raw_start: 0,
      raw_end: payload.length,
      transformation: "decode+newline-normalize",
    }],
    excluded_raw_ranges: [],
  }), "utf8");
  return manifest;
}

function fixture(options: { chapters: boolean }): {
  directory: string;
  store: LosslessBookStore;
  runId: string;
  epubPath: string;
  expectedTranslations: string[];
} {
  const source = [
    "A".repeat(3_500),
    "B".repeat(3_500),
    "C".repeat(3_500),
    "D".repeat(3_500),
  ].join("\n\n");
  const manifest = sourceManifest(source);
  const directory = dirname(manifest);
  const context = BookContext.openLossless({ manifestPath: manifest });
  const store = new LosslessBookStore(join(directory, "book.db"));
  const runId = options.chapters ? "run-chapters" : "run-fallback";
  store.registerSource(context.certifiedSource!);
  store.replaceDerivedPlan(context.sourceLedger.sourceVersion, {
    annotations: context.annotations,
    blocks: context.losslessBlocks,
  });
  const snapshot = createKnowledgeSnapshot(runId, []);
  store.createTranslationRun({
    runId,
    sourceVersion: context.sourceLedger.sourceVersion,
    protocolVersion: "v5-book-3",
    modelId: "epub-test-model",
    initialSnapshotId: snapshot.id,
    initialSnapshot: snapshot,
    metadata: { fixture: "epub-writer" },
  });
  const baseWindows = planBookWindows(context.losslessBlocks, {
    protocolVersion: "v5-book-3",
    maxBlocks: 1,
  });
  const windows = options.chapters
    ? baseWindows.map((window, index) => ({
      ...window,
      chapterId: `chapter-${Math.floor(index / 2) + 1}`,
      chapterTitle: `Source chapter ${Math.floor(index / 2) + 1}`,
    }))
    : baseWindows;
  store.initializeWindowPlan(runId, windows);
  const expectedTranslations: string[] = [];
  for (const [index, window] of windows.entries()) {
    store.bindWindowsToSnapshot(runId, [window.windowId], snapshot.id);
    store.claimWindow(runId, window.windowId);
    const text = [
      `第 ${index + 1} 段 & <保留> “引号”`,
      `日本語 ${index + 1}\n한국어 ${index + 1}`,
    ].join("\n\n");
    expectedTranslations.push(text);
    store.stageWindow({
      runId,
      windowId: window.windowId,
      snapshotId: snapshot.id,
      status: "completed",
      translations: window.blockIds.map((blockId) => {
        const block = context.losslessBlocks.find((item) => item.id === blockId)!;
        return { blockId, sourceHash: block.sourceHash, text };
      }),
      knowledgeCandidates: [],
      styleTail: "",
      budget: {},
      warnings: [],
    });
    store.promoteStagedWindow(runId, window.windowId);
  }
  context.close();
  return {
    directory,
    store,
    runId,
    epubPath: join(directory, `${runId}.epub`),
    expectedTranslations,
  };
}

test("EPUB writer emits a valid ordered EPUB 3 with escaped CJK XHTML and lineage", () => {
  const item = fixture({ chapters: true });
  try {
    assert.equal(writeLosslessBookEpub(
      item.store,
      item.runId,
      item.epubPath,
      { title: "书名 & <测试>", language: "zh-CN" },
    ), item.epubPath);

    const entries = readStoredZipEntries(item.epubPath);
    assert.equal(entries[0]?.name, "mimetype");
    assert.equal(entries[0]?.method, 0);
    assert.equal(entries[0]?.data.toString("utf8"), "application/epub+zip");
    for (const required of [
      "META-INF/container.xml",
      "META-INF/v5-lineage.json",
      "EPUB/package.opf",
      "EPUB/nav.xhtml",
      "EPUB/styles.css",
    ]) {
      assert.ok(entries.some((entry) => entry.name === required), required);
    }

    const parser = new XMLParser({ ignoreAttributes: false });
    const xmlEntries = entries.filter((entry) =>
      /\.(?:xml|opf|xhtml)$/u.test(entry.name));
    assert.doesNotThrow(() => {
      for (const entry of xmlEntries) parser.parse(entry.data.toString("utf8"));
    });
    const sections = entries.filter((entry) => /^EPUB\/section-\d+\.xhtml$/u.test(entry.name));
    assert.equal(sections.length, 2);
    const joined = sections.map((entry) => entry.data.toString("utf8")).join("\n");
    assert.match(joined, /&amp;/u);
    assert.match(joined, /&lt;保留&gt;/u);
    assert.match(joined, /日本語/u);
    assert.match(joined, /한국어/u);
    const positions = item.expectedTranslations.map((text) =>
      joined.indexOf(text.split("\n\n")[0]!.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")));
    assert.ok(positions.every((position) => position >= 0));
    assert.deepEqual([...positions].sort((left, right) => left - right), positions);

    const lineageEntry = entries.find((entry) => entry.name === "META-INF/v5-lineage.json");
    assert.ok(lineageEntry);
    const lineage = JSON.parse(lineageEntry.data.toString("utf8"));
    assert.equal(lineage.schema, "v5-book-lineage-1");
    assert.equal(lineage.runId, item.runId);
    assert.equal(lineage.complete, true);
  } finally {
    item.store.close();
    rmSync(item.directory, { recursive: true, force: true });
  }
});

test("EPUB writer falls back to deterministic block-boundary sections", () => {
  const item = fixture({ chapters: false });
  try {
    writeLosslessBookEpub(
      item.store,
      item.runId,
      item.epubPath,
      {
        title: "Fallback",
        language: "zh-CN",
        fallbackSectionChars: 30,
      },
    );
    const sections = readStoredZipEntries(item.epubPath)
      .filter((entry) => /^EPUB\/section-\d+\.xhtml$/u.test(entry.name));
    assert.equal(sections.length, item.expectedTranslations.length);
  } finally {
    item.store.close();
    rmSync(item.directory, { recursive: true, force: true });
  }
});

test("stored ZIP writer rejects duplicate and traversal entry names", () => {
  const directory = mkdtempSync(join(tmpdir(), "folioloom-stored-zip-"));
  try {
    assert.throws(
      () => writeStoredZip(join(directory, "duplicate.zip"), [
        { name: "same.txt", data: "a" },
        { name: "same.txt", data: "b" },
      ]),
      /duplicate ZIP entry/u,
    );
    for (const name of ["../escape.txt", "/absolute.txt", "folder\\file.txt"]) {
      assert.throws(
        () => writeStoredZip(join(directory, "invalid.zip"), [{ name, data: "x" }]),
        /invalid ZIP entry/u,
      );
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
