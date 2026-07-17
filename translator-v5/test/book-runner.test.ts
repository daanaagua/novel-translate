import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";

import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
  type Context,
} from "@earendil-works/pi-ai";

import { runBook } from "../src/fullbook/book-runner.js";
import { BookStore } from "../src/storage/book-store.js";

function createFixture(path: string, blockCount: number): void {
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
    INSERT INTO blocks VALUES(?, ?, 'ch1', 'One', ?, ?, ?, ?, 12)
  `);
  for (let index = 0; index < blockCount; index += 1) {
    insert.run(
      `block-${index}`,
      `legacy-${index}`,
      index,
      index,
      `The person moved slowly through room ${index}.`,
      `hash-${index}`,
    );
  }
  database.close();
}

function userText(context: Context): string {
  const message = context.messages.findLast((item) => item.role === "user");
  assert.ok(message && message.role === "user");
  return typeof message.content === "string"
    ? message.content
    : message.content
      .filter((item) => item.type === "text")
      .map((item) => item.text)
      .join("\n");
}

function dynamicResponse(context: Context) {
  const prompt = userText(context);
  if (prompt.includes("Submit zero to four additional questions")) {
    return fauxAssistantMessage(
      fauxToolCall("submit_questions", { questions: [] }),
      { stopReason: "toolUse" },
    );
  }
  const ids = [...prompt.matchAll(/\[(block-\d+)\]/gu)]
    .map((match) => match[1])
    .filter((value): value is string => value !== undefined);
  assert.ok(ids.length > 0, prompt.slice(0, 300));
  return fauxAssistantMessage(fauxToolCall("finalize_translation", {
    translations: ids.map((blockId) => ({
      blockId,
      text: "这是对应文本块的完整中文译文，保留了原文全部信息。",
    })),
    notes: [],
  }), { stopReason: "toolUse" });
}

test("book runner warms up, runs a parallel wave, and resumes without model calls", async () => {
  const directory = mkdtempSync(join(tmpdir(), "v5-book-runner-"));
  const sourceDb = join(directory, "source.db");
  const storePath = join(directory, "state.db");
  createFixture(sourceDb, 4);
  const faux = fauxProvider();
  faux.setResponses(Array.from({ length: 4 }, () => dynamicResponse));
  const first = await runBook({
    dbPath: sourceDb,
    storePath,
    outputDir: join(directory, "output"),
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    windowOptions: { maxBlocks: 1, maxSourceTokens: 100 },
    warmupWindows: 2,
    maxConcurrency: 2,
    maxAttempts: 2,
    hardDeadlineMs: 30_000,
  });
  assert.deepEqual(first.waves.map((wave) => wave.concurrency), [1, 1, 2]);
  assert.equal(first.status.completedWindows, 4);
  assert.equal(first.status.modelCalls, 4);

  const noCalls = fauxProvider();
  noCalls.setResponses([]);
  const resumed = await runBook({
    dbPath: sourceDb,
    storePath,
    outputDir: join(directory, "output"),
    model: noCalls.getModel(),
    streamFn: noCalls.provider.streamSimple.bind(noCalls.provider),
    windowOptions: { maxBlocks: 1, maxSourceTokens: 100 },
    warmupWindows: 2,
    maxConcurrency: 2,
    maxAttempts: 2,
    hardDeadlineMs: 30_000,
  });
  assert.equal(resumed.processedWindows, 0);
  assert.equal(resumed.status.modelCalls, 4);
});

test("book runner retries a no-submit window with a fresh per-window budget", async () => {
  const directory = mkdtempSync(join(tmpdir(), "v5-book-retry-"));
  const sourceDb = join(directory, "source.db");
  const storePath = join(directory, "state.db");
  createFixture(sourceDb, 1);
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage("No translation was submitted."),
    dynamicResponse,
  ]);
  const result = await runBook({
    dbPath: sourceDb,
    storePath,
    outputDir: join(directory, "output"),
    model: faux.getModel(),
    streamFn: faux.provider.streamSimple.bind(faux.provider),
    windowOptions: { maxBlocks: 1, maxSourceTokens: 100 },
    warmupWindows: 0,
    maxConcurrency: 1,
    maxAttempts: 2,
    hardDeadlineMs: 30_000,
  });
  const store = new BookStore(storePath);
  const window = store.window(store.pendingWindows()[0]?.windowId ?? "")
    ?? result.windows[0];
  assert.equal(result.status.completedWindows, 1);
  assert.equal(window?.attemptCount, 2);
  assert.equal(result.status.modelCalls, 2);
  store.close();
});

test("book runner aborts external provider failures without creating a human task", async () => {
  const directory = mkdtempSync(join(tmpdir(), "v5-book-provider-failure-"));
  const sourceDb = join(directory, "source.db");
  const storePath = join(directory, "state.db");
  createFixture(sourceDb, 1);
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage([], {
      stopReason: "error",
      errorMessage: "401: authentication failed for fixture credential",
    }),
  ]);

  await assert.rejects(
    runBook({
      dbPath: sourceDb,
      storePath,
      outputDir: join(directory, "output"),
      model: faux.getModel(),
      streamFn: faux.provider.streamSimple.bind(faux.provider),
      windowOptions: { maxBlocks: 1, maxSourceTokens: 100 },
      maxAttempts: 2,
      hardDeadlineMs: 30_000,
    }),
    /authentication failed/i,
  );
  const store = new BookStore(storePath);
  assert.equal(store.statusSummary().humanRequiredWindows, 0);
  assert.equal(store.statusSummary().pendingWindows, 1);
  store.close();
});
