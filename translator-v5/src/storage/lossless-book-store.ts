import { createHash, randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync, type StatementSync } from "node:sqlite";

import type { BookWindowPlan, BookWindowStatus } from "../fullbook/types.js";
import type { LosslessBlock, StructureAnnotation } from "../source/types.js";
import { scalarLength } from "../source/types.js";
import { LOSSLESS_BOOK_SCHEMA_V2 } from "./book-schema-v2.js";

export interface CertifiedSourceRange {
  rangeId: string;
  canonicalStart: number;
  canonicalEnd: number;
  originKind: string;
  originRef: string;
  transformation: string;
  rawStart?: number;
  rawEnd?: number;
}

export interface CertifiedSourceInput {
  sourceVersion: string;
  rawSha256: string;
  canonicalSha256: string;
  canonicalChars: number;
  coordinateUnit: "unicode_scalar";
  sourceFormat: string;
  encoding: string;
  extractor: string;
  ranges: readonly CertifiedSourceRange[];
}

export interface DerivedPlan {
  blocks: readonly LosslessBlock[];
  annotations: readonly StructureAnnotation[];
}

export interface TranslationRunMeta {
  runId?: string;
  sourceVersion: string;
  protocolVersion: string;
  modelId: string;
  initialSnapshotId: string;
  metadata?: unknown;
}

export interface StagedTranslationInput {
  blockId: string;
  sourceHash: string;
  text: string;
}

export interface KnowledgeCandidateInput {
  recordId: string;
  normalizedSubject: string;
  kind: string;
  payload: unknown;
}

export interface WindowStageInput {
  runId: string;
  windowId: string;
  snapshotId: string;
  status: "completed" | "completed_with_warnings";
  translations: StagedTranslationInput[];
  knowledgeCandidates: KnowledgeCandidateInput[];
  styleTail: string;
  budget: Readonly<Record<string, number>>;
  warnings: readonly string[];
}

export interface WindowFailureInput {
  error: string;
  retry: boolean;
  budget: Readonly<Record<string, number>>;
  warnings: readonly string[];
}

export interface PersistedLosslessWindow extends BookWindowPlan {
  status: BookWindowStatus | "staged";
  attemptCount: number;
  snapshotId: string | null;
  budget: Record<string, number>;
  warnings: string[];
  lastError: string;
}

export interface ActiveLosslessTranslation {
  runId: string;
  windowId: string;
  blockId: string;
  sourceVersion: string;
  sourceHash: string;
  text: string;
  status: "completed" | "completed_with_warnings";
  version: number;
}

export interface KnowledgeHistoryRecord {
  runId: string;
  recordId: string;
  revision: number;
  windowId: string;
  snapshotId: string;
  normalizedSubject: string;
  kind: string;
  payload: unknown;
  status: string;
  active: boolean;
}

export interface AuditProjection {
  runId: string;
  sourceVersion: string;
  protocolVersion: string;
  modelId: string;
  status: string;
  windows: PersistedLosslessWindow[];
  translations: ActiveLosslessTranslation[];
  knowledge: KnowledgeHistoryRecord[];
  snapshotIds: string[];
}

interface SourceVersionRow {
  source_version: string;
  canonical_chars: number;
  source_fingerprint: string;
  plan_fingerprint: string | null;
}

interface RunRow {
  run_id: string;
  source_version: string;
  protocol_version: string;
  model_id: string;
  status: string;
}

interface WindowRow {
  run_id: string;
  window_id: string;
  source_version: string;
  ordinal: number;
  chapter_id: string;
  chapter_title: string | null;
  source_tokens: number;
  source_chars: number;
  oversized: number;
  status: BookWindowStatus | "staged";
  attempt_count: number;
  snapshot_id: string | null;
  budget_json: string;
  warnings_json: string;
  last_error: string;
}

interface LogicalBlockRow {
  block_id: string;
  source_version: string;
  source_hash: string;
  global_index: number;
  canonical_start: number;
  canonical_end: number;
  token_count: number;
}

function all<T>(statement: StatementSync, ...values: any[]): T[] {
  return statement.all(...values) as unknown as T[];
}

function one<T>(statement: StatementSync, ...values: any[]): T | undefined {
  return statement.get(...values) as unknown as T | undefined;
}

function requireNonempty(value: string, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new TypeError(`${label} must be nonempty`);
  }
  return value;
}

function requireSafeInteger(value: number, label: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || value < minimum) {
    throw new TypeError(`${label} must be a safe integer >= ${minimum}`);
  }
  return value;
}

function jsonText(value: unknown, label: string): string {
  try {
    const encoded = JSON.stringify(value, (_key, candidate: unknown) => {
      if (candidate === undefined
        || typeof candidate === "function"
        || typeof candidate === "symbol"
        || typeof candidate === "bigint") {
        throw new TypeError("unsupported JSON value");
      }
      if (typeof candidate === "number" && !Number.isFinite(candidate)) {
        throw new TypeError("non-finite JSON number");
      }
      return candidate;
    });
    if (encoded === undefined) {
      throw new TypeError("undefined JSON root");
    }
    return encoded;
  } catch (error) {
    throw new TypeError(`${label} must be JSON-serializable`, { cause: error });
  }
}

function hashText(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function stringArrayFromJson(value: string, label: string): string[] {
  const parsed = JSON.parse(value) as unknown;
  if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "string")) {
    throw new Error(`corrupt ${label} JSON`);
  }
  return [...parsed];
}

function budgetFromJson(value: string): Record<string, number> {
  const parsed = JSON.parse(value) as unknown;
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("corrupt budget JSON");
  }
  const result: Record<string, number> = {};
  for (const [key, candidate] of Object.entries(parsed)) {
    if (typeof candidate !== "number" || !Number.isFinite(candidate) || candidate < 0) {
      throw new Error("corrupt budget JSON");
    }
    result[key] = candidate;
  }
  return result;
}

function validateBudget(budget: Readonly<Record<string, number>>): string {
  if (budget === null || typeof budget !== "object" || Array.isArray(budget)) {
    throw new TypeError("budget must be an object");
  }
  for (const [key, value] of Object.entries(budget)) {
    requireNonempty(key, "budget key");
    if (!Number.isFinite(value) || value < 0) {
      throw new TypeError(`budget value for ${key} must be finite and non-negative`);
    }
  }
  return jsonText(budget, "budget");
}

function validateWarnings(warnings: readonly string[]): string {
  if (!Array.isArray(warnings) || warnings.some((item) => typeof item !== "string")) {
    throw new TypeError("warnings must be an array of strings");
  }
  return jsonText(warnings, "warnings");
}

function windowFromRow(row: WindowRow, blockIds: string[]): PersistedLosslessWindow {
  return {
    windowId: row.window_id,
    ordinal: row.ordinal,
    chapterId: row.chapter_id,
    chapterTitle: row.chapter_title,
    blockIds,
    globalIndexes: [],
    sourceTokens: row.source_tokens,
    sourceChars: row.source_chars,
    oversized: Boolean(row.oversized),
    status: row.status,
    attemptCount: row.attempt_count,
    snapshotId: row.snapshot_id,
    budget: budgetFromJson(row.budget_json),
    warnings: stringArrayFromJson(row.warnings_json, "warnings"),
    lastError: row.last_error,
  };
}

export class LosslessBookStore {
  readonly #database: DatabaseSync;

  constructor(path: string) {
    const absolute = resolve(requireNonempty(path, "database path"));
    mkdirSync(dirname(absolute), { recursive: true });
    this.#database = new DatabaseSync(absolute);
    this.#database.exec("PRAGMA foreign_keys=ON");
    this.#database.exec("PRAGMA journal_mode=WAL");
    const legacyTable = one<{ name: string }>(this.#database.prepare(`
      SELECT name FROM sqlite_master
      WHERE type='table' AND name IN ('book_meta', 'book_blocks', 'windows')
      LIMIT 1
    `));
    if (legacyTable !== undefined) {
      this.#database.close();
      throw new Error("legacy BookStore schema requires a new database for schema v2; in-place migration is forbidden");
    }
    this.#database.exec(LOSSLESS_BOOK_SCHEMA_V2);
  }

  databaseSettings(): { foreignKeys: boolean; journalMode: string } {
    const foreignKeys = one<{ foreign_keys: number }>(
      this.#database.prepare("PRAGMA foreign_keys"),
    )?.foreign_keys;
    const journalMode = one<{ journal_mode: string }>(
      this.#database.prepare("PRAGMA journal_mode"),
    )?.journal_mode;
    return { foreignKeys: foreignKeys === 1, journalMode: journalMode ?? "" };
  }

  registerSource(input: CertifiedSourceInput): string {
    const normalized = this.#validateSource(input);
    const payload = jsonText(normalized, "certified source");
    const fingerprint = hashText(payload);
    const existing = one<SourceVersionRow>(
      this.#database.prepare("SELECT * FROM source_versions WHERE source_version=?"),
      input.sourceVersion,
    );
    if (existing !== undefined) {
      if (existing.source_fingerprint !== fingerprint) {
        throw new Error(`source version ${input.sourceVersion} already identifies a different source`);
      }
      return existing.source_version;
    }

    this.#transaction(() => {
      this.#database.prepare(`
        INSERT INTO source_versions(
          source_version, raw_sha256, canonical_sha256, canonical_chars,
          coordinate_unit, source_format, encoding, extractor,
          source_fingerprint, source_payload_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(
        normalized.sourceVersion,
        normalized.rawSha256,
        normalized.canonicalSha256,
        normalized.canonicalChars,
        normalized.coordinateUnit,
        normalized.sourceFormat,
        normalized.encoding,
        normalized.extractor,
        fingerprint,
        payload,
      );
      const insertRange = this.#database.prepare(`
        INSERT INTO source_ranges(
          source_version, range_id, canonical_start, canonical_end,
          origin_kind, origin_ref, transformation, raw_start, raw_end
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
      `);
      for (const range of normalized.ranges) {
        insertRange.run(
          normalized.sourceVersion,
          range.rangeId,
          range.canonicalStart,
          range.canonicalEnd,
          range.originKind,
          range.originRef,
          range.transformation,
          range.rawStart ?? null,
          range.rawEnd ?? null,
        );
      }
      this.#appendEvent(null, "source_registered", {
        sourceVersion: normalized.sourceVersion,
        fingerprint,
      });
    });
    return normalized.sourceVersion;
  }

  replaceDerivedPlan(sourceVersion: string, plan: DerivedPlan): void {
    requireNonempty(sourceVersion, "sourceVersion");
    const source = this.#source(sourceVersion);
    const normalized = this.#validateDerivedPlan(source, plan);
    const fingerprint = hashText(jsonText(normalized, "derived plan"));
    if (source.plan_fingerprint !== null) {
      if (source.plan_fingerprint !== fingerprint) {
        throw new Error(`derived plan for ${sourceVersion} already contains different data`);
      }
      return;
    }

    this.#transaction(() => {
      const insertAnnotation = this.#database.prepare(`
        INSERT INTO structure_annotations(
          source_version, annotation_id, kind, canonical_start, canonical_end,
          title, boundary_weight
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
      `);
      for (const annotation of normalized.annotations) {
        insertAnnotation.run(
          sourceVersion,
          annotation.id,
          annotation.kind,
          annotation.start,
          annotation.end,
          annotation.title,
          annotation.boundaryWeight,
        );
      }
      const insertBlock = this.#database.prepare(`
        INSERT INTO logical_blocks(
          source_version, block_id, canonical_start, canonical_end, source_text,
          source_hash, global_index, token_count, structure_id, structure_title
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `);
      for (const block of normalized.blocks) {
        insertBlock.run(
          sourceVersion,
          block.id,
          block.canonicalStart,
          block.canonicalEnd,
          block.sourceText,
          block.sourceHash,
          block.globalIndex,
          block.tokenCount,
          block.structureId,
          block.structureTitle,
        );
      }
      const result = this.#database.prepare(`
        UPDATE source_versions SET plan_fingerprint=?
        WHERE source_version=? AND plan_fingerprint IS NULL
      `).run(fingerprint, sourceVersion);
      if (Number(result.changes) !== 1) {
        throw new Error(`derived plan changed concurrently for ${sourceVersion}`);
      }
      this.#appendEvent(null, "derived_plan_registered", { sourceVersion, fingerprint });
    });
  }

  createTranslationRun(meta: TranslationRunMeta): string {
    const runId = meta.runId === undefined ? randomUUID() : requireNonempty(meta.runId, "runId");
    const sourceVersion = requireNonempty(meta.sourceVersion, "sourceVersion");
    const source = this.#source(sourceVersion);
    if (source.plan_fingerprint === null) {
      throw new Error(`source ${sourceVersion} has no derived plan`);
    }
    const protocolVersion = requireNonempty(meta.protocolVersion, "protocolVersion");
    const modelId = requireNonempty(meta.modelId, "modelId");
    const snapshotId = requireNonempty(meta.initialSnapshotId, "initialSnapshotId");
    const metadata = jsonText(meta.metadata ?? {}, "translation run metadata");
    const emptySnapshot = "[]";

    this.#transaction(() => {
      this.#database.prepare(`
        INSERT INTO translation_runs(
          run_id, source_version, protocol_version, model_id, metadata_json
        ) VALUES(?, ?, ?, ?, ?)
      `).run(runId, sourceVersion, protocolVersion, modelId, metadata);
      this.#database.prepare(`
        INSERT INTO knowledge_snapshots(
          run_id, snapshot_id, content_hash, payload_json
        ) VALUES(?, ?, ?, ?)
      `).run(runId, snapshotId, hashText(emptySnapshot), emptySnapshot);
      this.#appendEvent(runId, "translation_run_created", {
        runId, sourceVersion, protocolVersion, modelId, snapshotId,
      });
    });
    return runId;
  }

  initializeWindowPlan(runId: string, windows: readonly BookWindowPlan[]): void {
    const run = this.#run(runId);
    const existing = one<{ count: number }>(
      this.#database.prepare("SELECT COUNT(*) AS count FROM window_plans WHERE run_id=?"),
      runId,
    )?.count ?? 0;
    if (existing !== 0) {
      throw new Error(`window plan already initialized for run ${runId}`);
    }
    const normalized = this.#validateWindows(run, windows);

    this.#transaction(() => {
      const insertWindow = this.#database.prepare(`
        INSERT INTO window_plans(
          run_id, window_id, source_version, ordinal, chapter_id, chapter_title,
          source_tokens, source_chars, oversized
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
      `);
      const insertMembership = this.#database.prepare(`
        INSERT INTO window_membership(
          run_id, window_id, source_version, block_id, position
        ) VALUES(?, ?, ?, ?, ?)
      `);
      for (const window of normalized) {
        insertWindow.run(
          runId,
          window.windowId,
          run.source_version,
          window.ordinal,
          window.chapterId,
          window.chapterTitle,
          window.sourceTokens,
          window.sourceChars,
          window.oversized ? 1 : 0,
        );
        for (let position = 0; position < window.blockIds.length; position += 1) {
          insertMembership.run(
            runId,
            window.windowId,
            run.source_version,
            window.blockIds[position],
            position,
          );
        }
      }
      this.#database.prepare(
        "UPDATE translation_runs SET status='running' WHERE run_id=? AND status='created'",
      ).run(runId);
      this.#appendEvent(runId, "window_plan_initialized", {
        runId,
        windows: normalized.map((window) => window.windowId),
      });
    });
  }

  claimWindow(runId: string, windowId: string): PersistedLosslessWindow {
    requireNonempty(runId, "runId");
    requireNonempty(windowId, "windowId");
    return this.#transaction(() => {
      const result = this.#database.prepare(`
        UPDATE window_plans
        SET status='running', attempt_count=attempt_count+1,
            last_error='', updated_at=datetime('now')
        WHERE run_id=? AND window_id=? AND status='pending'
      `).run(runId, windowId);
      if (Number(result.changes) !== 1) {
        const exists = one<{ present: number }>(
          this.#database.prepare(`
            SELECT 1 AS present FROM window_plans WHERE run_id=? AND window_id=?
          `),
          runId,
          windowId,
        );
        if (exists === undefined) {
          throw new Error(`run ${runId} has no window ${windowId}`);
        }
        throw new Error(`window is not pending: ${runId}/${windowId}`);
      }
      this.#appendEvent(runId, "window_claimed", { runId, windowId });
      return this.#window(runId, windowId) as PersistedLosslessWindow;
    });
  }

  stageWindow(input: WindowStageInput): void {
    requireNonempty(input.runId, "runId");
    requireNonempty(input.windowId, "windowId");
    requireNonempty(input.snapshotId, "snapshotId");
    if (input.status !== "completed" && input.status !== "completed_with_warnings") {
      throw new TypeError("stage status must be completed or completed_with_warnings");
    }
    if (typeof input.styleTail !== "string") {
      throw new TypeError("styleTail must be a string");
    }
    const budgetJson = validateBudget(input.budget);
    const warningsJson = validateWarnings(input.warnings);
    const candidates = input.knowledgeCandidates.map((candidate) => ({
      recordId: requireNonempty(candidate.recordId, "knowledge recordId"),
      normalizedSubject: requireNonempty(candidate.normalizedSubject, "normalizedSubject"),
      kind: requireNonempty(candidate.kind, "knowledge kind"),
      payloadJson: jsonText(candidate.payload, "knowledge payload"),
    }));
    if (new Set(candidates.map((candidate) => candidate.recordId)).size !== candidates.length) {
      throw new Error("duplicate knowledge recordId in staged window");
    }

    this.#transaction(() => {
      const window = this.#window(input.runId, input.windowId);
      if (window === undefined) {
        throw new Error(`run ${input.runId} has no window ${input.windowId}`);
      }
      const snapshot = one<{ run_id: string }>(
        this.#database.prepare(`
          SELECT run_id FROM knowledge_snapshots WHERE run_id=? AND snapshot_id=?
        `),
        input.runId,
        input.snapshotId,
      );
      if (snapshot === undefined) {
        const other = one<{ run_id: string }>(
          this.#database.prepare("SELECT run_id FROM knowledge_snapshots WHERE snapshot_id=? LIMIT 1"),
          input.snapshotId,
        );
        if (other !== undefined) {
          throw new Error(`snapshot ${input.snapshotId} belongs to another run`);
        }
        throw new Error(`snapshot ${input.snapshotId} does not belong to run ${input.runId}`);
      }
      if (window.status !== "running") {
        throw new Error(`window is not running: ${input.runId}/${input.windowId}`);
      }
      const expected = this.#membership(input.runId, input.windowId);
      this.#validateTranslations(input.translations, expected);

      const insertTranslation = this.#database.prepare(`
        INSERT INTO translations(
          run_id, window_id, source_version, block_id, version, source_hash,
          text, result_status, stage_state, active, snapshot_id
        ) VALUES(
          ?, ?, ?, ?,
          COALESCE((SELECT MAX(version)+1 FROM translations WHERE run_id=? AND block_id=?), 1),
          ?, ?, ?, 'staged', 0, ?
        )
      `);
      for (const translation of input.translations) {
        const block = expected.get(translation.blockId) as LogicalBlockRow;
        insertTranslation.run(
          input.runId,
          input.windowId,
          block.source_version,
          translation.blockId,
          input.runId,
          translation.blockId,
          translation.sourceHash,
          translation.text,
          input.status,
          input.snapshotId,
        );
      }

      const insertKnowledge = this.#database.prepare(`
        INSERT INTO knowledge_records(
          run_id, record_id, revision, window_id, snapshot_id,
          normalized_subject, kind, payload_json, status, active
        ) VALUES(
          ?, ?,
          COALESCE((SELECT MAX(revision)+1 FROM knowledge_records WHERE run_id=? AND record_id=?), 1),
          ?, ?, ?, ?, ?, 'candidate', 0
        )
      `);
      for (const candidate of candidates) {
        insertKnowledge.run(
          input.runId,
          candidate.recordId,
          input.runId,
          candidate.recordId,
          input.windowId,
          input.snapshotId,
          candidate.normalizedSubject,
          candidate.kind,
          candidate.payloadJson,
        );
      }

      const result = this.#database.prepare(`
        UPDATE window_plans
        SET status='staged', result_status=?, snapshot_id=?, style_tail=?,
            budget_json=?, warnings_json=?, updated_at=datetime('now')
        WHERE run_id=? AND window_id=? AND status='running'
      `).run(
        input.status,
        input.snapshotId,
        input.styleTail,
        budgetJson,
        warningsJson,
        input.runId,
        input.windowId,
      );
      if (Number(result.changes) !== 1) {
        throw new Error(`failed to stage running window ${input.runId}/${input.windowId}`);
      }
      this.#appendEvent(input.runId, "window_staged", {
        runId: input.runId,
        windowId: input.windowId,
        snapshotId: input.snapshotId,
        blocks: input.translations.map((translation) => translation.blockId),
      });
    });
  }

  promoteStagedWindow(runId: string, windowId: string): void {
    requireNonempty(runId, "runId");
    requireNonempty(windowId, "windowId");
    this.#transaction(() => {
      const row = one<WindowRow & { result_status: string | null }>(
        this.#database.prepare(`
          SELECT * FROM window_plans WHERE run_id=? AND window_id=?
        `),
        runId,
        windowId,
      );
      if (row === undefined) {
        throw new Error(`run ${runId} has no window ${windowId}`);
      }
      if (row.status !== "staged") {
        throw new Error(`window is not staged: ${runId}/${windowId}`);
      }
      if (row.snapshot_id === null || row.result_status === null) {
        throw new Error(`staged window is missing snapshot or result status: ${runId}/${windowId}`);
      }
      const snapshot = one<{ present: number }>(
        this.#database.prepare(`
          SELECT 1 AS present FROM knowledge_snapshots WHERE run_id=? AND snapshot_id=?
        `),
        runId,
        row.snapshot_id,
      );
      if (snapshot === undefined) {
        throw new Error(`snapshot ${row.snapshot_id} does not belong to run ${runId}`);
      }

      const expected = this.#membership(runId, windowId);
      const staged = all<{
        translation_id: number;
        block_id: string;
        source_version: string;
        source_hash: string;
        text: string;
        active: number;
        stage_state: string;
        snapshot_id: string;
      }>(this.#database.prepare(`
        SELECT translation_id, block_id, source_version, source_hash, text,
               active, stage_state, snapshot_id
        FROM translations
        WHERE run_id=? AND window_id=? AND stage_state='staged'
        ORDER BY translation_id
      `), runId, windowId);
      if (staged.length !== expected.size) {
        throw new Error(`staged window expected ${expected.size} translations but found ${staged.length}`);
      }
      const seen = new Set<string>();
      for (const translation of staged) {
        const block = expected.get(translation.block_id);
        if (block === undefined || seen.has(translation.block_id)) {
          throw new Error(`staged window contains unknown or duplicate block ${translation.block_id}`);
        }
        seen.add(translation.block_id);
        if (translation.source_hash !== block.source_hash) {
          throw new Error(`source hash mismatch for block ${translation.block_id}`);
        }
        if (translation.source_version !== block.source_version) {
          throw new Error(`source version mismatch for block ${translation.block_id}`);
        }
        if (translation.text.trim().length === 0) {
          throw new Error(`empty staged translation for block ${translation.block_id}`);
        }
        if (translation.active !== 0 || translation.stage_state !== "staged") {
          throw new Error(`translation is not safely staged for block ${translation.block_id}`);
        }
        if (translation.snapshot_id !== row.snapshot_id) {
          throw new Error(`translation snapshot mismatch for block ${translation.block_id}`);
        }
      }

      for (const blockId of expected.keys()) {
        this.#database.prepare(`
          UPDATE translations SET active=0
          WHERE run_id=? AND block_id=? AND active=1
        `).run(runId, blockId);
      }
      const promoted = this.#database.prepare(`
        UPDATE translations SET active=1, stage_state='promoted'
        WHERE run_id=? AND window_id=? AND stage_state='staged' AND active=0
      `).run(runId, windowId);
      if (Number(promoted.changes) !== expected.size) {
        throw new Error(`failed to promote complete window ${runId}/${windowId}`);
      }
      this.#database.prepare(`
        UPDATE knowledge_records SET active=1
        WHERE run_id=? AND window_id=? AND snapshot_id=? AND active=0
      `).run(runId, windowId, row.snapshot_id);
      const completed = this.#database.prepare(`
        UPDATE window_plans SET status=result_status, updated_at=datetime('now')
        WHERE run_id=? AND window_id=? AND status='staged'
      `).run(runId, windowId);
      if (Number(completed.changes) !== 1) {
        throw new Error(`failed to complete staged window ${runId}/${windowId}`);
      }
      this.#appendEvent(runId, "window_promoted", {
        runId, windowId, snapshotId: row.snapshot_id,
      });
    });
  }

  failWindow(runId: string, windowId: string, failure: WindowFailureInput): void {
    requireNonempty(runId, "runId");
    requireNonempty(windowId, "windowId");
    if (typeof failure.error !== "string" || failure.error.trim().length === 0) {
      throw new TypeError("failure error must be nonempty");
    }
    const budgetJson = validateBudget(failure.budget);
    const warningsJson = validateWarnings(failure.warnings);
    const status = failure.retry ? "pending" : "human_required";
    this.#transaction(() => {
      const result = this.#database.prepare(`
        UPDATE window_plans
        SET status=?, budget_json=?, warnings_json=?, last_error=?,
            updated_at=datetime('now')
        WHERE run_id=? AND window_id=? AND status='running'
      `).run(
        status,
        budgetJson,
        warningsJson,
        failure.error.slice(0, 4_000),
        runId,
        windowId,
      );
      if (Number(result.changes) !== 1) {
        const exists = one<{ present: number }>(
          this.#database.prepare(`
            SELECT 1 AS present FROM window_plans WHERE run_id=? AND window_id=?
          `),
          runId,
          windowId,
        );
        if (exists === undefined) {
          throw new Error(`run ${runId} has no window ${windowId}`);
        }
        throw new Error(`window is not running: ${runId}/${windowId}`);
      }
      this.#appendEvent(runId, "window_failed", {
        runId,
        windowId,
        retry: failure.retry,
        status,
        error: failure.error.slice(0, 1_000),
      });
    });
  }

  activeTranslations(runId: string): ActiveLosslessTranslation[] {
    this.#run(runId);
    return all<{
      run_id: string;
      window_id: string;
      block_id: string;
      source_version: string;
      source_hash: string;
      text: string;
      result_status: "completed" | "completed_with_warnings";
      version: number;
    }>(this.#database.prepare(`
      SELECT t.run_id, t.window_id, t.block_id, t.source_version, t.source_hash,
             t.text, t.result_status, t.version
      FROM translations AS t
      JOIN logical_blocks AS b
        ON b.source_version=t.source_version AND b.block_id=t.block_id
      WHERE t.run_id=? AND t.active=1
      ORDER BY b.global_index
    `), runId).map((row) => ({
      runId: row.run_id,
      windowId: row.window_id,
      blockId: row.block_id,
      sourceVersion: row.source_version,
      sourceHash: row.source_hash,
      text: row.text,
      status: row.result_status,
      version: row.version,
    }));
  }

  knowledgeHistory(runId: string): KnowledgeHistoryRecord[] {
    this.#run(runId);
    return all<{
      run_id: string;
      record_id: string;
      revision: number;
      window_id: string;
      snapshot_id: string;
      normalized_subject: string;
      kind: string;
      payload_json: string;
      status: string;
      active: number;
    }>(this.#database.prepare(`
      SELECT * FROM knowledge_records WHERE run_id=? ORDER BY record_id, revision
    `), runId).map((row) => ({
      runId: row.run_id,
      recordId: row.record_id,
      revision: row.revision,
      windowId: row.window_id,
      snapshotId: row.snapshot_id,
      normalizedSubject: row.normalized_subject,
      kind: row.kind,
      payload: JSON.parse(row.payload_json) as unknown,
      status: row.status,
      active: Boolean(row.active),
    }));
  }

  auditRows(runId: string): AuditProjection {
    const run = this.#run(runId);
    const windows = all<WindowRow>(
      this.#database.prepare("SELECT * FROM window_plans WHERE run_id=? ORDER BY ordinal"),
      runId,
    ).map((row) => windowFromRow(row, this.#membershipIds(runId, row.window_id)));
    for (const window of windows) {
      window.globalIndexes = all<{ global_index: number }>(this.#database.prepare(`
        SELECT b.global_index
        FROM window_membership AS m
        JOIN logical_blocks AS b
          ON b.source_version=m.source_version AND b.block_id=m.block_id
        WHERE m.run_id=? AND m.window_id=? ORDER BY m.position
      `), runId, window.windowId).map((row) => row.global_index);
    }
    const snapshotIds = all<{ snapshot_id: string }>(this.#database.prepare(`
      SELECT snapshot_id FROM knowledge_snapshots WHERE run_id=? ORDER BY created_at, snapshot_id
    `), runId).map((row) => row.snapshot_id);
    return {
      runId: run.run_id,
      sourceVersion: run.source_version,
      protocolVersion: run.protocol_version,
      modelId: run.model_id,
      status: run.status,
      windows,
      translations: this.activeTranslations(runId),
      knowledge: this.knowledgeHistory(runId),
      snapshotIds,
    };
  }

  close(): void {
    this.#database.close();
  }

  #validateSource(input: CertifiedSourceInput): CertifiedSourceInput {
    const sourceVersion = requireNonempty(input.sourceVersion, "sourceVersion");
    const rawSha256 = requireNonempty(input.rawSha256, "rawSha256");
    const canonicalSha256 = requireNonempty(input.canonicalSha256, "canonicalSha256");
    const canonicalChars = requireSafeInteger(input.canonicalChars, "canonicalChars");
    if (input.coordinateUnit !== "unicode_scalar") {
      throw new TypeError("coordinateUnit must be unicode_scalar");
    }
    const sourceFormat = requireNonempty(input.sourceFormat, "sourceFormat");
    const encoding = requireNonempty(input.encoding, "encoding");
    const extractor = requireNonempty(input.extractor, "extractor");
    if (!Array.isArray(input.ranges)) {
      throw new TypeError("source ranges must be an array");
    }
    const ids = new Set<string>();
    let cursor = 0;
    const ranges = input.ranges.map((range, index) => {
      const rangeId = requireNonempty(range.rangeId, `range[${index}].rangeId`);
      if (ids.has(rangeId)) {
        throw new Error(`duplicate source range id ${rangeId}`);
      }
      ids.add(rangeId);
      const canonicalStart = requireSafeInteger(range.canonicalStart, `range[${index}].canonicalStart`);
      const canonicalEnd = requireSafeInteger(range.canonicalEnd, `range[${index}].canonicalEnd`);
      if (canonicalStart !== cursor || canonicalEnd < canonicalStart || canonicalEnd > canonicalChars) {
        throw new Error(`source ranges must cover canonical coordinates continuously at ${cursor}`);
      }
      cursor = canonicalEnd;
      const hasRawStart = range.rawStart !== undefined;
      const hasRawEnd = range.rawEnd !== undefined;
      if (hasRawStart !== hasRawEnd) {
        throw new Error(`source range ${rangeId} must provide both rawStart and rawEnd`);
      }
      const rawStart = hasRawStart
        ? requireSafeInteger(range.rawStart as number, `range[${index}].rawStart`)
        : undefined;
      const rawEnd = hasRawEnd
        ? requireSafeInteger(range.rawEnd as number, `range[${index}].rawEnd`)
        : undefined;
      if (rawStart !== undefined && rawEnd !== undefined && rawEnd < rawStart) {
        throw new Error(`source range ${rangeId} has reversed raw coordinates`);
      }
      return {
        rangeId,
        canonicalStart,
        canonicalEnd,
        originKind: requireNonempty(range.originKind, `range[${index}].originKind`),
        originRef: requireNonempty(range.originRef, `range[${index}].originRef`),
        transformation: requireNonempty(range.transformation, `range[${index}].transformation`),
        ...(rawStart === undefined ? {} : { rawStart, rawEnd: rawEnd as number }),
      };
    });
    if (cursor !== canonicalChars) {
      throw new Error(`source ranges cover ${cursor} canonical characters, expected ${canonicalChars}`);
    }
    return {
      sourceVersion,
      rawSha256,
      canonicalSha256,
      canonicalChars,
      coordinateUnit: input.coordinateUnit,
      sourceFormat,
      encoding,
      extractor,
      ranges,
    };
  }

  #validateDerivedPlan(source: SourceVersionRow, plan: DerivedPlan): DerivedPlan {
    if (!Array.isArray(plan.annotations) || !Array.isArray(plan.blocks)) {
      throw new TypeError("derived plan annotations and blocks must be arrays");
    }
    const annotationIds = new Set<string>();
    const annotations = plan.annotations.map((annotation, index) => {
      const id = requireNonempty(annotation.id, `annotation[${index}].id`);
      if (annotationIds.has(id)) {
        throw new Error(`duplicate annotation id ${id}`);
      }
      annotationIds.add(id);
      const start = requireSafeInteger(annotation.start, `annotation[${index}].start`);
      const end = requireSafeInteger(annotation.end, `annotation[${index}].end`);
      if (end < start || end > source.canonical_chars) {
        throw new Error(`annotation ${id} is outside canonical source`);
      }
      if (!Number.isFinite(annotation.boundaryWeight) || annotation.boundaryWeight < 0) {
        throw new TypeError(`annotation ${id} boundaryWeight must be finite and non-negative`);
      }
      return {
        id,
        kind: annotation.kind,
        start,
        end,
        title: annotation.title,
        boundaryWeight: annotation.boundaryWeight,
      };
    });
    if (plan.blocks.length === 0 && source.canonical_chars > 0) {
      throw new Error("derived plan has no logical blocks");
    }
    const blockIds = new Set<string>();
    let cursor = 0;
    const blocks = plan.blocks.map((block, index) => {
      const id = requireNonempty(block.id, `block[${index}].id`);
      if (blockIds.has(id)) {
        throw new Error(`duplicate block id ${id}`);
      }
      blockIds.add(id);
      if (block.sourceVersion !== source.source_version) {
        throw new Error(`block ${id} source version does not match ${source.source_version}`);
      }
      const start = requireSafeInteger(block.canonicalStart, `block[${index}].canonicalStart`);
      const end = requireSafeInteger(block.canonicalEnd, `block[${index}].canonicalEnd`);
      if (start !== cursor || end <= start || end > source.canonical_chars) {
        throw new Error(`logical blocks must cover canonical source continuously at ${cursor}`);
      }
      cursor = end;
      requireNonempty(block.sourceText, `block[${index}].sourceText`);
      if (scalarLength(block.sourceText) !== end - start) {
        throw new Error(`block ${id} source text length does not match scalar range`);
      }
      requireNonempty(block.sourceHash, `block[${index}].sourceHash`);
      const globalIndex = requireSafeInteger(block.globalIndex, `block[${index}].globalIndex`);
      if (globalIndex !== index) {
        throw new Error("logical block global indexes must be unique and continuous");
      }
      requireSafeInteger(block.tokenCount, `block[${index}].tokenCount`);
      if (block.structureId !== null && !annotationIds.has(block.structureId)) {
        throw new Error(`block ${id} references unknown structure ${block.structureId}`);
      }
      return {
        id,
        sourceVersion: block.sourceVersion,
        canonicalStart: start,
        canonicalEnd: end,
        sourceText: block.sourceText,
        sourceHash: block.sourceHash,
        globalIndex,
        tokenCount: block.tokenCount,
        structureId: block.structureId,
        structureTitle: block.structureTitle,
      };
    });
    if (cursor !== source.canonical_chars) {
      throw new Error(`logical blocks cover ${cursor} canonical characters, expected ${source.canonical_chars}`);
    }
    return { annotations, blocks };
  }

  #validateWindows(run: RunRow, windows: readonly BookWindowPlan[]): BookWindowPlan[] {
    if (!Array.isArray(windows) || windows.length === 0) {
      throw new Error("window plan must not be empty");
    }
    const blocks = all<LogicalBlockRow>(this.#database.prepare(`
      SELECT block_id, source_version, source_hash, global_index,
             canonical_start, canonical_end, token_count
      FROM logical_blocks WHERE source_version=? ORDER BY global_index
    `), run.source_version);
    const blockById = new Map(blocks.map((block) => [block.block_id, block]));
    const sorted = [...windows].sort((left, right) => left.ordinal - right.ordinal);
    const windowIds = new Set<string>();
    const memberIds = new Set<string>();
    let nextGlobalIndex = 0;
    for (let index = 0; index < sorted.length; index += 1) {
      const window = sorted[index] as BookWindowPlan;
      const windowId = requireNonempty(window.windowId, `window[${index}].windowId`);
      if (windowIds.has(windowId)) {
        throw new Error(`duplicate window id ${windowId}`);
      }
      windowIds.add(windowId);
      if (requireSafeInteger(window.ordinal, `window ${windowId} ordinal`) !== index) {
        throw new Error("window ordinals must be unique and continuous from zero");
      }
      requireNonempty(window.chapterId, `window ${windowId} chapterId`);
      if (!Array.isArray(window.blockIds) || window.blockIds.length === 0) {
        throw new Error(`window ${windowId} must contain blocks`);
      }
      if (!Array.isArray(window.globalIndexes)
        || window.globalIndexes.length !== window.blockIds.length) {
        throw new Error(`window ${windowId} global indexes must match block membership`);
      }
      let calculatedTokens = 0;
      let calculatedChars = 0;
      for (let position = 0; position < window.blockIds.length; position += 1) {
        const blockId = requireNonempty(window.blockIds[position] as string, `window ${windowId} blockId`);
        if (memberIds.has(blockId)) {
          throw new Error(`duplicate block membership ${blockId}`);
        }
        memberIds.add(blockId);
        const block = blockById.get(blockId);
        if (block === undefined) {
          throw new Error(`unknown block ${blockId} in window ${windowId}`);
        }
        if (window.globalIndexes[position] !== block.global_index) {
          throw new Error(`window ${windowId} global index mismatch for block ${blockId}`);
        }
        if (block.global_index !== nextGlobalIndex) {
          throw new Error(`window membership is not in source order at block ${blockId}`);
        }
        nextGlobalIndex += 1;
        calculatedTokens += block.token_count;
        calculatedChars += block.canonical_end - block.canonical_start;
      }
      requireSafeInteger(window.sourceTokens, `window ${windowId} sourceTokens`);
      requireSafeInteger(window.sourceChars, `window ${windowId} sourceChars`);
      if (window.sourceTokens !== calculatedTokens || window.sourceChars !== calculatedChars) {
        throw new Error(`window ${windowId} token or character totals do not match membership`);
      }
      if (typeof window.oversized !== "boolean") {
        throw new TypeError(`window ${windowId} oversized must be boolean`);
      }
    }
    if (memberIds.size !== blocks.length) {
      throw new Error(`window plan does not provide complete block membership: ${memberIds.size}/${blocks.length}`);
    }
    return sorted;
  }

  #validateTranslations(
    translations: readonly StagedTranslationInput[],
    expected: ReadonlyMap<string, LogicalBlockRow>,
  ): void {
    if (!Array.isArray(translations)) {
      throw new TypeError("translations must be an array");
    }
    const seen = new Set<string>();
    for (const translation of translations) {
      const blockId = requireNonempty(translation.blockId, "translation blockId");
      if (seen.has(blockId)) {
        throw new Error(`duplicate translation for block ${blockId}`);
      }
      seen.add(blockId);
      const block = expected.get(blockId);
      if (block === undefined) {
        throw new Error(`unknown translated block ${blockId}`);
      }
      if (translation.sourceHash !== block.source_hash) {
        throw new Error(`source hash mismatch for block ${blockId}`);
      }
      if (typeof translation.text !== "string" || translation.text.trim().length === 0) {
        throw new Error(`empty translation for block ${blockId}`);
      }
    }
    if (seen.size !== expected.size) {
      throw new Error(`window requires ${expected.size} translations but received ${seen.size}`);
    }
  }

  #source(sourceVersion: string): SourceVersionRow {
    requireNonempty(sourceVersion, "sourceVersion");
    const source = one<SourceVersionRow>(
      this.#database.prepare("SELECT * FROM source_versions WHERE source_version=?"),
      sourceVersion,
    );
    if (source === undefined) {
      throw new Error(`unknown source version ${sourceVersion}`);
    }
    return source;
  }

  #run(runId: string): RunRow {
    requireNonempty(runId, "runId");
    const run = one<RunRow>(
      this.#database.prepare("SELECT * FROM translation_runs WHERE run_id=?"),
      runId,
    );
    if (run === undefined) {
      throw new Error(`unknown translation run ${runId}`);
    }
    return run;
  }

  #window(runId: string, windowId: string): PersistedLosslessWindow | undefined {
    const row = one<WindowRow>(
      this.#database.prepare("SELECT * FROM window_plans WHERE run_id=? AND window_id=?"),
      runId,
      windowId,
    );
    if (row === undefined) {
      return undefined;
    }
    const window = windowFromRow(row, this.#membershipIds(runId, windowId));
    window.globalIndexes = this.#globalIndexes(runId, windowId);
    return window;
  }

  #membershipIds(runId: string, windowId: string): string[] {
    return all<{ block_id: string }>(this.#database.prepare(`
      SELECT block_id FROM window_membership
      WHERE run_id=? AND window_id=? ORDER BY position
    `), runId, windowId).map((row) => row.block_id);
  }

  #globalIndexes(runId: string, windowId: string): number[] {
    return all<{ global_index: number }>(this.#database.prepare(`
      SELECT b.global_index
      FROM window_membership AS m
      JOIN logical_blocks AS b
        ON b.source_version=m.source_version AND b.block_id=m.block_id
      WHERE m.run_id=? AND m.window_id=? ORDER BY m.position
    `), runId, windowId).map((row) => row.global_index);
  }

  #membership(runId: string, windowId: string): Map<string, LogicalBlockRow> {
    const rows = all<LogicalBlockRow>(this.#database.prepare(`
      SELECT b.block_id, b.source_version, b.source_hash, b.global_index,
             b.canonical_start, b.canonical_end, b.token_count
      FROM window_membership AS m
      JOIN logical_blocks AS b
        ON b.source_version=m.source_version AND b.block_id=m.block_id
      WHERE m.run_id=? AND m.window_id=? ORDER BY m.position
    `), runId, windowId);
    return new Map(rows.map((row) => [row.block_id, row]));
  }

  #appendEvent(runId: string | null, kind: string, payload: unknown): void {
    this.#database.prepare(`
      INSERT INTO events(run_id, kind, payload_json) VALUES(?, ?, ?)
    `).run(runId, requireNonempty(kind, "event kind"), jsonText(payload, "event payload"));
  }

  #transaction<T>(operation: () => T): T {
    this.#database.exec("BEGIN IMMEDIATE");
    try {
      const result = operation();
      this.#database.exec("COMMIT");
      return result;
    } catch (error) {
      this.#database.exec("ROLLBACK");
      throw error;
    }
  }
}
