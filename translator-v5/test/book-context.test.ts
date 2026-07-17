import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";

import { BookContext } from "../src/fullbook/book-context.js";

function createFixture(path: string): void {
  const database = new DatabaseSync(path);
  database.exec(`
    CREATE TABLE blocks (
      id TEXT PRIMARY KEY, legacy_id TEXT, chapter_id TEXT, chapter_title TEXT,
      block_index INTEGER, global_index INTEGER, source_text TEXT,
      source_hash TEXT, token_count INTEGER
    );
    CREATE TABLE concepts (
      id TEXT PRIMARY KEY, canonical_source TEXT, default_target TEXT,
      working_target TEXT, verified_target TEXT, status TEXT, locked INTEGER,
      retired_version INTEGER
    );
    CREATE TABLE lexemes (
      id TEXT PRIMARY KEY, canonical_form TEXT, default_target TEXT,
      working_target TEXT, verified_target TEXT, status TEXT, locked INTEGER,
      retired_version INTEGER
    );
    CREATE TABLE concept_lexemes (
      concept_id TEXT, lexeme_id TEXT, status TEXT, retired_version INTEGER
    );
    CREATE TABLE source_forms (lexeme_id TEXT, form TEXT);
  `);
  const insert = database.prepare(`
    INSERT INTO blocks VALUES(?, ?, 'ch1', 'One', ?, ?, ?, ?, 10)
  `);
  insert.run("block-0", "legacy-0", 0, 0, "Smoky walked.", "hash-0");
  insert.run("block-1", "legacy-1", 1, 1, "Smoky stopped.", "hash-1");
  insert.run("block-2", "legacy-2", 2, 2, "Alice waited.", "hash-2");
  database.close();
}

test("book context loads source and builds one reusable evidence index", () => {
  const directory = mkdtempSync(join(tmpdir(), "v5-book-context-"));
  const path = join(directory, "book.db");
  createFixture(path);
  const context = BookContext.open(path);
  try {
    assert.deepEqual(context.blocks.map((item) => item.globalIndex), [0, 1, 2]);
    assert.equal(context.stableTerms.length, 0);
    assert.equal(context.evidenceIndex, context.evidenceIndex);
    assert.equal(context.sourceFingerprint.length, 64);
    assert.deepEqual(
      context.blocksForIndexes([2, 0]).map((item) => item.globalIndex),
      [0, 2],
    );
  } finally {
    context.close();
  }
});
