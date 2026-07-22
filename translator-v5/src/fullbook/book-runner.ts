import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { join, resolve } from "node:path";

import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import {
  collectWindowAnchorCandidates,
  LexicalAnchorer,
  type LexicalAnchor,
  type LexicalAnchorOutcome,
} from "../agents/lexical-anchorer.js";
import { ModelProviderError, PiRuntime } from "../agents/pi-runtime.js";
import { runTranslationBatch } from "../agents/translation-batch.js";
import type { TranslationRequestInput } from "../agents/translation-request.js";
import { entityLinkAsTerms, type EntityLink } from "../domain/entity-links.js";
import type { StableTerm, V4Block } from "../domain/types.js";
import {
  relevantGlossaryTerms,
  type LoadedGlossary,
} from "../glossary/glossary-profile.js";
import { DEFAULT_BUDGET_LIMITS } from "../kernel/budget.js";
import { BudgetLedger } from "../kernel/budget.js";
import { RunLease } from "../kernel/run-lease.js";
import {
  canonicalJson,
  KnowledgeStore,
  type KnowledgeCandidate,
} from "../knowledge/knowledge-store.js";
import { createKnowledgeSnapshot } from "../knowledge/snapshot.js";
import { SourceLedger } from "../source/source-ledger.js";
import { WeightedTokenEstimator } from "../source/token-estimator.js";
import { analyzeSourceAnomalies } from "../source/anomaly-report.js";
import {
  runTranslationWindow,
  type PilotResult,
} from "../pilot-runner.js";
import { writeBookArtifacts, type BookArtifactPaths } from "../report.js";
import {
  BookStore,
  type BookStatusSummary,
  type PersistedWindow,
} from "../storage/book-store.js";
import {
  LosslessBookStore,
  type LosslessBookStatusSummary,
  type PersistedLosslessWindow,
} from "../storage/lossless-book-store.js";
import type { TranslationMemoryCandidate } from "../tools/candidate-collector.js";
import type { StyleState } from "../tools/translation-tools.js";
import {
  composeEffectiveStyle,
  createBookStyleConstitution,
} from "../style/effective-style.js";
import { createStyleObservation } from "../style/style-observation.js";
import { projectEffectiveStyle } from "../style/style-projection.js";
import type {
  BookStyleConstitution,
  EffectiveStyleProjection,
  VoiceProfile,
} from "../style/types.js";
import { BookContext } from "./book-context.js";
import {
  AdaptiveScheduler,
  type SchedulerObservationStatus,
} from "./adaptive-scheduler.js";
import { CommitCoordinator } from "./commit-coordinator.js";
import {
  boundedActiveTail,
  memoriesFromSnapshot,
} from "./memory-projection.js";
import type { WindowExecutionSummary } from "./types.js";
import type {
  PhysicalRequestPlan,
  TranslationRuntime,
  TranslationRuntimeSet,
} from "./types.js";
import { packPhysicalRequests } from "./request-batcher.js";
import {
  RequestBudgeter,
  type RequestBudgetAssessment,
} from "./request-budgeter.js";
import {
  nextConcurrency,
  planBookWindows,
  type WindowPlanOptions,
} from "./window-planner.js";

export const LOSSLESS_BOOK_PROTOCOL_VERSION = "v5-book-3";
const DEFAULT_PROTOCOL_VERSION = LOSSLESS_BOOK_PROTOCOL_VERSION;
const DEFAULT_MAX_ATTEMPTS = 2;
const DEFAULT_MAX_CONCURRENCY = 2;
const FAST_TARGET_SOURCE_TOKENS = 3_200;
const FAST_MAX_SOURCE_TOKENS = 4_800;
const FAST_MAX_BLOCKS = 4;

export class BookStorageIncidentError extends Error {
  readonly code = "STORAGE_LOCKED" as const;
  readonly retryable = true;

  constructor(cause: unknown) {
    super(`STORAGE_LOCKED: ${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = "BookStorageIncidentError";
  }
}

class BookSourceVersionChangedError extends Error {
  readonly code = "SOURCE_VERSION_CHANGED" as const;

  constructor(message: string) {
    super(`SOURCE_VERSION_CHANGED: ${message}`);
    this.name = "BookSourceVersionChangedError";
  }
}

function isStorageLocked(error: unknown): boolean {
  return error instanceof Error
    && /(?:SQLITE_BUSY|database(?: table)? is locked)/iu.test(error.message);
}

function assertSourceVersionUnchanged(context: BookContext): void {
  const expected = context.sourceLedger.sourceVersion;
  try {
    const current = SourceLedger.open(context.sourceLedger.manifestPath);
    if (current.sourceVersion !== expected) {
      throw new BookSourceVersionChangedError(
        `expected ${expected}, found ${current.sourceVersion}`,
      );
    }
  } catch (error) {
    if (error instanceof BookSourceVersionChangedError) {
      throw error;
    }
    throw new BookSourceVersionChangedError(
      error instanceof Error ? error.message : String(error),
    );
  }
}

function runMetadataWithLanguageProfile(
  metadata: unknown,
  context: BookContext,
  runtimeSet?: TranslationRuntimeSet,
): Record<string, unknown> {
  const userMetadata = typeof metadata === "object"
    && metadata !== null
    && !Array.isArray(metadata)
    ? metadata as Record<string, unknown>
    : metadata === undefined
      ? {}
      : { userMetadata: metadata };
  return {
    ...userMetadata,
    sourceLanguageProfile: {
      id: context.languageProfile.id,
      version: context.languageProfile.version,
      compatibilityMode: context.sourceLedger.sourceLanguageCompatibilityMode,
    },
    sourceAnomalies: analyzeSourceAnomalies(context.sourceLedger.sourceText),
    ...(runtimeSet === undefined ? {} : {
      translationRuntime: {
        mode: runtimeSet.mode,
        primary: {
          modelId: runtimeSet.primary.model.id,
          ...(runtimeSet.primary.effort === undefined
            ? {}
            : { effort: runtimeSet.primary.effort }),
          ...(runtimeSet.primary.thinkingLevel === undefined
            ? {}
            : { thinkingLevel: runtimeSet.primary.thinkingLevel }),
        },
        escalation: {
          modelId: runtimeSet.escalation.model.id,
          ...(runtimeSet.escalation.effort === undefined
            ? {}
            : { effort: runtimeSet.escalation.effort }),
          ...(runtimeSet.escalation.thinkingLevel === undefined
            ? {}
            : { thinkingLevel: runtimeSet.escalation.thinkingLevel }),
        },
      },
    }),
  };
}

function runtimeMetadata(metadata: unknown): unknown | undefined {
  if (metadata === null || typeof metadata !== "object" || Array.isArray(metadata)) {
    return undefined;
  }
  return (metadata as Record<string, unknown>).translationRuntime;
}

function losslessStyleConstitution(style: StyleState | undefined): BookStyleConstitution {
  return createBookStyleConstitution({
    register: style?.register,
    sentencePolicy: style?.sentencePolicy,
    explicitation: style?.explicitation,
    imagery: style?.imagery,
    dialogue: style?.dialogue,
    technicalProse: style?.technicalProse,
    typography: style?.typography ?? style?.dialogueQuotes,
    additionalInstruction: style?.additionalInstruction,
  });
}

function losslessVoiceProfiles(style: StyleState | undefined): VoiceProfile[] {
  return [{
    voiceId: "narrator",
    scope: "main_narrator",
    instruction: style?.narratorVoice ?? "保持作品主叙述者既定视角、距离和信息显隐",
    confidence: 1,
  }];
}
const DEFAULT_WARMUP_WINDOWS = 2;

export interface BookPreflight {
  sourceFingerprint: string;
  blocks: number;
  chapters: number;
  windows: number;
  sourceTokens: number;
  sourceChars: number;
  oversizedWindows: number;
  sourceWarnings: string[];
}

export interface BookWaveReport {
  wave: number;
  concurrency: number;
  windowIds: string[];
}

export interface BookRunOptions {
  dbPath: string;
  storePath: string;
  outputDir: string;
  model: Model<any>;
  streamFn: StreamFn;
  windowOptions?: WindowPlanOptions;
  protocolVersion?: string;
  styleState?: StyleState;
  maxWindows?: number;
  warmupWindows?: number;
  maxConcurrency?: number;
  maxAttempts?: number;
  hardDeadlineMs?: number;
}

export interface BookRunResult {
  outcome: "completed" | "completed_with_warnings" | "human_required" | "partial";
  processedWindows: number;
  waves: BookWaveReport[];
  status: BookStatusSummary;
  windows: PersistedWindow[];
  wallTimeMs: number;
  leaseReleased: boolean;
  artifacts: BookArtifactPaths;
}

export interface LosslessBookRunMeta {
  runId: string;
  protocolVersion: string;
  modelId?: string;
  metadata?: unknown;
}

export interface LosslessBookRunOptions {
  manifestPath: string;
  legacyV4DbPath?: string;
  storePath: string;
  runMeta: LosslessBookRunMeta;
  model: Model<any>;
  streamFn: StreamFn;
  /** Optional explicit dual-runtime policy. Legacy callers remain quality mode. */
  runtimeSet?: TranslationRuntimeSet;
  windowOptions?: WindowPlanOptions;
  styleState?: StyleState;
  glossary?: LoadedGlossary;
  maxWindows?: number;
  maxConcurrency?: number;
  maxInFlightTokens?: number;
  maxAttempts?: number;
  hardDeadlineMs?: number;
  tinyWindowTokens?: number;
  maxRequestTokens?: number;
  maxWindowsPerRequest?: number;
  /**
   * Stops between durable window operations. A promotion that has already
   * entered the store remains atomic and is never interrupted mid-transaction.
   */
  signal?: AbortSignal;
}

export class BookRequestCapacityError extends Error {
  readonly code = "REQUEST_CONTEXT_EXCEEDED" as const;
  readonly retryable = false;

  constructor(requestId: string, assessment: RequestBudgetAssessment) {
    super(
      `REQUEST_CONTEXT_EXCEEDED: ${requestId} reserves ${assessment.totalReserved} tokens `
      + `for a ${assessment.contextWindowTokens}-token context`,
    );
    this.name = "BookRequestCapacityError";
  }
}

export interface LosslessBookRunResult {
  outcome: "completed" | "completed_with_warnings" | "human_required" | "partial";
  runId: string;
  processedWindows: number;
  waves: BookWaveReport[];
  status: LosslessBookStatusSummary;
  windows: PersistedLosslessWindow[];
  wallTimeMs: number;
  leaseReleased: boolean;
  artifacts: null;
}

interface AttemptResult {
  window: PersistedWindow;
  result?: PilotResult;
  error?: string;
  fatalProviderError?: boolean;
}

function nonNegativeInteger(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${name} must be a non-negative safe integer`);
  }
  return value;
}

function positiveInteger(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError(`${name} must be a positive safe integer`);
  }
  return value;
}

export function preflightBook(
  databasePath: string,
  windowOptions: WindowPlanOptions = {},
): BookPreflight {
  const context = BookContext.open(databasePath);
  try {
    const blocks = context.blocks;
    const windows = planBookWindows(blocks, windowOptions);
    const source = blocks.map((block) => block.sourceText).join("\n");
    const sourceWarnings: string[] = [];
    const replacements = [...source.matchAll(/�/gu)].length;
    const nulls = [...source.matchAll(/\0/gu)].length;
    if (replacements > 0) {
      sourceWarnings.push(`source contains ${replacements} replacement character(s)`);
    }
    if (nulls > 0) {
      sourceWarnings.push(`source contains ${nulls} NUL character(s)`);
    }
    return {
      sourceFingerprint: context.sourceFingerprint,
      blocks: blocks.length,
      chapters: new Set(blocks.map((block) => block.chapterId)).size,
      windows: windows.length,
      sourceTokens: blocks.reduce((total, block) => total + block.tokenCount, 0),
      sourceChars: blocks.reduce((total, block) => total + block.sourceText.length, 0),
      oversizedWindows: windows.filter((window) => window.oversized).length,
      sourceWarnings,
    };
  } finally {
    context.close();
  }
}

function normalizeForm(value: string): string {
  return value.normalize("NFKC").trim().toLocaleLowerCase();
}

function anchorConflict(
  existing: readonly LexicalAnchor[],
  proposed: readonly LexicalAnchor[],
): string | undefined {
  const bySource = new Map(existing.map((anchor) => [normalizeForm(anchor.sourceForm), anchor]));
  for (const anchor of proposed) {
    const prior = bySource.get(normalizeForm(anchor.sourceForm));
    if (prior === undefined) {
      continue;
    }
    if (prior.mode !== anchor.mode
      || (prior.mode === "stable" && prior.target !== anchor.target)) {
      return `lexical anchor conflict for ${anchor.sourceForm}`;
    }
  }
  return undefined;
}

function resultSummary(result: PilotResult): WindowExecutionSummary {
  return {
    status: result.status === "completed"
      ? "completed"
      : result.status === "completed_with_warnings"
        ? "completed_with_warnings"
        : "human_required",
    modelCalls: result.metrics.modelCalls,
    modelCallLimit: DEFAULT_BUDGET_LIMITS.modelCalls,
    repaired: result.audit.validations.some((item) => item.repaired),
    deadlineExceeded: result.metrics.degradedReasons.some((reason) =>
      reason.toLocaleLowerCase().includes("deadline")),
  };
}

async function runLegacyBook(options: BookRunOptions): Promise<BookRunResult> {
  const startedAt = performance.now();
  const maxWindows = nonNegativeInteger(
    options.maxWindows ?? Number.MAX_SAFE_INTEGER,
    "maxWindows",
  );
  const maxConcurrency = positiveInteger(
    options.maxConcurrency ?? DEFAULT_MAX_CONCURRENCY,
    "maxConcurrency",
  );
  const maxAttempts = positiveInteger(
    options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS,
    "maxAttempts",
  );
  const warmupWindows = nonNegativeInteger(
    options.warmupWindows ?? DEFAULT_WARMUP_WINDOWS,
    "warmupWindows",
  );
  const protocolVersion = options.protocolVersion ?? DEFAULT_PROTOCOL_VERSION;
  mkdirSync(options.outputDir, { recursive: true });
  const context = BookContext.open(options.dbPath);
  const store = new BookStore(options.storePath);
  const windows = planBookWindows(context.blocks, {
    ...options.windowOptions,
    protocolVersion,
  });
  store.initializePlan({
    sourceDbPath: options.dbPath,
    sourceFingerprint: context.sourceFingerprint,
    protocolVersion,
    modelId: options.model.id,
    blocks: context.blocks,
    windows,
  });
  const leasePath = `${resolve(options.storePath)}.run.lock`;
  const lease = RunLease.acquire(leasePath, `book:${context.sourceFingerprint}`);
  const waves: BookWaveReport[] = [];
  const history: WindowExecutionSummary[] = [];
  let processedWindows = 0;

  const runAttempt = async (pending: PersistedWindow): Promise<AttemptResult> => {
    const claimed = store.claimWindow(pending.windowId);
    try {
      const result = await runTranslationWindow({
        dbPath: options.dbPath,
        context,
        outputDir: join(
          options.outputDir,
          ".windows",
          claimed.windowId,
          `attempt-${claimed.attemptCount}`,
        ),
        outputPrefix: "window",
        globalIndexes: claimed.globalIndexes,
        model: options.model,
        streamFn: options.streamFn,
        translationConcurrency: 1,
        hardDeadlineMs: options.hardDeadlineMs,
        protocolVersion,
        persistedAnchors: store.loadLexicalAnchors(),
        persistedNarrativeMemories: store.loadNarrativeMemories(),
        previousActiveTail: store.loadStyleTail(),
        styleState: options.styleState,
        researchMode: "on_demand",
      });
      return { window: claimed, result };
    } catch (error) {
      return {
        window: claimed,
        error: error instanceof Error ? error.message : String(error),
        fatalProviderError: error instanceof ModelProviderError,
      };
    }
  };

  const finalizeAttempt = async (initial: AttemptResult): Promise<WindowExecutionSummary> => {
    let attempt = initial;
    while (true) {
      if (attempt.fatalProviderError) {
        const error = attempt.error ?? "external model provider failure";
        store.failWindow(attempt.window.windowId, {
          error,
          retry: true,
          budget: {},
          warnings: ["external model provider failure; run aborted without human task"],
        });
        throw new ModelProviderError(error);
      }
      const result = attempt.result;
      const conflict = result === undefined
        ? undefined
        : anchorConflict(store.loadLexicalAnchors(), result.audit.lexicalAnchors);
      const successful = result !== undefined
        && !result.audit.validations.some((item) => !item.valid)
        && result.translations.length === attempt.window.blockIds.length
        && (result.status === "completed" || result.status === "completed_with_warnings")
        && conflict === undefined;
      if (successful) {
        const sourceById = new Map(context.blocks.map((block) => [block.id, block]));
        store.commitWindow({
          windowId: attempt.window.windowId,
          status: result.status as "completed" | "completed_with_warnings",
          translations: result.translations.map((translation) => ({
            blockId: translation.blockId,
            sourceHash: (sourceById.get(translation.blockId) as { sourceHash: string }).sourceHash,
            text: translation.text,
          })),
          lexicalAnchors: result.audit.lexicalAnchors,
          narrativeMemories: [
            ...memoriesFromSnapshot(result.snapshot),
            ...result.narrativeMemories,
          ],
          styleTail: boundedActiveTail(
            result.translations.map((translation) => translation.text).join("\n\n"),
          ),
          budget: result.metrics.budget,
          warnings: result.metrics.degradedReasons,
        });
        return resultSummary(result);
      }

      const error = conflict
        ?? attempt.error
        ?? `window ended as ${result?.status ?? "failed"} without a complete valid submission`;
      const retry = attempt.window.attemptCount < maxAttempts;
      store.failWindow(attempt.window.windowId, {
        error,
        retry,
        budget: result?.metrics.budget ?? {},
        warnings: result?.metrics.degradedReasons ?? [error],
      });
      if (!retry) {
        return {
          status: "human_required",
          modelCalls: result?.metrics.modelCalls ?? 0,
          modelCallLimit: DEFAULT_BUDGET_LIMITS.modelCalls,
          repaired: result?.audit.validations.some((item) => item.repaired) ?? false,
          deadlineExceeded: error.toLocaleLowerCase().includes("deadline"),
        };
      }
      attempt = await runAttempt(store.window(attempt.window.windowId) as PersistedWindow);
    }
  };

  try {
    while (processedWindows < maxWindows) {
      const pending = store.pendingWindows();
      if (pending.length === 0) {
        break;
      }
      const concurrency = nextConcurrency(history, {
        warmupWindows,
        maxConcurrency,
      });
      const remaining = maxWindows - processedWindows;
      const selected = pending.slice(0, Math.min(concurrency, remaining));
      if (selected.length === 0) {
        break;
      }
      waves.push({
        wave: waves.length,
        concurrency: selected.length,
        windowIds: selected.map((window) => window.windowId),
      });
      const attempts = await Promise.all(selected.map(runAttempt));
      attempts.sort((left, right) => left.window.ordinal - right.window.ordinal);
      for (const attempt of attempts) {
        history.push(await finalizeAttempt(attempt));
      }
      processedWindows += selected.length;
    }
    const status = store.statusSummary();
    const outcome: BookRunResult["outcome"] = status.humanRequiredWindows > 0
      ? "human_required"
      : status.pendingWindows > 0
        ? "partial"
        : status.warningWindows > 0
          ? "completed_with_warnings"
          : "completed";
    const artifacts = writeBookArtifacts(store, options.outputDir, {
      allowIncomplete: true,
    });
    return {
      outcome,
      processedWindows,
      waves,
      status,
      windows: store.allWindows(),
      wallTimeMs: performance.now() - startedAt,
      leaseReleased: true,
      artifacts,
    };
  } finally {
    lease.release();
    store.close();
    context.close();
  }
}

function requiredIdentifier(value: string, name: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new TypeError(`${name} must be nonempty`);
  }
  return value;
}

function knowledgeCandidatesFor(
  runId: string,
  windowId: string,
  candidates: readonly TranslationMemoryCandidate[],
): KnowledgeCandidate[] {
  return candidates.map((candidate, index) => {
    const normalizedSubject = candidate.subjectForms[0]
      ?.normalize("NFKC")
      .trim()
      .toLocaleLowerCase();
    if (normalizedSubject === undefined || normalizedSubject.length === 0) {
      throw new Error(`memory candidate ${index} has no nonempty subject form`);
    }
    const payload = {
      fact: candidate.fact,
      confidence: candidate.confidence,
      subjectForms: [...candidate.subjectForms],
    };
    const recordId = `knowledge-${createHash("sha256")
      .update(`${runId}\0${windowId}\0${index}\0${JSON.stringify({
        normalizedSubject,
        kind: candidate.kind,
        payload,
      })}`)
      .digest("hex")
      .slice(0, 24)}`;
    return {
      recordId,
      normalizedSubject,
      kind: candidate.kind,
      payload,
    };
  });
}

interface WaveKnowledgeCandidate {
  candidate: KnowledgeCandidate;
  sourceForms: readonly string[];
}

interface WaveAnchorSnapshot {
  schemaVersion: "v5-wave-anchor-1";
  inputHash: string;
  anchors: readonly LexicalAnchor[];
  entityLinks: readonly EntityLink[];
  terms: readonly StableTerm[];
}

function waveAnchorInputHash(
  context: BookContext,
  candidates: readonly { sourceForm: string; contexts: readonly string[] }[],
  stableTerms: readonly StableTerm[],
): string {
  return createHash("sha256").update(canonicalJson({
    schemaVersion: "v5-wave-anchor-input-1",
    profile: {
      id: context.languageProfile.id,
      version: context.languageProfile.version,
    },
    candidates,
    stableTerms: stableTerms.map((term) => ({
      sourceForm: term.sourceForm,
      canonicalSource: term.canonicalSource,
      target: term.target,
      locked: term.locked,
      ...(term.policy === undefined ? {} : { policy: term.policy }),
      ...(term.note === undefined ? {} : { note: term.note }),
      ...(term.origin === undefined ? {} : { origin: term.origin }),
    })),
  })).digest("hex");
}

function parseWaveAnchorSnapshot(
  value: unknown,
  expectedInputHash: string,
): WaveAnchorSnapshot {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("corrupt cached wave anchor decision");
  }
  const candidate = value as Partial<WaveAnchorSnapshot>;
  if (candidate.schemaVersion !== "v5-wave-anchor-1"
    || candidate.inputHash !== expectedInputHash
    || !Array.isArray(candidate.anchors)
    || !Array.isArray(candidate.entityLinks)
    || !Array.isArray(candidate.terms)) {
    throw new Error("corrupt cached wave anchor decision");
  }
  return structuredClone(candidate as WaveAnchorSnapshot);
}

function unresolvedEntityWarnings(snapshot: WaveAnchorSnapshot | undefined): string[] {
  return snapshot?.entityLinks.filter((link) => link.status !== "confirmed")
    .map((link) => [
      link.sourceForms.join(" / "),
      link.status,
      "same-entity relation is unresolved; do not lock them to one Chinese target",
    ].join(": ")) ?? [];
}

function losslessAsV4(block: BookContext["losslessBlocks"][number]): V4Block {
  return {
    id: block.id,
    legacyId: null,
    chapterId: block.structureId,
    chapterTitle: block.structureTitle,
    globalIndex: block.globalIndex,
    blockIndex: block.globalIndex,
    sourceText: block.sourceText,
    sourceHash: block.sourceHash,
    tokenCount: block.tokenCount,
  };
}

function withoutStructureHeadingLines(
  block: V4Block,
  context: BookContext,
): V4Block | undefined {
  const sourceText = block.sourceText.split(/\r?\n/gu)
    .filter((line) => context.languageProfile.detectStructureHeading(line.trim()) === null)
    .join("\n")
    .trim();
  return sourceText.length === 0 ? undefined : { ...block, sourceText };
}

function windowSourceText(
  window: PersistedLosslessWindow,
  blockById: ReadonlyMap<string, BookContext["losslessBlocks"][number]>,
): string {
  return window.blockIds.map((blockId) => blockById.get(blockId)?.sourceText ?? "")
    .join("\n\n");
}

function termsFromKnowledge(
  revisions: readonly unknown[],
): StableTerm[] {
  const terms: StableTerm[] = [];
  for (const raw of revisions) {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
      continue;
    }
    const revision = raw as { kind?: unknown; payload?: unknown; status?: unknown };
    if (revision.status !== "active") {
      continue;
    }
    if (revision.kind === "lexical_anchor"
      && revision.payload !== null
      && typeof revision.payload === "object"
      && !Array.isArray(revision.payload)) {
      const term = revision.payload as Partial<StableTerm>;
      if (typeof term.conceptId === "string"
        && typeof term.lexemeId === "string"
        && typeof term.sourceForm === "string"
        && typeof term.canonicalSource === "string"
        && typeof term.target === "string"
        && typeof term.locked === "boolean") {
        terms.push({
          ...(term as StableTerm),
          origin: term.origin ?? "knowledge",
        });
      }
    }
    if (revision.kind === "entity_alias_link"
      && revision.payload !== null
      && typeof revision.payload === "object"
      && !Array.isArray(revision.payload)) {
      terms.push(...entityLinkAsTerms(revision.payload as EntityLink).map((term) => ({
        ...term,
        origin: term.origin ?? "knowledge",
      })));
    }
  }
  return terms;
}

function uniqueTerms(
  terms: readonly StableTerm[],
  context: BookContext,
): StableTerm[] {
  const byForm = new Map<string, StableTerm>();
  for (const term of terms) {
    const normalized = context.languageProfile.normalizeSourceForm(term.sourceForm);
    const previous = byForm.get(normalized);
    const priority = (value: StableTerm): number => {
      if (value.origin === "glossary") {
        return 3;
      }
      if (value.origin === "legacy") {
        return 2;
      }
      return 1;
    };
    if (previous === undefined || priority(term) >= priority(previous)) {
      byForm.set(normalized, { ...term });
    }
  }
  return [...byForm.values()].sort((left, right) =>
    left.sourceForm.localeCompare(right.sourceForm));
}

function sourceBlocksForWindows(
  windows: readonly Pick<PersistedLosslessWindow, "blockIds">[],
  blockById: ReadonlyMap<string, BookContext["losslessBlocks"][number]>,
): BookContext["losslessBlocks"] {
  return windows.flatMap((window) => window.blockIds
    .map((blockId) => blockById.get(blockId))
    .filter((block): block is BookContext["losslessBlocks"][number] => block !== undefined));
}

function termsForWindows(
  terms: readonly StableTerm[],
  windows: readonly Pick<PersistedLosslessWindow, "blockIds" | "globalIndexes">[],
  context: BookContext,
  glossary: LoadedGlossary | undefined,
): StableTerm[] {
  const nonGlossary = terms.filter((term) => term.origin !== "glossary");
  if (glossary === undefined) {
    return uniqueTerms(nonGlossary, context);
  }
  const glossaryRelevant = relevantGlossaryTerms(
    glossary,
    windows.flatMap((window) => window.globalIndexes),
  );
  return uniqueTerms([...nonGlossary, ...glossaryRelevant], context);
}

function waveKnowledgeCandidates(
  runId: string,
  outcome: Pick<WaveAnchorSnapshot, "terms" | "entityLinks"> | undefined,
  context: BookContext,
): WaveKnowledgeCandidate[] {
  if (outcome === undefined) {
    return [];
  }
  const entityForms = new Set(outcome.entityLinks.flatMap((link) =>
    link.normalizedForms));
  const result: WaveKnowledgeCandidate[] = [];
  for (const term of outcome.terms) {
    if (entityForms.has(context.languageProfile.normalizeSourceForm(term.sourceForm))) {
      continue;
    }
    const payload = { ...term };
    result.push({
      candidate: {
        recordId: `wave-anchor-${createHash("sha256")
          .update(`${runId}\0${canonicalJson(payload)}`)
          .digest("hex")
          .slice(0, 24)}`,
        normalizedSubject: context.languageProfile.normalizeSourceForm(term.sourceForm),
        kind: "lexical_anchor",
        payload,
      },
      sourceForms: [term.sourceForm],
    });
  }
  for (const link of outcome.entityLinks) {
    result.push({
      candidate: {
        recordId: `wave-entity-${createHash("sha256")
          .update(`${runId}\0${canonicalJson(link)}`)
          .digest("hex")
          .slice(0, 24)}`,
        normalizedSubject: `entity-alias:${link.linkId}`,
        kind: "entity_alias_link",
        payload: link,
      },
      sourceForms: link.sourceForms,
    });
  }
  return result;
}

function windowContainsAnyForm(
  window: PersistedLosslessWindow,
  forms: readonly string[],
  blockById: ReadonlyMap<string, BookContext["losslessBlocks"][number]>,
  context: BookContext,
): boolean {
  const requested = new Set(forms.map((form) =>
    context.languageProfile.normalizeSourceForm(form)));
  return window.blockIds.some((blockId) => {
    const block = blockById.get(blockId);
    return block !== undefined && context.languageProfile.segment(block.sourceText)
      .some((token) => token.isWordLike && requested.has(token.normalized));
  });
}

function assignWaveKnowledge(
  candidates: readonly WaveKnowledgeCandidate[],
  selected: readonly PersistedLosslessWindow[],
  successfulWindowIds: ReadonlySet<string>,
  blockById: ReadonlyMap<string, BookContext["losslessBlocks"][number]>,
  context: BookContext,
): Map<string, KnowledgeCandidate[]> {
  const assigned = new Map<string, KnowledgeCandidate[]>();
  const successful = selected.filter((window) => successfulWindowIds.has(window.windowId));
  for (const item of candidates) {
    const owner = successful.find((window) =>
      windowContainsAnyForm(window, item.sourceForms, blockById, context));
    if (owner === undefined) {
      continue;
    }
    const values = assigned.get(owner.windowId) ?? [];
    values.push(item.candidate);
    assigned.set(owner.windowId, values);
  }
  return assigned;
}

function firstUncommitted(
  windows: readonly PersistedLosslessWindow[],
): PersistedLosslessWindow | undefined {
  return windows.find((window) =>
    window.status !== "completed" && window.status !== "completed_with_warnings");
}

function combinedBudget(
  previous: Readonly<Record<string, number>>,
  current: Readonly<Record<string, number>>,
): Record<string, number> {
  const combined = { ...previous };
  for (const [counter, value] of Object.entries(current)) {
    combined[counter] = (combined[counter] ?? 0) + value;
  }
  return combined;
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  signal?.throwIfAborted();
}

function normalizeRuntimeSet(options: LosslessBookRunOptions): TranslationRuntimeSet {
  const runtimeSet: TranslationRuntimeSet = options.runtimeSet ?? {
    mode: "quality" as const,
    primary: { model: options.model, streamFn: options.streamFn },
    escalation: { model: options.model, streamFn: options.streamFn },
  };
  if (runtimeSet.primary.model.id !== runtimeSet.escalation.model.id) {
    throw new TypeError("primary and escalation runtimes must use the same model identity");
  }
  if (runtimeSet.mode === "quality"
    && (runtimeSet.primary.effort !== runtimeSet.escalation.effort
      || runtimeSet.primary.thinkingLevel !== runtimeSet.escalation.thinkingLevel
      || runtimeSet.primary.model !== runtimeSet.escalation.model
      || runtimeSet.primary.streamFn !== runtimeSet.escalation.streamFn)) {
    throw new TypeError("quality mode cannot change effort during retries or repair");
  }
  return runtimeSet;
}

export function windowOptionsForRunMode(
  mode: TranslationRuntimeSet["mode"],
  requested: WindowPlanOptions = {},
): WindowPlanOptions {
  if (mode === "quality") return { ...requested };
  const maxSourceTokens = requested.maxSourceTokens ?? FAST_MAX_SOURCE_TOKENS;
  return {
    ...requested,
    targetSourceTokens: requested.targetSourceTokens
      ?? Math.min(FAST_TARGET_SOURCE_TOKENS, maxSourceTokens),
    maxSourceTokens,
    maxBlocks: requested.maxBlocks ?? FAST_MAX_BLOCKS,
  };
}

function reasoningReserveTokens(runtime: TranslationRuntime): number {
  switch (runtime.effort ?? runtime.thinkingLevel) {
    case undefined:
    case "off":
      return 0;
    case "minimal":
      return 512;
    case "low":
      return 1_024;
    case "medium":
    case "on":
      return 2_048;
    case "high":
      return 4_096;
    case "xhigh":
      return 6_144;
    case "max":
      return 8_192;
  }
}

function outputReserveTokens(request: PhysicalRequestPlan, runtime: TranslationRuntime): number {
  return Math.min(
    runtime.model.maxTokens,
    Math.max(768, Math.ceil(request.sourceTokens * 1.6) + 512),
  );
}

interface AdmittedRequest<TInput> {
  request: PhysicalRequestPlan;
  input: TInput;
  assessment: RequestBudgetAssessment;
}

function admitTranslationRequests<TInput extends TranslationRequestInput>(
  requests: readonly PhysicalRequestPlan[],
  runtime: TranslationRuntime,
  estimator: WeightedTokenEstimator,
  buildInput: (request: PhysicalRequestPlan) => TInput,
): AdmittedRequest<TInput>[] {
  const admitted: AdmittedRequest<TInput>[] = [];
  const queue = [...requests];
  while (queue.length > 0) {
    const request = queue.shift() as PhysicalRequestPlan;
    const input = buildInput(request);
    const assessment = new RequestBudgeter(estimator, {
      contextWindowTokens: runtime.model.contextWindow,
      outputTokens: outputReserveTokens(request, runtime),
      reasoningReserveTokens: Math.min(
        reasoningReserveTokens(runtime),
        runtime.model.maxTokens,
      ),
      safetyMarginTokens: Math.max(512, Math.ceil(runtime.model.contextWindow * 0.02)),
    }).assess(input);
    if (assessment.fits) {
      admitted.push({ request, input, assessment });
      continue;
    }
    if (assessment.decision === "split_request" && request.windows.length > 1) {
      queue.unshift(...packPhysicalRequests(request.windows, {
        tinyWindowTokens: 1,
        maxRequestTokens: Math.max(1, request.sourceTokens),
        maxWindowsPerRequest: 1,
      }));
      continue;
    }
    throw new BookRequestCapacityError(request.requestId, assessment);
  }
  return admitted;
}

interface ScheduledResult<T> {
  value: T;
  status: SchedulerObservationStatus;
}

async function runWithAdaptiveScheduler<TInput, TOutput>(
  items: readonly AdmittedRequest<TInput>[],
  scheduler: AdaptiveScheduler,
  worker: (item: AdmittedRequest<TInput>) => Promise<ScheduledResult<TOutput>>,
  signal?: AbortSignal,
): Promise<TOutput[]> {
  const pending = [...items];
  const running = new Set<Promise<void>>();
  const completed: TOutput[] = [];
  while (pending.length > 0 || running.size > 0) {
    throwIfAborted(signal);
    let admittedAny = false;
    for (let index = 0; index < pending.length;) {
      const item = pending[index] as AdmittedRequest<TInput>;
      const permit = scheduler.tryAcquire(item.assessment.totalReserved);
      if (permit === undefined) {
        index += 1;
        continue;
      }
      pending.splice(index, 1);
      admittedAny = true;
      const startedAt = performance.now();
      let task: Promise<void>;
      task = (async () => {
        const result = await worker(item);
        completed.push(result.value);
        scheduler.observe({
          status: result.status,
          durationMs: performance.now() - startedAt,
          estimatedTokens: item.assessment.totalReserved,
        });
      })().finally(() => {
        permit.release();
        running.delete(task);
      });
      running.add(task);
    }
    if (running.size === 0 && pending.length > 0) {
      const smallest = Math.min(...pending.map((item) => item.assessment.totalReserved));
      throw new RangeError(
        `maxInFlightTokens cannot admit the smallest request reservation (${smallest})`,
      );
    }
    if (running.size > 0 && (!admittedAny || pending.length === 0)) {
      await Promise.race(running);
    }
  }
  return completed;
}

async function runLosslessBook(
  options: LosslessBookRunOptions,
): Promise<LosslessBookRunResult> {
  const startedAt = performance.now();
  const runtimeSet = normalizeRuntimeSet(options);
  const maxWindows = nonNegativeInteger(
    options.maxWindows ?? Number.MAX_SAFE_INTEGER,
    "maxWindows",
  );
  const maxConcurrency = positiveInteger(
    options.maxConcurrency ?? (runtimeSet.mode === "fast" ? 4 : DEFAULT_MAX_CONCURRENCY),
    "maxConcurrency",
  );
  const maxAttempts = positiveInteger(
    options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS,
    "maxAttempts",
  );
  const tinyWindowTokens = positiveInteger(
    options.tinyWindowTokens ?? 128,
    "tinyWindowTokens",
  );
  const maxRequestTokens = positiveInteger(
    options.maxRequestTokens ?? 3_200,
    "maxRequestTokens",
  );
  const maxWindowsPerRequest = positiveInteger(
    options.maxWindowsPerRequest ?? 4,
    "maxWindowsPerRequest",
  );
  const maxInFlightTokens = positiveInteger(
    options.maxInFlightTokens ?? Math.min(
      runtimeSet.primary.model.contextWindow * maxConcurrency,
      256_000,
    ),
    "maxInFlightTokens",
  );
  const runId = requiredIdentifier(options.runMeta.runId, "runMeta.runId");
  const protocolVersion = requiredIdentifier(
    options.runMeta.protocolVersion,
    "runMeta.protocolVersion",
  );

  // Opening the certified ledger and running the independent coverage audit is
  // deliberately the first source operation. No provider call is reachable
  // before this doctor gate succeeds.
  const context = BookContext.openLossless({
    manifestPath: options.manifestPath,
    ...(options.legacyV4DbPath === undefined
      ? {}
      : { legacyV4DbPath: options.legacyV4DbPath }),
  });
  const glossaryExpectedSourceVersion = context.sourceLedger.sourceVersion;
  if (options.glossary?.sourceVersion !== undefined
    && options.glossary.sourceVersion !== glossaryExpectedSourceVersion) {
    context.close();
    throw new Error(
      `glossary snapshot source version mismatch: expected ${glossaryExpectedSourceVersion}, found ${options.glossary.sourceVersion}`,
    );
  }
  const modelId = options.runMeta.modelId ?? runtimeSet.primary.model.id;
  if (modelId !== runtimeSet.primary.model.id) {
    context.close();
    throw new Error(
      `run model mismatch: metadata declares ${modelId}, provider model is ${runtimeSet.primary.model.id}`,
    );
  }
  let lease: ReturnType<typeof RunLease.acquire>;
  try {
    lease = RunLease.acquire(
      `${resolve(options.storePath)}.run.lock`,
      `lossless:${runId}`,
    );
  } catch (error) {
    context.close();
    throw error;
  }
  let store: LosslessBookStore;
  try {
    store = new LosslessBookStore(options.storePath);
  } catch (error) {
    lease.release();
    context.close();
    throw isStorageLocked(error) ? new BookStorageIncidentError(error) : error;
  }
  const waves: BookWaveReport[] = [];
  let processedWindows = 0;

  try {
    store.registerSource(context.certifiedSource as NonNullable<typeof context.certifiedSource>);
    store.replaceDerivedPlan(context.sourceLedger.sourceVersion, {
      blocks: context.losslessBlocks,
      annotations: context.annotations,
    });
    const initialSnapshot = createKnowledgeSnapshot(runId, []);
    const requestedMetadata = runMetadataWithLanguageProfile(
      options.runMeta.metadata,
      context,
      runtimeSet,
    );
    const existingRun = store.listTranslationRuns().find((item) => item.runId === runId);
    const existingRuntimeMetadata = runtimeMetadata(existingRun?.metadata);
    if (existingRun !== undefined) {
      if (existingRuntimeMetadata === undefined && runtimeSet.mode !== "quality") {
        throw new Error("legacy translation runs can only resume in quality mode");
      }
      if (existingRuntimeMetadata !== undefined
        && canonicalJson(existingRuntimeMetadata) !== canonicalJson(
          runtimeMetadata(requestedMetadata),
        )) {
        throw new Error(`translation runtime policy mismatch for ${runId}`);
      }
    }
    store.createTranslationRun({
      runId,
      sourceVersion: context.sourceLedger.sourceVersion,
      protocolVersion,
      modelId,
      initialSnapshotId: initialSnapshot.id,
      initialSnapshot,
      metadata: existingRun?.metadata ?? requestedMetadata,
    });
    const planned = planBookWindows(context.losslessBlocks, {
      ...windowOptionsForRunMode(runtimeSet.mode, options.windowOptions),
      protocolVersion,
    });
    store.initializeWindowPlan(runId, planned);
    store.recoverInterruptedWindows(runId);
    const estimator = new WeightedTokenEstimator();
    const schedulerSnapshot = store.latestSchedulerSnapshot(runId);
    const scheduler = new AdaptiveScheduler({
      initialConcurrency: Math.min(2, maxConcurrency),
      maxConcurrency,
      maxInFlightTokens,
      ...(schedulerSnapshot === undefined ? {} : { snapshot: schedulerSnapshot }),
    });
    const blockById = new Map(context.losslessBlocks.map((block) => [block.id, block]));
    const styleConstitution = losslessStyleConstitution(options.styleState);
    const voiceProfiles = losslessVoiceProfiles(options.styleState);

    while (processedWindows < maxWindows) {
      throwIfAborted(options.signal);
      assertSourceVersionUnchanged(context);
      const allWindows = store.allWindows(runId);
      const barrier = firstUncommitted(allWindows);
      if (barrier === undefined || barrier.status !== "pending") {
        break;
      }
      const remaining = maxWindows - processedWindows;
      const selected: PersistedLosslessWindow[] = [];
      for (const window of allWindows.slice(barrier.ordinal)) {
        if (window.status !== "pending"
          || selected.length >= remaining) {
          break;
        }
        const tentative = [...selected, window];
        const physicalCount = packPhysicalRequests(
          tentative.map((item) => ({ ...item, status: "pending" as const })),
          { tinyWindowTokens, maxRequestTokens, maxWindowsPerRequest },
        ).length;
        if (physicalCount > maxConcurrency) {
          break;
        }
        selected.push(window);
      }
      if (selected.length === 0) {
        break;
      }

      const snapshot = store.latestKnowledgeSnapshot(runId);
      const priorStyleObservations = store.styleObservations(runId);
      const effectiveStyleByWindow = Object.fromEntries(selected.map((window) => [
        window.windowId,
        projectEffectiveStyle(composeEffectiveStyle({
          constitution: styleConstitution,
          voices: voiceProfiles,
          observations: priorStyleObservations,
          currentOrdinal: window.ordinal,
          sourceText: windowSourceText(window, blockById),
          defaultVoiceId: "narrator",
        })),
      ])) as Record<string, EffectiveStyleProjection>;
      const establishedTerms = uniqueTerms([
        ...context.stableTerms.map((term) => ({
          ...term,
          origin: term.origin ?? "legacy" as const,
        })),
        ...(options.glossary?.stableTerms ?? []),
        ...termsFromKnowledge(snapshot.revisions),
      ], context);
      const selectedSourceBlocks = sourceBlocksForWindows(selected, blockById);
      const selectedBlocks = selectedSourceBlocks.map(losslessAsV4);
      const anchorStableTerms = termsForWindows(
        establishedTerms,
        selected,
        context,
        options.glossary,
      );
      const corpusBlocks = context.losslessBlocks.map(losslessAsV4);
      const anchorCandidates = collectWindowAnchorCandidates(
        selectedBlocks.map((block) => withoutStructureHeadingLines(block, context))
          .filter((block): block is V4Block => block !== undefined),
        corpusBlocks.map((block) => withoutStructureHeadingLines(block, context))
          .filter((block): block is V4Block => block !== undefined),
        anchorStableTerms,
        [],
        context.languageProfile,
      );
      const anchorBudget = new BudgetLedger();
      let waveAnchorSnapshot: WaveAnchorSnapshot | undefined;
      if (anchorCandidates.length >= 2) {
        const inputHash = waveAnchorInputHash(context, anchorCandidates, anchorStableTerms);
        const cached = store.waveAnchorDecision(runId, inputHash);
        if (cached !== undefined) {
          waveAnchorSnapshot = parseWaveAnchorSnapshot(cached, inputHash);
        } else {
          throwIfAborted(options.signal);
          const outcome: LexicalAnchorOutcome = await new LexicalAnchorer(new PiRuntime()).run({
            candidates: anchorCandidates,
            stableTerms: anchorStableTerms,
            model: runtimeSet.escalation.model,
            streamFn: runtimeSet.escalation.streamFn,
            budget: anchorBudget,
            sourceLanguageProfile: context.languageProfile,
            signal: options.signal,
            deadlineMs: options.hardDeadlineMs,
          });
          waveAnchorSnapshot = {
            schemaVersion: "v5-wave-anchor-1",
            inputHash,
            anchors: outcome.anchors,
            entityLinks: outcome.entityLinks,
            terms: outcome.terms,
          };
          store.cacheWaveAnchorDecision(runId, inputHash, waveAnchorSnapshot);
        }
      }
      const activeTerms = uniqueTerms([
        ...establishedTerms,
        ...(waveAnchorSnapshot?.terms ?? []).map((term) => ({
          ...term,
          origin: term.origin ?? "knowledge" as const,
        })),
      ], context);
      const unpersistedWaveKnowledge = waveKnowledgeCandidates(
        runId,
        waveAnchorSnapshot,
        context,
      );
      const entityLinkWarnings = unresolvedEntityWarnings(waveAnchorSnapshot);
      const coordinator = new CommitCoordinator(
        runId,
        new KnowledgeStore(store.knowledgeRevisions(runId)),
        {
          commitPromotion: (promotion) => {
            store.promoteStagedWindow(promotion);
          },
        },
        snapshot,
      );
      const relativeOrdinal = new Map<string, number>();
      selected.forEach((window, ordinal) => {
        relativeOrdinal.set(window.windowId, ordinal);
        coordinator.bindWindow({ ordinal, windowId: window.windowId, snapshot });
      });
      let retryWindows = selected;
      let providerFailure: ModelProviderError | undefined;
      let firstProviderFailure: ModelProviderError | undefined;
      let initialRequestCount = 0;
      let retryRound = 0;
      let anchorBudgetPending = Object.keys(anchorBudget.snapshot()).length > 0;
      const persistedBudgetFor = (
        window: PersistedLosslessWindow,
        requestBudget: Readonly<Record<string, number>>,
        receivesRequestBudget: boolean,
      ): Record<string, number> => {
        let increment = receivesRequestBudget ? requestBudget : {};
        if (anchorBudgetPending && window.windowId === selected[0]?.windowId) {
          increment = combinedBudget(increment, anchorBudget.snapshot());
          anchorBudgetPending = false;
        }
        return combinedBudget(window.budget, increment);
      };
      while (retryWindows.length > 0 && providerFailure === undefined) {
        throwIfAborted(options.signal);
        store.bindWindowsToSnapshot(
          runId,
          retryWindows.map((window) => window.windowId),
          snapshot.id,
        );
        const requests = packPhysicalRequests(
          retryWindows.map((window) => ({ ...window, status: "pending" as const })),
          { tinyWindowTokens, maxRequestTokens, maxWindowsPerRequest },
        );
        const executionRuntime = retryRound === 0
          ? runtimeSet.primary
          : runtimeSet.escalation;
        const requestInputs = admitTranslationRequests(
          requests,
          executionRuntime,
          estimator,
          (request): TranslationRequestInput => ({
            request,
            blocks: context.losslessBlocks,
            stableTerms: termsForWindows(
              activeTerms,
              request.windows,
              context,
              options.glossary,
            ),
            snapshot,
            styleState: options.styleState,
            sourceLanguageProfile: context.languageProfile,
            entityLinkWarnings,
            effectiveStyleByWindow: Object.fromEntries(request.windows.map((window) => [
              window.windowId,
              effectiveStyleByWindow[window.windowId] as EffectiveStyleProjection,
            ])),
          }),
        );
        if (initialRequestCount === 0) {
          initialRequestCount = requestInputs.length;
        }
        const oversizedReservation = requestInputs.find((item) =>
          item.assessment.totalReserved > maxInFlightTokens);
        if (oversizedReservation !== undefined) {
          throw new RangeError(
            `maxInFlightTokens cannot admit request ${oversizedReservation.request.requestId} `
            + `(${oversizedReservation.assessment.totalReserved} tokens)`,
          );
        }
        const claimed = new Map<string, PersistedLosslessWindow>();
        throwIfAborted(options.signal);
        for (const { request } of requestInputs) {
          for (const window of request.windows) {
            claimed.set(window.windowId, store.claimWindow(runId, window.windowId));
          }
        }

        type CompletedRequest = {
          request: (typeof requests)[number];
          budget: BudgetLedger;
          result?: Awaited<ReturnType<typeof runTranslationBatch>>;
          error?: unknown;
        };
        const completionOrder = await runWithAdaptiveScheduler(
          requestInputs,
          scheduler,
          async ({ request, input }): Promise<ScheduledResult<CompletedRequest>> => {
            const budget = new BudgetLedger();
            try {
              throwIfAborted(options.signal);
              const result = await runTranslationBatch({
                ...input,
                model: executionRuntime.model,
                streamFn: executionRuntime.streamFn,
                thinkingLevel: executionRuntime.thinkingLevel,
                repairRuntime: {
                  model: runtimeSet.escalation.model,
                  streamFn: runtimeSet.escalation.streamFn,
                  thinkingLevel: runtimeSet.escalation.thinkingLevel,
                },
                budget,
                signal: options.signal,
                deadlineMs: options.hardDeadlineMs,
              });
              return {
                value: { request, budget, result },
                status: "success",
              };
            } catch (error) {
              const status: SchedulerObservationStatus = error instanceof ModelProviderError
                && (error.kind === "throttled"
                  || error.kind === "timeout"
                  || error.kind === "busy"
                  || error.kind === "context")
                ? error.kind
                : "failed";
              return {
                value: { request, budget, error },
                status,
              };
            }
          },
          options.signal,
        );
        store.saveSchedulerSnapshot(runId, scheduler.snapshot());

        throwIfAborted(options.signal);

        const successfulWindowIds = new Set(completionOrder.flatMap((completed) =>
          completed.result?.windows
            .filter((window) => window.status !== "failed")
            .map((window) => window.windowId) ?? []));
        const assignedWaveKnowledge = assignWaveKnowledge(
          unpersistedWaveKnowledge,
          selected,
          successfulWindowIds,
          blockById,
          context,
        );
        const assignedRecordIds = new Set([...assignedWaveKnowledge.values()]
          .flatMap((items) => items.map((item) => item.recordId)));
        for (let index = unpersistedWaveKnowledge.length - 1; index >= 0; index -= 1) {
          if (assignedRecordIds.has(unpersistedWaveKnowledge[index]!.candidate.recordId)) {
            unpersistedWaveKnowledge.splice(index, 1);
          }
        }
        const nextRetries: PersistedLosslessWindow[] = [];
        for (const completed of completionOrder) {
          if (completed.error !== undefined) {
            const completedError = completed.error;
            const message = completedError instanceof Error
              ? completedError.message
              : String(completedError);
            for (const requestWindow of completed.request.windows) {
              const window = claimed.get(requestWindow.windowId) as PersistedLosslessWindow;
              const external = completedError instanceof ModelProviderError;
              if (external && firstProviderFailure === undefined) {
                firstProviderFailure = completedError;
              }
              const boundedProviderRetry = external
                && completedError.retryable
                && window.attemptCount < maxAttempts;
              const retry = external || window.attemptCount < maxAttempts;
              store.failWindow(runId, window.windowId, {
                error: message,
                retry,
                budget: persistedBudgetFor(
                  window,
                  completed.budget.snapshot(),
                  completed.request.windows[0]?.windowId === window.windowId,
                ),
                warnings: external
                  ? ["external model provider failure; run aborted without human task"]
                  : [message],
              });
              if ((!external && retry) || boundedProviderRetry) {
                nextRetries.push(store.pendingWindows(runId)
                  .find((item) => item.windowId === window.windowId) as PersistedLosslessWindow);
              }
            }
            if (completedError instanceof ModelProviderError
              && (!completedError.retryable
                || completed.request.windows.some((requestWindow) =>
                  (claimed.get(requestWindow.windowId)?.attemptCount ?? maxAttempts) >= maxAttempts))) {
              const definitiveFailure = completedError.kind === "auth"
                || completedError.kind === "quota"
                || completedError.kind === "protocol"
                || completedError.kind === "context";
              providerFailure = definitiveFailure
                ? completedError
                : (firstProviderFailure ?? completedError);
            }
            continue;
          }

          const result = completed.result as Awaited<ReturnType<typeof runTranslationBatch>>;
          for (const windowResult of result.windows) {
            const window = claimed.get(windowResult.windowId) as PersistedLosslessWindow;
            if (windowResult.status === "failed") {
              const error = windowResult.error ?? "invalid batch window submission";
              const retry = window.attemptCount < maxAttempts;
              store.failWindow(runId, window.windowId, {
                error,
                retry,
                budget: persistedBudgetFor(
                  window,
                  completed.budget.snapshot(),
                  completed.request.windows[0]?.windowId === window.windowId,
                ),
                warnings: [error, ...result.responseErrors],
              });
              if (retry) {
                nextRetries.push(store.pendingWindows(runId)
                  .find((item) => item.windowId === window.windowId) as PersistedLosslessWindow);
              }
              continue;
            }

            const candidates = [
              ...knowledgeCandidatesFor(
                runId,
                window.windowId,
                windowResult.memoryCandidates,
              ),
              ...(assignedWaveKnowledge.get(window.windowId) ?? []),
            ];
            // Validate domain reconciliation before any durable stage is written.
            coordinator.knowledge.fork().reconcileCandidates(candidates, window.windowId);
            const ordinal = relativeOrdinal.get(window.windowId) as number;
            const translations = windowResult.translations.map((translation) => ({
              ...translation,
              sourceHash: (blockById.get(translation.blockId) as { sourceHash: string }).sourceHash,
            }));
            const warnings = [...windowResult.notes, ...result.responseErrors];
            const status = warnings.length > 0
              ? "completed_with_warnings" as const
              : "completed" as const;
            const styleObservation = createStyleObservation({
              windowId: window.windowId,
              ordinal: window.ordinal,
              sourceText: windowSourceText(window, blockById),
              translations: window.blockIds.map((blockId) =>
                translations.find((item) => item.blockId === blockId)?.text ?? ""),
              submission: windowResult.styleObservation,
            });
            throwIfAborted(options.signal);
            store.stageWindow({
              runId,
              windowId: window.windowId,
              snapshotId: snapshot.id,
              status,
              translations,
              knowledgeCandidates: candidates,
              styleTail: canonicalJson(styleObservation),
              budget: persistedBudgetFor(
                window,
                completed.budget.snapshot(),
                completed.request.windows[0]?.windowId === window.windowId,
              ),
              warnings,
            });
            coordinator.stage({
              runId,
              windowId: window.windowId,
              ordinal,
              snapshotId: snapshot.id,
              candidates,
            });
            throwIfAborted(options.signal);
            coordinator.promoteReady();
          }
        }
        retryWindows = nextRetries;
        retryRound += 1;
      }

      const initialRequests = packPhysicalRequests(
        selected.map((window) => ({ ...window, status: "pending" as const })),
        { tinyWindowTokens, maxRequestTokens, maxWindowsPerRequest },
      );
      waves.push({
        wave: waves.length,
        concurrency: initialRequestCount || initialRequests.length,
        windowIds: selected.map((window) => window.windowId),
      });
      processedWindows += selected.length;
      if (providerFailure !== undefined) {
        throw providerFailure;
      }
    }

    const status = store.statusSummary(runId);
    const outcome: LosslessBookRunResult["outcome"] = status.humanRequiredWindows > 0
      ? "human_required"
      : status.pendingWindows > 0 || status.runningWindows > 0 || status.stagedWindows > 0
        ? "partial"
        : status.warningWindows > 0
          ? "completed_with_warnings"
          : "completed";
    return {
      outcome,
      runId,
      processedWindows,
      waves,
      status,
      windows: store.allWindows(runId),
      wallTimeMs: performance.now() - startedAt,
      leaseReleased: true,
      artifacts: null,
    };
  } catch (error) {
    if (isStorageLocked(error)) {
      throw new BookStorageIncidentError(error);
    }
    throw error;
  } finally {
    store.close();
    lease.release();
    context.close();
  }
}

export function runBook(options: BookRunOptions): Promise<BookRunResult>;
export function runBook(options: LosslessBookRunOptions): Promise<LosslessBookRunResult>;
export function runBook(
  options: BookRunOptions | LosslessBookRunOptions,
): Promise<BookRunResult | LosslessBookRunResult> {
  return "manifestPath" in options
    ? runLosslessBook(options)
    : runLegacyBook(options);
}
