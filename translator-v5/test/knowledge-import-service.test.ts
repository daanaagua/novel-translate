import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import test from "node:test";

import {
  KnowledgeImportService,
  type KnowledgeImportStorageAdapter,
  type PreparedImportRecord,
  type StageBatchInput,
} from "../src/knowledge-import/knowledge-import-service.js";
import type {
  ImportFieldMapping,
  ImportSelection,
  StagedImportReport,
} from "../src/knowledge-import/types.js";

function termSelection(recordPathId?: string): ImportSelection {
  return {
    objectType: "term",
    scope: "book",
    ...(recordPathId === undefined ? {} : { recordPathId }),
  };
}

function mapField(targetField: string, sourceColumn: string): ImportFieldMapping {
  return {
    targetField,
    sourceColumn,
    confidence: "high",
    confirmed: true,
  };
}

async function withTempDirectory(
  callback: (directory: string) => Promise<void>,
): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "folioloom-import-service-"));
  try {
    await callback(directory);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

test("keeps absolute paths private while inspecting and suggesting a JSON mapping", async () => {
  await withTempDirectory(async (directory) => {
    const path = join(directory, "terms.json");
    await writeFile(path, JSON.stringify([
      { source: "Archon", target: "执政官" },
      { source: "Autarch", target: "独裁官" },
    ]), "utf8");
    const service = new KnowledgeImportService();
    const pending = service.registerPending(path);
    assert.equal(pending.fileName, "terms.json");
    assert.equal(JSON.stringify(pending).includes(directory), false);

    const inspected = await service.inspect({
      pendingImportId: pending.pendingImportId,
      operationId: randomUUID(),
    });
    assert.equal(inspected.status, "ready");
    if (inspected.status !== "ready") assert.fail("expected a ready inspection");
    assert.equal(JSON.stringify(inspected).includes(directory), false);
    assert.equal(inspected.inspection.sample.length, 2);
    const recordPathId = inspected.inspection.recordPaths[0]?.id;
    assert.ok(recordPathId);

    const suggestion = await service.suggestMapping(
      pending.pendingImportId,
      termSelection(recordPathId),
    );
    assert.equal(suggestion.fields.source?.sourceColumn, "source");
    assert.equal(suggestion.fields.target?.sourceColumn, "target");
    assert.equal(suggestion.fields.source?.confirmed, true);
  });
});

test("expires pending IDs and enforces the four-file cap without evicting live entries", async () => {
  let now = 1_000;
  const service = new KnowledgeImportService({ now: () => now });
  const ids = Array.from({ length: 4 }, (_item, index) =>
    service.registerPending(`C:\\imports\\terms-${index}.json`).pendingImportId);
  assert.equal(new Set(ids).size, 4);
  assert.throws(
    () => service.registerPending("C:\\imports\\terms-4.json"),
    /KNOWLEDGE_IMPORT_PENDING_LIMIT/u,
  );

  now += 15 * 60 * 1_000 + 1;
  const replacement = service.registerPending("C:\\imports\\replacement.json");
  await assert.rejects(
    () => service.inspect({
      pendingImportId: ids[0] as string,
      operationId: randomUUID(),
    }),
    /KNOWLEDGE_IMPORT_PENDING_UNKNOWN_OR_EXPIRED/u,
  );
  assert.doesNotThrow(() => service.cancelPendingImport(replacement.pendingImportId));
  assert.doesNotThrow(() => service.cancelPendingImport(replacement.pendingImportId));
});

test("rejects relative paths and duplicate active operation IDs", async () => {
  const service = new KnowledgeImportService();
  assert.throws(
    () => service.registerPending("terms.json"),
    /KNOWLEDGE_IMPORT_PATH_INVALID/u,
  );

  await withTempDirectory(async (directory) => {
    const path = join(directory, "terms.json");
    await writeFile(path, JSON.stringify([{ source: "Archon", target: "执政官" }]), "utf8");
    const pending = service.registerPending(path);
    const operationId = randomUUID();
    const first = service.inspect({
      pendingImportId: pending.pendingImportId,
      operationId,
    });
    await assert.rejects(
      () => service.inspect({
        pendingImportId: pending.pendingImportId,
        operationId,
      }),
      /KNOWLEDGE_IMPORT_OPERATION_ACTIVE/u,
    );
    await first;
  });
});

test("cancels staging through an AbortSignal and never exposes partial adapter state", async () => {
  await withTempDirectory(async (directory) => {
    const path = join(directory, "terms.json");
    await writeFile(path, JSON.stringify([
      { source: "Archon", target: "执政官" },
      { source: "Autarch", target: "独裁官" },
    ]), "utf8");
    let entered: (() => void) | undefined;
    const reachedAdapter = new Promise<void>((resolve) => {
      entered = resolve;
    });
    let visibleRows = 0;
    const adapter: KnowledgeImportStorageAdapter = {
      async stageBatch(input: StageBatchInput): Promise<StagedImportReport> {
        const transactionRows = [];
        for await (const record of input.records) transactionRows.push(record);
        entered?.();
        await new Promise<void>((resolve, reject) => {
          const cancel = (): void => reject(new Error("adapter aborted"));
          if (input.signal.aborted) {
            cancel();
            return;
          }
          input.signal.addEventListener("abort", cancel, { once: true });
        });
        visibleRows = transactionRows.length;
        throw new Error("unreachable");
      },
    };
    const service = new KnowledgeImportService({ storage: adapter });
    const pending = service.registerPending(path);
    const inspected = await service.inspect({
      pendingImportId: pending.pendingImportId,
      operationId: randomUUID(),
    });
    assert.equal(inspected.status, "ready");
    if (inspected.status !== "ready") assert.fail("expected a ready inspection");
    const recordPathId = inspected.inspection.recordPaths[0]?.id;
    assert.ok(recordPathId);
    const operationId = randomUUID();
    const staging = service.stage({
      pendingImportId: pending.pendingImportId,
      operationId,
      expectedGeneration: 0,
      expectedSnapshotId: "snapshot-0",
      selection: termSelection(recordPathId),
      fields: {
        source: mapField("source", "source"),
        target: mapField("target", "target"),
      },
    });
    await reachedAdapter;
    service.cancelOperation({ operationId });
    await assert.rejects(() => staging, /KNOWLEDGE_IMPORT_CANCELLED/u);
    assert.equal(visibleRows, 0);
    assert.doesNotThrow(() => service.cancelOperation({ operationId }));
  });
});

test("prepares normalized records for an atomic adapter without disclosing the source path", async () => {
  await withTempDirectory(async (directory) => {
    const path = join(directory, "terms.json");
    await writeFile(path, JSON.stringify([
      { source: "Archon", target: "执政官" },
    ]), "utf8");
    const observed: StageBatchInput[] = [];
    const adapter: KnowledgeImportStorageAdapter = {
      async stageBatch(input) {
        const rows: PreparedImportRecord[] = [];
        for await (const row of input.records) rows.push(row);
        observed.push({ ...input, records: {
          async *[Symbol.asyncIterator]() {
            yield* rows;
          },
        } });
        assert.equal(rows.length, 1);
        assert.equal(rows[0]?.state, "normalized");
        if (rows[0]?.state !== "normalized") assert.fail("expected a normalized row");
        assert.equal(rows[0].record.command.normalizedSubject, "Archon");
        return {
          batchId: input.batchId,
          counts: { ready: 1, merge: 0, conflict: 0, invalid: 0, skipped: 0 },
          unresolved: 0,
          rows: [],
        };
      },
    };
    const service = new KnowledgeImportService({ storage: adapter });
    const pending = service.registerPending(path);
    const inspected = await service.inspect({
      pendingImportId: pending.pendingImportId,
      operationId: randomUUID(),
    });
    assert.equal(inspected.status, "ready");
    if (inspected.status !== "ready") assert.fail("expected a ready inspection");
    const recordPathId = inspected.inspection.recordPaths[0]?.id;
    assert.ok(recordPathId);
    const report = await service.stage({
      pendingImportId: pending.pendingImportId,
      operationId: randomUUID(),
      expectedGeneration: 0,
      expectedSnapshotId: "snapshot-0",
      selection: termSelection(recordPathId),
      fields: {
        source: mapField("source", "source"),
        target: mapField("target", "target"),
      },
    });
    assert.equal(report.counts.ready, 1);
    assert.equal(observed.length, 1);
    assert.equal(JSON.stringify({
      batchId: observed[0]?.batchId,
      sourceName: observed[0]?.sourceName,
      sourceFormat: observed[0]?.sourceFormat,
    }).includes(directory), false);
  });
});
