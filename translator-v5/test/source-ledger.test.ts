import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { auditSourceCoverage } from "../src/source/auditor.js";
import { buildLosslessBlocks } from "../src/source/block-builder.js";
import {
  SourceIntegrityError,
  SourceLedger,
} from "../src/source/source-ledger.js";
import { annotateStructure } from "../src/source/structure-annotator.js";
import { scalarLength } from "../src/source/types.js";

function sha256(payload: string | Buffer): string {
  return createHash("sha256").update(payload).digest("hex");
}

interface LedgerFixture {
  directory: string;
  manifestPath: string;
  rawPath: string;
  canonicalPath: string;
}

function ledgerFixture(source: string): LedgerFixture {
  const directory = mkdtempSync(join(tmpdir(), "v5-source-ledger-"));
  const rawPath = join(directory, "original.txt");
  const canonicalPath = join(directory, "source.txt");
  const manifestPath = join(directory, "source_manifest.json");
  const raw = Buffer.from(source, "utf8");
  const canonical = Buffer.from(source, "utf8");
  writeFileSync(rawPath, raw);
  writeFileSync(canonicalPath, canonical);
  writeFileSync(manifestPath, JSON.stringify({
    schema_version: "v5-source-ledger-1",
    coordinate_unit: "unicode_scalar",
    raw_path: "original.txt",
    raw_size: raw.length,
    raw_sha256: sha256(raw),
    source_format: ".txt",
    encoding: "utf-8",
    extractor: "plain-text-v1",
    canonical_path: "source.txt",
    canonical_chars: scalarLength(source),
    canonical_sha256: sha256(canonical),
    canonical_segments: [{
      canonical_start: 0,
      canonical_end: scalarLength(source),
      origin_kind: "decoded_bytes",
      origin_ref: "source/original.txt",
      raw_start: 0,
      raw_end: raw.length,
      transformation: "decode+newline-normalize",
    }],
    excluded_raw_ranges: [],
  }), "utf8");
  return { directory, manifestPath, rawPath, canonicalPath };
}

function mutateManifest(
  fixture: LedgerFixture,
  mutate: (manifest: Record<string, unknown>) => void,
): void {
  const manifest = JSON.parse(
    readFileSync(fixture.manifestPath, "utf8"),
  ) as Record<string, unknown>;
  mutate(manifest);
  writeFileSync(fixture.manifestPath, JSON.stringify(manifest), "utf8");
}

function expectIntegrityCode(action: () => unknown, code: string): void {
  assert.throws(action, (error: unknown) => (
    error instanceof SourceIntegrityError && error.code === code
  ));
}

test("lossless source ledger verifies hashes and keeps Unicode scalar coordinates", () => {
  const fixture = ledgerFixture("A😀\nB");
  try {
    const ledger = SourceLedger.open(fixture.manifestPath);
    assert.equal(ledger.sourceText, "A😀\nB");
    assert.equal(ledger.sourceVersion, sha256(Buffer.from("A😀\nB")));
    assert.equal(ledger.canonicalChars, 4);
    assert.equal(ledger.slice(1, 2), "😀");
    assert.deepEqual(
      ledger.canonicalSegments.map((segment) => [segment.canonicalStart, segment.canonicalEnd]),
      [[0, 4]],
    );
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("lossless source ledger rejects raw and canonical tampering with stable codes", () => {
  const rawFixture = ledgerFixture("Alpha.");
  const canonicalFixture = ledgerFixture("Beta.");
  try {
    writeFileSync(rawFixture.rawPath, "Omega!", "utf8");
    expectIntegrityCode(
      () => SourceLedger.open(rawFixture.manifestPath),
      "RAW_HASH_MISMATCH",
    );

    writeFileSync(canonicalFixture.canonicalPath, "Zeta!", "utf8");
    expectIntegrityCode(
      () => SourceLedger.open(canonicalFixture.manifestPath),
      "CANONICAL_HASH_MISMATCH",
    );
  } finally {
    rmSync(rawFixture.directory, { recursive: true, force: true });
    rmSync(canonicalFixture.directory, { recursive: true, force: true });
  }
});

test("lossless source ledger rejects segment gaps and unknown exclusion policies", () => {
  const gapFixture = ledgerFixture("Alpha.");
  const policyFixture = ledgerFixture("Beta.");
  try {
    mutateManifest(gapFixture, (manifest) => {
      manifest.canonical_segments = [{
        canonical_start: 1,
        canonical_end: 6,
        origin_kind: "decoded_bytes",
        origin_ref: "source/original.txt",
        transformation: "decode+newline-normalize",
      }];
    });
    expectIntegrityCode(
      () => SourceLedger.open(gapFixture.manifestPath),
      "CANONICAL_SEGMENTS_INVALID",
    );

    mutateManifest(policyFixture, (manifest) => {
      manifest.excluded_raw_ranges = [{
        raw_start: 0,
        raw_end: 1,
        policy: "free-form exclusion",
      }];
    });
    expectIntegrityCode(
      () => SourceLedger.open(policyFixture.manifestPath),
      "EXCLUDED_POLICY_UNKNOWN",
    );
  } finally {
    rmSync(gapFixture.directory, { recursive: true, force: true });
    rmSync(policyFixture.directory, { recursive: true, force: true });
  }
});

test("lossless blocks cover canonical source exactly once despite duplicate chapter names", () => {
  const source = "Book One\n\nChapter I\n\nAlpha.\n\nBook Two\n\nChapter I\n\nBeta.";
  const annotations = annotateStructure(source, "source-v1");
  const blocks = buildLosslessBlocks(source, annotations, { maxSourceTokens: 8 });
  const report = auditSourceCoverage(source, blocks);
  assert.equal(report.ok, true);
  assert.equal(blocks.map((item) => item.sourceText).join(""), source);
  assert.equal(new Set(blocks.map((item) => item.id)).size, blocks.length);
  assert.equal(
    annotations.filter((item) => item.kind === "chapter_heading").length,
    2,
  );
});

test("auditor reports the exact first gap without using chapter ids", () => {
  const source = "Alpha.\n\nBeta.";
  const blocks = buildLosslessBlocks(source, [], { maxSourceTokens: 100 });
  const report = auditSourceCoverage(source, [{ ...blocks[0]!, canonicalEnd: 5 }]);
  assert.equal(report.ok, false);
  assert.equal(report.incidents[0]?.code, "SOURCE_SPAN_GAP");
  assert.equal(report.incidents[0]?.start, 5);
});

test("auditor independently detects overlap and source hash tampering", () => {
  const source = "Alpha.\n\nBeta.";
  const blocks = buildLosslessBlocks(source, [], { maxSourceTokens: 2 });
  assert.ok(blocks.length >= 2);
  const overlap = blocks.map((block, index) => index === 1
    ? { ...block, canonicalStart: block.canonicalStart - 1 }
    : block);
  const overlapReport = auditSourceCoverage(source, overlap);
  assert.equal(overlapReport.ok, false);
  assert.equal(overlapReport.incidents[0]?.code, "SOURCE_SPAN_OVERLAP");

  const tampered = blocks.map((block, index) => index === 0
    ? { ...block, sourceHash: "0".repeat(64) }
    : block);
  assert.ok(
    auditSourceCoverage(source, tampered).incidents.some(
      (incident) => incident.code === "SOURCE_HASH_MISMATCH",
    ),
  );
});
