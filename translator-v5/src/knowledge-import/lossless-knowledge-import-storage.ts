import { canonicalJson } from "../knowledge/knowledge-store.js";
import {
  type CommitStoredKnowledgeImportInput,
  LosslessBookStore,
} from "../storage/lossless-book-store.js";
import { mappingIdentity } from "./field-mapping.js";
import type {
  KnowledgeImportStorageAdapter,
  PreparedImportRecord,
  StageBatchInput,
} from "./knowledge-import-service.js";
import type {
  CommitImportRequest,
  CommittedImportReport,
  DiscardStagedImportRequest,
  ImportDecisionRequest,
  RollbackImportRequest,
  RolledBackImportReport,
  StagedImportPageRequest,
  StagedImportReport,
  StagedImportSummary,
} from "./types.js";

function mappingJson(input: StageBatchInput): string {
  const fields = Object.fromEntries(
    Object.entries(input.request.fields)
      .filter((entry) => entry[1] !== undefined)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0),
  );
  return canonicalJson({
    schema: "folioloom-knowledge-import-mapping-1",
    format: input.sourceFormat,
    selection: input.request.selection,
    fields,
  });
}

function storedMutation(
  runId: string,
  input: CommitImportRequest | RollbackImportRequest,
  signal: AbortSignal,
): CommitStoredKnowledgeImportInput {
  return {
    runId,
    batchId: input.batchId,
    expectedGeneration: input.expectedGeneration,
    expectedSnapshotId: input.expectedSnapshotId,
    signal,
  };
}

export class LosslessKnowledgeImportStorageAdapter
implements KnowledgeImportStorageAdapter {
  constructor(
    readonly store: LosslessBookStore,
    readonly runId: string,
  ) {}

  async stageBatch(input: StageBatchInput): Promise<StagedImportReport> {
    const records: PreparedImportRecord[] = [];
    for await (const record of input.records) {
      if (input.signal.aborted) throw new Error("knowledge import cancelled");
      records.push(record);
    }
    if (input.signal.aborted) throw new Error("knowledge import cancelled");
    const canonicalMapping = mappingJson(input);
    return this.store.stageKnowledgeImport({
      runId: this.runId,
      batchId: input.batchId,
      sourceHash: input.sourceHash,
      sourceName: input.sourceName,
      sourceFormat: input.sourceFormat,
      mappingJson: canonicalMapping,
      mappingHash: mappingIdentity(
        input.sourceFormat,
        input.request.selection,
        input.request.fields,
      ),
      request: input.request,
      records,
    });
  }

  async listStaged(): Promise<readonly StagedImportSummary[]> {
    return this.store.listStagedKnowledgeImports(this.runId);
  }

  async getStaged(input: StagedImportPageRequest): Promise<StagedImportReport> {
    return this.store.getStagedKnowledgeImport(this.runId, input);
  }

  async setDecisions(input: ImportDecisionRequest): Promise<StagedImportReport> {
    return this.store.setKnowledgeImportDecisions(this.runId, input);
  }

  async discardStaged(input: DiscardStagedImportRequest): Promise<void> {
    this.store.discardStagedKnowledgeImport(this.runId, input);
  }

  async commitBatch(
    input: CommitImportRequest & { readonly signal: AbortSignal },
  ): Promise<CommittedImportReport> {
    return this.store.commitKnowledgeImport(
      storedMutation(this.runId, input, input.signal),
    );
  }

  async rollbackBatch(
    input: RollbackImportRequest & { readonly signal: AbortSignal },
  ): Promise<RolledBackImportReport> {
    return this.store.rollbackKnowledgeImport(
      storedMutation(this.runId, input, input.signal),
    );
  }
}
