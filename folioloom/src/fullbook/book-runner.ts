import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import {
  collectWindowAnchorCandidates,
  LexicalAnchorer,
  sourceAuthoredAnchorFallback,
  softenModelAnchorTerm,
  type LexicalAnchor,
  type LexicalAnchorOutcome,
} from "../agents/lexical-anchorer.js";
import { ModelProviderError, PiRuntime } from "../agents/pi-runtime.js";
import {
  runTranslationBatch,
  type TranslationBatchResult,
  type TranslationBatchWindowResult,
} from "../agents/translation-batch.js";
import type { TranslationRequestInput } from "../agents/translation-request.js";
import type { EntityLink } from "../domain/entity-links.js";
import type { StableTerm, V4Block } from "../domain/types.js";
import {
  relevantGlossaryTerms,
  type LoadedGlossary,
} from "../glossary/glossary-profile.js";
import {
  BudgetExceeded,
  BudgetLedger,
  DEFAULT_BUDGET_LIMITS,
} from "../kernel/budget.js";
import { RunLease } from "../kernel/run-lease.js";
import {
  canonicalJson,
  KnowledgeStore,
  type KnowledgeCandidate,
  type KnowledgeRevision,
} from "../knowledge/knowledge-store.js";
import {
  mergeStyleState,
  persistedStyleFromKnowledge,
} from "../knowledge/persisted-style.js";
import { stableTermsFromKnowledge } from "../knowledge/stable-terms-from-knowledge.js";
import {
  conceptFromAnchor,
  type LexicalSemanticClass,
} from "../knowledge/lexical-concept.js";
import {
  evaluateRevalidationBindings,
  type RevalidationBindingDecision,
} from "../knowledge/sparse-revalidation.js";
import { conceptsFromStableTerms } from "../knowledge/term-usage.js";
import { createKnowledgeSnapshot } from "../knowledge/snapshot.js";
import { SourceLedger } from "../source/source-ledger.js";
import type { LosslessBlock } from "../source/types.js";
import {
  WeightedTokenEstimator,
  type UsageObservation,
} from "../source/token-estimator.js";
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
  type ConceptCoverageRevalidationReport,
  type KnowledgeRevalidationTask,
  type LosslessBookStatusSummary,
  type PersistedLosslessWindow,
  type RevalidationReplacementInput,
  type RevalidationWorkItem,
} from "../storage/lossless-book-store.js";
import type { TranslationMemoryCandidate } from "../tools/candidate-collector.js";
import type { StyleState } from "../tools/translation-tools.js";
import { TranslationValidator } from "../validators/translation-validator.js";
import {
  composeEffectiveStyle,
  createBookStyleConstitution,
} from "../style/effective-style.js";
import { createStyleObservation } from "../style/style-observation.js";
import { projectEffectiveStyle } from "../style/style-projection.js";
import { simplifyChineseTranslation } from "../style/chinese-script-normalization.js";
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
  RequestBatchWindow,
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

function joinedStyleInstruction(
  primary: string | undefined,
  qualifierLabel: string,
  qualifier: string | undefined,
): string | undefined {
  const normalizedQualifier = qualifier?.trim();
  const values = [
    normalizedQualifier === undefined || normalizedQualifier.length === 0
      ? undefined
      : `${qualifierLabel}：${normalizedQualifier}`,
    primary?.trim(),
  ].filter((value): value is string => value !== undefined && value.length > 0);
  return values.length === 0 ? undefined : values.join("；");
}

function losslessStyleConstitution(style: StyleState | undefined): BookStyleConstitution {
  return createBookStyleConstitution({
    register: style?.register,
    sentencePolicy: style?.sentencePolicy,
    explicitation: style?.explicitation,
    imagery: style?.imagery,
    dialogue: joinedStyleInstruction(
      style?.dialogue,
      "对话语域",
      style?.dialogueRegister,
    ),
    technicalProse: style?.technicalProse,
    typography: style?.typography ?? style?.dialogueQuotes,
    additionalInstruction: style?.additionalInstruction,
  });
}

function losslessVoiceProfiles(style: StyleState | undefined): VoiceProfile[] {
  return [{
    voiceId: "narrator",
    scope: "main_narrator",
    instruction: joinedStyleInstruction(
      style?.narratorVoice,
      "叙事距离",
      style?.narrativeDistance,
    ) ?? "保持作品主叙述者既定视角、距离和信息显隐",
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
  /** Optional shared estimator keeps provider/language calibration across waves and test runs. */
  tokenEstimator?: WeightedTokenEstimator;
  /**
   * Stops between durable window operations. A promotion that has already
   * entered the store remains atomic and is never interrupted mid-transaction.
   */
  signal?: AbortSignal;
}

export class BookRequestCapacityError extends Error {
  readonly code = "REQUEST_CONTEXT_EXCEEDED" as const;
  readonly retryable = false;

  constructor(
    requestId: string,
    assessment: RequestBudgetAssessment,
    detail?: string,
  ) {
    super(
      `REQUEST_CONTEXT_EXCEEDED: ${requestId} reserves ${assessment.totalReserved} tokens `
      + `for a ${assessment.contextWindowTokens}-token context`
      + (detail === undefined ? "" : ` (${detail})`),
    );
    this.name = "BookRequestCapacityError";
  }
}

class RevalidationOutputError extends Error {
  readonly code = "REVALIDATION_OUTPUT_INVALID" as const;

  constructor(detail: string) {
    super(`REVALIDATION_OUTPUT_INVALID: ${detail}`);
    this.name = "RevalidationOutputError";
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
  revalidationOverhead: {
    readonly coverageScan: ConceptCoverageRevalidationReport;
    readonly drain: RevalidationDrainReport;
  };
  leaseReleased: boolean;
  artifacts: null;
}

type RevalidationQueueStore = Pick<
  LosslessBookStore,
  | "claimNextRevalidationTask"
  | "revalidationWorkItem"
  | "resolveRevalidationNoop"
  | "replaceTranslationForRevalidation"
  | "completeRevalidationWithWarning"
>;

export interface RevalidationTranslationOutput
  extends Omit<
    RevalidationReplacementInput,
    "runId" | "taskId" | "action"
  > {
  readonly telemetry?: RevalidationModelTelemetry;
}

export interface RevalidationModelTelemetry {
  readonly modelCalls: number;
  readonly modelDurationMs: number;
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly cacheReadTokens: number;
  readonly cacheWriteTokens: number;
  readonly reasoningTokens: number;
  readonly totalTokens: number;
}

export interface RevalidationDrainReport {
  readonly claimed: number;
  readonly noop: number;
  readonly repaired: number;
  readonly retranslated: number;
  readonly warning: number;
  readonly modelCalls: number;
  readonly modelDurationMs: number;
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly cacheReadTokens: number;
  readonly cacheWriteTokens: number;
  readonly reasoningTokens: number;
  readonly totalTokens: number;
  readonly tokenUsageComplete: boolean;
  readonly wallTimeMs: number;
}

export interface RevalidationDrainOptions {
  readonly store: RevalidationQueueStore;
  readonly runId: string;
  readonly maxAttempts: number;
  readonly translate: (
    work: RevalidationWorkItem,
    action: Exclude<RevalidationBindingDecision["action"], "noop">,
  ) => Promise<RevalidationTranslationOutput>;
  readonly isExpectedFailure: (error: unknown) => boolean;
  readonly shouldRetryFailure?: (
    error: unknown,
    task: KnowledgeRevalidationTask,
  ) => boolean;
}

function revalidationFailureCode(error: unknown): string {
  if (error instanceof ModelProviderError) {
    return `PROVIDER_${error.kind.toUpperCase()}`;
  }
  if (error instanceof BudgetExceeded) {
    return "BUDGET_EXCEEDED";
  }
  if (error instanceof BookRequestCapacityError) {
    return error.code;
  }
  return "REVALIDATION_FAILED";
}

/**
 * Drain only the durable task set that already exists. Translation-generated
 * knowledge is intentionally absent from this loop, so one revalidation wave
 * cannot recursively schedule itself.
 */
export async function drainKnowledgeRevalidationTasks(
  options: RevalidationDrainOptions,
): Promise<RevalidationDrainReport> {
  positiveInteger(options.maxAttempts, "revalidation maxAttempts");
  const startedAt = performance.now();
  const report = {
    claimed: 0,
    noop: 0,
    repaired: 0,
    retranslated: 0,
    warning: 0,
    modelCalls: 0,
    modelDurationMs: 0,
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    reasoningTokens: 0,
    totalTokens: 0,
    tokenUsageComplete: true,
  };
  while (true) {
    const task = options.store.claimNextRevalidationTask(
      options.runId,
      options.maxAttempts,
    );
    if (task === undefined) break;
    report.claimed += 1;
    const work = options.store.revalidationWorkItem(
      options.runId,
      task.taskId,
    );
    const decision = evaluateRevalidationBindings(work.concepts);
    if (decision.action === "noop") {
      options.store.resolveRevalidationNoop(
        options.runId,
        task.taskId,
        { reason: "recorded_surfaces_remain_allowed" },
      );
      report.noop += 1;
      continue;
    }
    try {
      report.modelCalls += 1;
      const modelStartedAt = performance.now();
      const output = await options.translate(work, decision.action);
      if (output.telemetry === undefined) {
        report.modelDurationMs += performance.now() - modelStartedAt;
        report.tokenUsageComplete = false;
      } else {
        report.modelCalls += output.telemetry.modelCalls - 1;
        report.modelDurationMs += output.telemetry.modelDurationMs;
        report.inputTokens += output.telemetry.inputTokens;
        report.outputTokens += output.telemetry.outputTokens;
        report.cacheReadTokens += output.telemetry.cacheReadTokens;
        report.cacheWriteTokens += output.telemetry.cacheWriteTokens;
        report.reasoningTokens += output.telemetry.reasoningTokens;
        report.totalTokens += output.telemetry.totalTokens;
      }
      options.store.replaceTranslationForRevalidation({
        ...output,
        runId: options.runId,
        taskId: task.taskId,
        action: decision.action,
      });
      if (decision.action === "repair") {
        report.repaired += 1;
      } else {
        report.retranslated += 1;
      }
    } catch (error) {
      report.tokenUsageComplete = false;
      if (!options.isExpectedFailure(error)) {
        throw error;
      }
      const retryable = options.shouldRetryFailure?.(error, task) ?? true;
      if (retryable && task.attempts < options.maxAttempts) {
        continue;
      }
      options.store.completeRevalidationWithWarning(
        options.runId,
        task.taskId,
        { code: revalidationFailureCode(error) },
      );
      report.warning += 1;
    }
  }
  return {
    ...report,
    wallTimeMs: performance.now() - startedAt,
  };
}

function emptyRevalidationDrainReport(): RevalidationDrainReport {
  return {
    claimed: 0,
    noop: 0,
    repaired: 0,
    retranslated: 0,
    warning: 0,
    modelCalls: 0,
    modelDurationMs: 0,
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    reasoningTokens: 0,
    totalTokens: 0,
    tokenUsageComplete: true,
    wallTimeMs: 0,
  };
}

function mergeRevalidationDrainReports(
  left: RevalidationDrainReport,
  right: RevalidationDrainReport,
): RevalidationDrainReport {
  return {
    claimed: left.claimed + right.claimed,
    noop: left.noop + right.noop,
    repaired: left.repaired + right.repaired,
    retranslated: left.retranslated + right.retranslated,
    warning: left.warning + right.warning,
    modelCalls: left.modelCalls + right.modelCalls,
    modelDurationMs: left.modelDurationMs + right.modelDurationMs,
    inputTokens: left.inputTokens + right.inputTokens,
    outputTokens: left.outputTokens + right.outputTokens,
    cacheReadTokens: left.cacheReadTokens + right.cacheReadTokens,
    cacheWriteTokens: left.cacheWriteTokens + right.cacheWriteTokens,
    reasoningTokens: left.reasoningTokens + right.reasoningTokens,
    totalTokens: left.totalTokens + right.totalTokens,
    tokenUsageComplete:
      left.tokenUsageComplete && right.tokenUsageComplete,
    wallTimeMs: left.wallTimeMs + right.wallTimeMs,
  };
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
      ...(term.semanticClass === undefined ? {} : { semanticClass: term.semanticClass }),
      ...(term.allowedTargets === undefined ? {} : { allowedTargets: term.allowedTargets }),
      ...(term.revisionId === undefined ? {} : { revisionId: term.revisionId }),
      ...(term.renderFingerprint === undefined
        ? {}
        : { renderFingerprint: term.renderFingerprint }),
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
  const snapshot = structuredClone(candidate as WaveAnchorSnapshot);
  return {
    ...snapshot,
    anchors: snapshot.anchors.map((anchor) => ({
      ...anchor,
      semanticClass: anchor.semanticClass ?? "unclassified",
      lockEligible: anchor.lockEligible === true,
      target: simplifyChineseTranslation(anchor.target),
    })),
    terms: snapshot.terms.map((term) => softenModelAnchorTerm({
      ...term,
      target: simplifyChineseTranslation(term.target),
    })),
    entityLinks: snapshot.entityLinks.map((link) => ({
      ...link,
      preferredTarget: link.preferredTarget === null
        ? null
        : simplifyChineseTranslation(link.preferredTarget),
    })),
  };
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

function decidedAnchorFormsFromKnowledge(revisions: readonly unknown[]): string[] {
  return revisions.flatMap((raw) => {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
      return [];
    }
    const revision = raw as { kind?: unknown; payload?: unknown; status?: unknown };
    if ((revision.kind !== "lexical_anchor_decision"
      && revision.kind !== "lexical_concept")
      || (revision.status !== "active" && revision.status !== "contextual")
      || revision.payload === null
      || typeof revision.payload !== "object"
      || Array.isArray(revision.payload)) {
      return [];
    }
    const payload = revision.payload as {
      sourceForm?: unknown;
      sourceForms?: unknown;
    };
    if (revision.kind === "lexical_concept" && Array.isArray(payload.sourceForms)) {
      return payload.sourceForms.filter((sourceForm): sourceForm is string =>
        typeof sourceForm === "string" && sourceForm.trim().length > 0);
    }
    return typeof payload.sourceForm === "string" && payload.sourceForm.trim().length > 0
      ? [payload.sourceForm]
      : [];
  });
}

function uniqueTerms(
  terms: readonly StableTerm[],
  context: BookContext,
): StableTerm[] {
  const byForm = new Map<string, StableTerm>();
  for (const sourceTerm of terms) {
    const safeSourceTerm = softenModelAnchorTerm(sourceTerm);
    const term = safeSourceTerm.origin === "knowledge"
      ? { ...safeSourceTerm, target: simplifyChineseTranslation(safeSourceTerm.target) }
      : safeSourceTerm;
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
  outcome: Pick<WaveAnchorSnapshot, "anchors" | "terms" | "entityLinks"> | undefined,
  context: BookContext,
): WaveKnowledgeCandidate[] {
  if (outcome === undefined) {
    return [];
  }
  const entityForms = new Set(outcome.entityLinks
    .filter((link) => link.status === "confirmed")
    .flatMap((link) => link.normalizedForms));
  const projectedTermForms = new Set(outcome.terms.map((term) =>
    context.languageProfile.normalizeSourceForm(term.sourceForm)));
  const termsByForm = new Map(outcome.terms.map((term) => [
    context.languageProfile.normalizeSourceForm(term.sourceForm),
    term,
  ]));
  const conceptForms = new Set<string>();
  const result: WaveKnowledgeCandidate[] = [];
  for (const anchor of outcome.anchors) {
    const normalizedSource = context.languageProfile.normalizeSourceForm(anchor.sourceForm);
    const semanticClass = anchor.semanticClass ?? "unclassified";
    const term = termsByForm.get(normalizedSource);
    const conceptEligible = [
      "proper_name",
      "unique_title",
      "technical_term",
      "role",
    ].includes(semanticClass);
    if (conceptEligible && term !== undefined) {
      const concept = conceptFromAnchor({
        sourceForm: anchor.sourceForm,
        target: simplifyChineseTranslation(anchor.target),
        mode: anchor.mode,
        semanticClass: semanticClass as LexicalSemanticClass,
        confidence: anchor.confidence,
        allowedRealizations: term.allowedTargets ?? [term.target],
      });
      conceptForms.add(normalizedSource);
      result.push({
        candidate: {
          recordId: `wave-lexical-concept-${createHash("sha256")
            .update(`${runId}\0${canonicalJson(concept)}`)
            .digest("hex")
            .slice(0, 24)}`,
          normalizedSubject: normalizedSource,
          kind: "lexical_concept",
          payload: concept,
        },
        sourceForms: concept.sourceForms,
      });
      continue;
    }
    if (anchor.mode !== "contextual" && !projectedTermForms.has(normalizedSource)) continue;
    const payload = {
      sourceForm: anchor.sourceForm,
      target: simplifyChineseTranslation(anchor.target),
      mode: anchor.mode,
      semanticClass,
      confidence: anchor.confidence,
    };
    result.push({
      candidate: {
        recordId: `wave-anchor-decision-${createHash("sha256")
          .update(`${runId}\0${canonicalJson(payload)}`)
          .digest("hex")
          .slice(0, 24)}`,
        normalizedSubject: normalizedSource,
        kind: "lexical_anchor_decision",
        payload,
      },
      sourceForms: [anchor.sourceForm],
    });
  }
  for (const sourceTerm of outcome.terms) {
    const term = softenModelAnchorTerm(sourceTerm);
    const normalizedSource =
      context.languageProfile.normalizeSourceForm(term.sourceForm);
    if (entityForms.has(normalizedSource) || conceptForms.has(normalizedSource)) {
      continue;
    }
    const payload = { ...term, target: simplifyChineseTranslation(term.target) };
    result.push({
      candidate: {
        recordId: `wave-anchor-${createHash("sha256")
          .update(`${runId}\0${canonicalJson(payload)}`)
          .digest("hex")
          .slice(0, 24)}`,
        normalizedSubject: normalizedSource,
        kind: "lexical_anchor",
        payload,
      },
      sourceForms: [term.sourceForm],
    });
  }
  for (const link of outcome.entityLinks) {
    const payload = {
      ...link,
      preferredTarget: link.preferredTarget === null
        ? null
        : simplifyChineseTranslation(link.preferredTarget),
    };
    result.push({
      candidate: {
        recordId: `wave-entity-${createHash("sha256")
          .update(`${runId}\0${canonicalJson(payload)}`)
          .digest("hex")
          .slice(0, 24)}`,
        normalizedSubject: `entity-alias:${link.linkId}`,
        kind: "entity_alias_link",
        payload,
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
    context.languageProfile.normalizeAnchorSourceForm(form)));
  return window.blockIds.some((blockId) => {
    const block = blockById.get(blockId);
    return block !== undefined && context.languageProfile.segment(block.sourceText)
      .some((token) => token.isWordLike
        && requested.has(context.languageProfile.normalizeAnchorSourceForm(token.value)));
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

const CONTEXT_FRAGMENT_PROTOCOL_VERSION = "v5-context-fragment-1";
const MAX_CONTEXT_SPLIT_DEPTH = 16;
const MAX_CONTEXT_SPLIT_ATTEMPTS = 32;
const MAX_PROTOCOL_SPLIT_ATTEMPTS = 16;

interface AdmittedRequestFragment<TInput> {
  request: PhysicalRequestPlan;
  input: TInput;
  assessment: RequestBudgetAssessment;
  /** Every split raises this number, so recovery cannot re-enqueue the same shape forever. */
  depth: number;
}

interface AdmittedRequest<TInput> {
  /** Original physical grouping: it is the durable claim/stage/promote boundary. */
  request: PhysicalRequestPlan;
  /** Context-safe provider calls, executed serially while holding one scheduler permit. */
  fragments: readonly AdmittedRequestFragment<TInput>[];
  /** Reserve only the largest fragment, never the sum of serial fragment calls. */
  assessment: RequestBudgetAssessment;
}

function contextFragmentRequestId(
  parentRequestId: string,
  windowId: string,
  blockIds: readonly string[],
): string {
  const hash = createHash("sha256");
  hash.update(CONTEXT_FRAGMENT_PROTOCOL_VERSION);
  hash.update("\0");
  hash.update(parentRequestId);
  hash.update("\0");
  hash.update(windowId);
  for (const blockId of blockIds) {
    hash.update("\0");
    hash.update(blockId);
  }
  return `request-${hash.digest("hex").slice(0, 20)}`;
}

function blocksForFragment(
  blockIds: readonly string[],
  blockById: ReadonlyMap<string, LosslessBlock>,
): LosslessBlock[] {
  return blockIds.map((blockId) => {
    const block = blockById.get(blockId);
    if (block === undefined) {
      throw new Error(`context fragment references unknown block ${blockId}`);
    }
    return block;
  });
}

function fragmentWindowAtBlocks(
  window: RequestBatchWindow,
  blockIds: readonly string[],
  blockById: ReadonlyMap<string, LosslessBlock>,
): RequestBatchWindow {
  const blocks = blocksForFragment(blockIds, blockById);
  return {
    ...window,
    blockIds: [...blockIds],
    globalIndexes: blocks.map((block) => block.globalIndex),
    sourceTokens: blocks.reduce((total, block) => total + block.tokenCount, 0),
    sourceChars: blocks.reduce(
      (total, block) => total + block.canonicalEnd - block.canonicalStart,
      0,
    ),
    oversized: false,
  };
}

function splitSingleWindowRequestAtBlocks(
  request: PhysicalRequestPlan,
  blockById: ReadonlyMap<string, LosslessBlock>,
): PhysicalRequestPlan[] {
  const window = request.windows[0];
  if (window === undefined || window.blockIds.length < 2) {
    return [];
  }
  const blocks = blocksForFragment(window.blockIds, blockById);
  const totalTokens = blocks.reduce((total, block) => total + block.tokenCount, 0);
  let leftTokens = 0;
  let bestCut = 1;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (let index = 1; index < blocks.length; index += 1) {
    leftTokens += (blocks[index - 1] as LosslessBlock).tokenCount;
    const distance = Math.abs(totalTokens - leftTokens * 2);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestCut = index;
    }
  }
  const partitions = [
    window.blockIds.slice(0, bestCut),
    window.blockIds.slice(bestCut),
  ];
  return partitions.map((blockIds) => {
    const fragmentWindow = fragmentWindowAtBlocks(window, blockIds, blockById);
    return {
      requestId: contextFragmentRequestId(request.requestId, window.windowId, blockIds),
      windows: [fragmentWindow],
      sourceTokens: fragmentWindow.sourceTokens,
    };
  });
}

/**
 * Always isolate logical windows before splitting their source.  This ordering
 * preserves maximal narrative context whenever the provider capacity permits it.
 */
function splitPhysicalRequestAtBoundaries(
  request: PhysicalRequestPlan,
  blockById: ReadonlyMap<string, LosslessBlock>,
): PhysicalRequestPlan[] {
  if (request.windows.length > 1) {
    return packPhysicalRequests(request.windows, {
      tinyWindowTokens: 1,
      maxRequestTokens: Math.max(1, request.sourceTokens),
      maxWindowsPerRequest: 1,
    });
  }
  return splitSingleWindowRequestAtBlocks(request, blockById);
}

function assessTranslationFragment<TInput extends TranslationRequestInput>(
  request: PhysicalRequestPlan,
  runtime: TranslationRuntime,
  estimator: WeightedTokenEstimator,
  buildInput: (request: PhysicalRequestPlan) => TInput,
  depth = 0,
): AdmittedRequestFragment<TInput> {
  const input = buildInput(request);
  const assessment = new RequestBudgeter(estimator, {
    modelId: runtime.model.id,
    contextWindowTokens: runtime.model.contextWindow,
    outputTokens: outputReserveTokens(request, runtime),
    reasoningReserveTokens: Math.min(
      reasoningReserveTokens(runtime),
      runtime.model.maxTokens,
    ),
    safetyMarginTokens: Math.max(512, Math.ceil(runtime.model.contextWindow * 0.02)),
  }).assess(input);
  return { request, input, assessment, depth };
}

function capacityAssessmentForProviderContext(
  assessment: RequestBudgetAssessment,
): RequestBudgetAssessment {
  if (!assessment.fits) {
    return assessment;
  }
  const totalReserved = assessment.contextWindowTokens < Number.MAX_SAFE_INTEGER
    ? Math.max(assessment.totalReserved, assessment.contextWindowTokens + 1)
    : assessment.totalReserved;
  return {
    ...assessment,
    totalReserved,
    fits: false,
    decision: "split_window",
  };
}

function admitTranslationFragments<TInput extends TranslationRequestInput>(
  requests: readonly PhysicalRequestPlan[],
  runtime: TranslationRuntime,
  estimator: WeightedTokenEstimator,
  blockById: ReadonlyMap<string, LosslessBlock>,
  buildInput: (request: PhysicalRequestPlan) => TInput,
  initialDepth = 0,
): AdmittedRequestFragment<TInput>[] {
  const admitted: AdmittedRequestFragment<TInput>[] = [];
  const queue = requests.map((request) => ({ request, depth: initialDepth }));
  while (queue.length > 0) {
    const queued = queue.shift() as { request: PhysicalRequestPlan; depth: number };
    const fragment = assessTranslationFragment(
      queued.request,
      runtime,
      estimator,
      buildInput,
      queued.depth,
    );
    if (fragment.assessment.fits) {
      admitted.push(fragment);
      continue;
    }
    const children = splitPhysicalRequestAtBoundaries(fragment.request, blockById);
    if (children.length === 0 || fragment.depth >= MAX_CONTEXT_SPLIT_DEPTH) {
      throw new BookRequestCapacityError(
        fragment.request.requestId,
        fragment.assessment,
        children.length === 0
          ? "one logical source block cannot be subdivided further"
          : `automatic context subdivision reached ${MAX_CONTEXT_SPLIT_DEPTH} levels`,
      );
    }
    queue.unshift(...children.map((request) => ({ request, depth: fragment.depth + 1 })));
  }
  return admitted;
}

function admitTranslationRequests<TInput extends TranslationRequestInput>(
  requests: readonly PhysicalRequestPlan[],
  runtime: TranslationRuntime,
  estimator: WeightedTokenEstimator,
  blockById: ReadonlyMap<string, LosslessBlock>,
  buildInput: (request: PhysicalRequestPlan) => TInput,
): AdmittedRequest<TInput>[] {
  return requests.map((request) => {
    const fragments = admitTranslationFragments(
      [request],
      runtime,
      estimator,
      blockById,
      buildInput,
    );
    const largest = fragments.reduce((current, fragment) =>
      fragment.assessment.totalReserved > current.assessment.totalReserved
        ? fragment
        : current,
    );
    return { request, fragments, assessment: largest.assessment };
  });
}

interface FragmentTranslationExecution {
  fragment: AdmittedRequestFragment<TranslationRequestInput>;
  result: TranslationBatchResult;
}

interface MergedTranslationResult {
  windows: TranslationBatchWindowResult[];
  responseErrors: string[];
}

const RECOVERABLE_STRUCTURAL_SUBMISSION_ERROR = /^(?:missing window submission|duplicate windowId|unknown blockId|duplicate blockId|empty translation|block set mismatch|missing block translations while merging context fragments|duplicate block translation while merging context fragments)/u;
const RECOVERABLE_LOCAL_DEGENERATION_ERROR = /^(?:validation|cross-block validation) failed after one targeted repair:[\s\S]*(?:untranslated_latin|paragraph_count_incompatible|paragraph_length_incompatible|abnormal_block_shortening|insufficient_lexical_content|abnormal_shortening|cross_block_translation_overlap)/u;

function failedWindowIdsMatching(
  result: TranslationBatchResult,
  pattern: RegExp,
): Set<string> {
  return new Set(result.windows.flatMap((window) => (
    window.status === "failed"
      && pattern.test(window.error ?? "")
      ? [window.windowId]
      : []
  )));
}

function recoverableStructuralWindowIds(result: TranslationBatchResult): Set<string> {
  return failedWindowIdsMatching(result, RECOVERABLE_STRUCTURAL_SUBMISSION_ERROR);
}

function recoverableDegenerationWindowIds(result: TranslationBatchResult): Set<string> {
  return failedWindowIdsMatching(result, RECOVERABLE_LOCAL_DEGENERATION_ERROR);
}

function requestWithWindows(
  request: PhysicalRequestPlan,
  windowIds: ReadonlySet<string>,
): PhysicalRequestPlan {
  const windows = request.windows.filter((window) => windowIds.has(window.windowId));
  return {
    ...request,
    windows,
    sourceTokens: windows.reduce((total, window) => total + window.sourceTokens, 0),
  };
}

function mergeFragmentTranslationResults(
  request: PhysicalRequestPlan,
  executions: readonly FragmentTranslationExecution[],
): MergedTranslationResult {
  const partsByWindow = new Map<string, TranslationBatchWindowResult[]>();
  for (const execution of executions) {
    for (const result of execution.result.windows) {
      const parts = partsByWindow.get(result.windowId) ?? [];
      parts.push(result);
      partsByWindow.set(result.windowId, parts);
    }
  }
  const windows = request.windows.map((logicalWindow): TranslationBatchWindowResult => {
    const parts = partsByWindow.get(logicalWindow.windowId) ?? [];
    const failedPart = parts.find((part) => part.status === "failed");
    if (failedPart !== undefined) {
      return {
        windowId: logicalWindow.windowId,
        ordinal: logicalWindow.ordinal,
        status: "failed",
        translations: [],
        termUsages: [],
        notes: [],
        memoryCandidates: [],
        error: failedPart.error ?? "fragment translation failed",
      };
    }
    const translationsByBlock = new Map<string, { blockId: string; text: string }>();
    for (const part of parts) {
      for (const translation of part.translations) {
        if (translationsByBlock.has(translation.blockId)) {
          return {
            windowId: logicalWindow.windowId,
            ordinal: logicalWindow.ordinal,
            status: "failed",
            translations: [],
            termUsages: [],
            notes: [],
            memoryCandidates: [],
            error: `duplicate block translation while merging context fragments: ${translation.blockId}`,
          };
        }
        translationsByBlock.set(translation.blockId, translation);
      }
    }
    const missing = logicalWindow.blockIds.filter((blockId) => !translationsByBlock.has(blockId));
    if (missing.length > 0) {
      return {
        windowId: logicalWindow.windowId,
        ordinal: logicalWindow.ordinal,
        status: "failed",
        translations: [],
        termUsages: [],
        notes: [],
        memoryCandidates: [],
        error: `missing block translations while merging context fragments: ${missing.join(", ")}`,
      };
    }
    let styleObservation: TranslationBatchWindowResult["styleObservation"];
    for (const part of parts) {
      if (part.styleObservation !== undefined) {
        styleObservation = part.styleObservation;
      }
    }
    return {
      windowId: logicalWindow.windowId,
      ordinal: logicalWindow.ordinal,
      status: parts.some((part) => part.status === "completed_with_warnings")
        ? "completed_with_warnings"
        : "completed",
      translations: logicalWindow.blockIds.map((blockId) =>
        translationsByBlock.get(blockId) as { blockId: string; text: string }),
      termUsages: parts.flatMap((part) => part.termUsages),
      notes: parts.flatMap((part) => part.notes),
      memoryCandidates: parts.flatMap((part) => part.memoryCandidates),
      ...(styleObservation === undefined ? {} : { styleObservation }),
    };
  });
  return {
    windows,
    responseErrors: executions.flatMap((execution) => execution.result.responseErrors),
  };
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
    options.maxRequestTokens
      ?? (runtimeSet.mode === "fast" ? FAST_MAX_SOURCE_TOKENS : 3_200),
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
  mkdirSync(dirname(resolve(options.storePath)), { recursive: true });
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
    const estimator = options.tokenEstimator ?? new WeightedTokenEstimator();
    const schedulerSnapshot = store.latestSchedulerSnapshot(runId);
    const scheduler = new AdaptiveScheduler({
      initialConcurrency: Math.min(2, maxConcurrency),
      maxConcurrency,
      maxInFlightTokens,
      ...(schedulerSnapshot === undefined ? {} : { snapshot: schedulerSnapshot }),
    });
    const blockById = new Map(context.losslessBlocks.map((block) => [block.id, block]));
    store.syncScopedKnowledge(runId);
    const coverageScan = store.ensureConceptCoverageRevalidationTasks(
      runId,
      store.latestKnowledgeSnapshot(runId).id,
    );
    let revalidationDrain = emptyRevalidationDrainReport();
    let fastWaveHorizonMultiplier = runtimeSet.mode === "fast" ? 2 : 1;
    const translateRevalidation = async (
      work: RevalidationWorkItem,
      action: "repair" | "retranslate",
    ): Promise<RevalidationTranslationOutput> => {
      throwIfAborted(options.signal);
      const sourceBlock = blockById.get(work.source.blockId);
      if (sourceBlock === undefined
        || sourceBlock.sourceHash !== work.source.sourceHash) {
        throw new RevalidationOutputError("source block provenance changed");
      }
      const snapshot = store.latestKnowledgeSnapshot(runId);
      if (snapshot.id !== work.task.toSnapshotId) {
        const currentConceptIds = new Set(
          stableTermsFromKnowledge(snapshot.revisions)
            .map((term) => term.conceptId),
        );
        if (work.task.conceptIds.some((conceptId) =>
          !currentConceptIds.has(conceptId))) {
          throw new RevalidationOutputError(
            "latest snapshot no longer contains a changed concept",
          );
        }
      }
      const requestStyle = mergeStyleState(
        options.styleState,
        persistedStyleFromKnowledge(snapshot.revisions),
      );
      const requestWindow: RequestBatchWindow = {
        windowId: work.window.windowId,
        ordinal: work.window.ordinal,
        chapterId: work.window.chapterId,
        chapterTitle: work.window.chapterTitle,
        blockIds: [work.source.blockId],
        globalIndexes: [work.source.globalIndex],
        sourceTokens: work.source.tokenCount,
        sourceChars: Array.from(work.source.sourceText).length,
        oversized: work.source.tokenCount > maxRequestTokens,
        status: "pending",
      };
      const request: PhysicalRequestPlan = {
        requestId: `revalidation-${createHash("sha256")
          .update([
            runId,
            work.task.taskId,
            String(work.task.attempts),
          ].join("\0"), "utf8")
          .digest("hex")
          .slice(0, 24)}`,
        windows: [requestWindow],
        sourceTokens: requestWindow.sourceTokens,
      };
      const terms = termsForWindows(
        uniqueTerms([
          ...context.stableTerms.map((term) => ({
            ...term,
            origin: term.origin ?? "legacy" as const,
          })),
          ...(options.glossary?.stableTerms ?? []),
          ...stableTermsFromKnowledge(snapshot.revisions),
        ], context),
        [requestWindow],
        context,
        options.glossary,
      );
      const effectiveStyle = projectEffectiveStyle(composeEffectiveStyle({
        constitution: losslessStyleConstitution(requestStyle),
        voices: losslessVoiceProfiles(requestStyle),
        observations: store.styleObservations(runId),
        currentOrdinal: work.window.ordinal,
        sourceText: work.source.sourceText,
        defaultVoiceId: "narrator",
      }));
      const runtime = work.task.attempts > 1
        ? runtimeSet.escalation
        : runtimeSet.primary;
      const budget = new BudgetLedger();
      const result = await runTranslationBatch({
        request,
        blocks: context.losslessBlocks,
        stableTerms: terms,
        snapshot,
        styleState: requestStyle,
        sourceLanguageProfile: context.languageProfile,
        entityLinkWarnings: [],
        effectiveStyleByWindow: {
          [requestWindow.windowId]: effectiveStyle,
        },
        responseProtocol: runtimeSet.mode === "fast"
          ? "framed_text"
          : "typed_tool",
        model: runtime.model,
        streamFn: runtime.streamFn,
        thinkingLevel: runtime.thinkingLevel,
        repairRuntime: {
          model: runtimeSet.escalation.model,
          streamFn: runtimeSet.escalation.streamFn,
          thinkingLevel: runtimeSet.escalation.thinkingLevel,
        },
        budget,
        signal: options.signal,
        deadlineMs: options.hardDeadlineMs,
      });
      const windowResult = result.windows.find((candidate) =>
        candidate.windowId === requestWindow.windowId);
      if (windowResult === undefined
        || windowResult.status === "failed"
        || windowResult.translations.length !== 1
        || windowResult.translations[0]?.blockId !== work.source.blockId) {
        throw new RevalidationOutputError("single-block translation was incomplete");
      }
      const translatedText = windowResult.translations[0].text;
      const active = store.activeTranslations(runId);
      const activeIndex = active.findIndex((translation) =>
        translation.blockId === work.source.blockId);
      const boundaryTranslations = [
        ...(activeIndex > 0 ? [active[activeIndex - 1]!] : []),
        {
          ...work.translation,
          text: translatedText,
        },
        ...(activeIndex >= 0 && activeIndex + 1 < active.length
          ? [active[activeIndex + 1]!]
          : []),
      ].map((translation) => ({
        blockId: translation.blockId,
        text: translation.text,
      }));
      const boundaryBlocks = boundaryTranslations
        .map((translation) => blockById.get(translation.blockId))
        .filter((block): block is BookContext["losslessBlocks"][number] =>
          block !== undefined)
        .map(losslessAsV4);
      const boundary = new TranslationValidator().validateCrossBlockAlignment(
        boundaryBlocks,
        {
          translations: boundaryTranslations,
          notes: [],
          repaired: action === "repair",
        },
      );
      if (boundary.failures.length > 0) {
        throw new RevalidationOutputError("cross-block alignment failed");
      }
      const warnings = [...windowResult.notes, ...result.responseErrors];
      const revalidationRuns = [result.run, ...result.repairRuns];
      const telemetry: RevalidationModelTelemetry = {
        modelCalls: revalidationRuns.reduce(
          (total, run) => total + run.modelCalls,
          0,
        ),
        modelDurationMs: revalidationRuns.reduce(
          (total, run) => total + run.durationMs,
          0,
        ),
        inputTokens: revalidationRuns.reduce(
          (total, run) => total + run.usage.input,
          0,
        ),
        outputTokens: revalidationRuns.reduce(
          (total, run) => total + run.usage.output,
          0,
        ),
        cacheReadTokens: revalidationRuns.reduce(
          (total, run) => total + run.usage.cacheRead,
          0,
        ),
        cacheWriteTokens: revalidationRuns.reduce(
          (total, run) => total + run.usage.cacheWrite,
          0,
        ),
        reasoningTokens: revalidationRuns.reduce(
          (total, run) => total + (run.usage.reasoning ?? 0),
          0,
        ),
        totalTokens: revalidationRuns.reduce(
          (total, run) => total + run.usage.totalTokens,
          0,
        ),
      };
      return {
        snapshotId: snapshot.id,
        text: translatedText,
        resultStatus: warnings.length > 0
          ? "completed_with_warnings"
          : "completed",
        termUsages: windowResult.termUsages,
        concepts: conceptsFromStableTerms(terms),
        telemetry,
        result: {
          action,
          responseWarnings: result.responseErrors.length,
          notes: windowResult.notes.length,
          modelCalls: result.run.modelCalls
            + result.repairRuns.reduce(
              (total, repairRun) => total + repairRun.modelCalls,
              0,
            ),
        },
      };
    };

    while (processedWindows < maxWindows) {
      throwIfAborted(options.signal);
      assertSourceVersionUnchanged(context);
      // A book or project catalog can be edited by another completed run while
      // this run is paused. Synchronize only at the wave boundary, before any
      // window is claimed, so the next request sees the newest durable user
      // knowledge without ever changing a running/staged wave.
      store.syncScopedKnowledge(runId);
      const drainedRevalidation = await drainKnowledgeRevalidationTasks({
        store,
        runId,
        maxAttempts,
        translate: translateRevalidation,
        isExpectedFailure: (error) =>
          error instanceof ModelProviderError
          || error instanceof BudgetExceeded
          || error instanceof BookRequestCapacityError
          || error instanceof RevalidationOutputError,
        shouldRetryFailure: (error) =>
          error instanceof RevalidationOutputError
          || (error instanceof ModelProviderError && error.retryable),
      });
      revalidationDrain = mergeRevalidationDrainReports(
        revalidationDrain,
        drainedRevalidation,
      );
      const allWindows = store.allWindows(runId);
      const barrier = firstUncommitted(allWindows);
      if (barrier === undefined || barrier.status !== "pending") {
        break;
      }
      const remaining = maxWindows - processedWindows;
      const selected: PersistedLosslessWindow[] = [];
      const physicalRequestHorizon = maxConcurrency * fastWaveHorizonMultiplier;
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
        if (physicalCount > physicalRequestHorizon) {
          break;
        }
        selected.push(window);
      }
      if (selected.length === 0) {
        break;
      }

      const snapshot = store.latestKnowledgeSnapshot(runId);
      const requestStyle = mergeStyleState(
        options.styleState,
        persistedStyleFromKnowledge(snapshot.revisions),
      );
      const styleConstitution = losslessStyleConstitution(requestStyle);
      const voiceProfiles = losslessVoiceProfiles(requestStyle);
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
        ...stableTermsFromKnowledge(snapshot.revisions),
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
        decidedAnchorFormsFromKnowledge(snapshot.revisions),
        context.languageProfile,
      );
      const anchorBudget = new BudgetLedger();
      let waveAnchorSnapshot: WaveAnchorSnapshot | undefined;
      if (anchorCandidates.length === 1
        && anchorCandidates[0]?.sourceAuthoredTarget !== undefined) {
        const inputHash = waveAnchorInputHash(context, anchorCandidates, anchorStableTerms);
        const cached = store.waveAnchorDecision(runId, inputHash);
        if (cached !== undefined) {
          waveAnchorSnapshot = parseWaveAnchorSnapshot(cached, inputHash);
        } else {
          const outcome = sourceAuthoredAnchorFallback(anchorCandidates);
          waveAnchorSnapshot = {
            schemaVersion: "v5-wave-anchor-1",
            inputHash,
            anchors: outcome.anchors,
            entityLinks: outcome.entityLinks,
            terms: outcome.terms,
          };
          const projectedForms = new Set(outcome.terms.map((term) =>
            context.languageProfile.normalizeSourceForm(term.sourceForm)));
          const anchorByForm = new Map(outcome.anchors.map((anchor) => [
            context.languageProfile.normalizeSourceForm(anchor.sourceForm),
            anchor,
          ]));
          const reusableDecision = anchorCandidates.every((candidate) => {
            const normalized = context.languageProfile.normalizeSourceForm(candidate.sourceForm);
            const anchor = anchorByForm.get(normalized);
            return anchor !== undefined
              && (anchor.mode === "contextual" || projectedForms.has(normalized));
          });
          if (reusableDecision) {
            store.cacheWaveAnchorDecision(runId, inputHash, waveAnchorSnapshot);
          }
        }
      }
      if (anchorCandidates.length >= 2) {
        const inputHash = waveAnchorInputHash(context, anchorCandidates, anchorStableTerms);
        const cached = store.waveAnchorDecision(runId, inputHash);
        if (cached !== undefined) {
          waveAnchorSnapshot = parseWaveAnchorSnapshot(cached, inputHash);
        } else {
          throwIfAborted(options.signal);
          const anchorRuntime = runtimeSet.mode === "fast"
            ? runtimeSet.primary
            : runtimeSet.escalation;
          const anchorer = new LexicalAnchorer(new PiRuntime());
          const anchorInput = (runtime: TranslationRuntime) => ({
            candidates: anchorCandidates,
            stableTerms: anchorStableTerms,
            model: runtime.model,
            streamFn: runtime.streamFn,
            budget: anchorBudget,
            sourceLanguageProfile: context.languageProfile,
            thinkingLevel: runtime.thinkingLevel,
            signal: options.signal,
            deadlineMs: options.hardDeadlineMs,
          });
          const resolveAnchors = (runtime: TranslationRuntime) =>
            anchorer.run(anchorInput(runtime));
          const resolvePreferredFallback = (runtime: TranslationRuntime) =>
            anchorer.runPreferredTextFallback(anchorInput(runtime));
          const preferredOrSourceFallback = async (runtime: TranslationRuntime) => {
            try {
              return await resolvePreferredFallback(runtime);
            } catch (fallbackError) {
              if (fallbackError instanceof ModelProviderError
                && fallbackError.kind === "protocol") {
                return sourceAuthoredAnchorFallback(anchorCandidates);
              }
              throw fallbackError;
            }
          };
          const escalationIsDistinct = anchorRuntime.model !== runtimeSet.escalation.model
            || anchorRuntime.streamFn !== runtimeSet.escalation.streamFn
            || anchorRuntime.effort !== runtimeSet.escalation.effort
            || anchorRuntime.thinkingLevel !== runtimeSet.escalation.thinkingLevel;
          let outcome: Pick<LexicalAnchorOutcome, "anchors" | "entityLinks" | "terms">;
          if (runtimeSet.mode === "fast") {
            try {
              outcome = await resolvePreferredFallback(anchorRuntime);
            } catch (error) {
              throwIfAborted(options.signal);
              const cannotBenefitFromEscalation = error instanceof ModelProviderError
                && (error.kind === "auth" || error.kind === "quota");
              if (cannotBenefitFromEscalation || !escalationIsDistinct) {
                if (error instanceof ModelProviderError && error.kind === "protocol") {
                  outcome = sourceAuthoredAnchorFallback(anchorCandidates);
                } else {
                  throw error;
                }
              } else {
                try {
                  outcome = await resolveAnchors(runtimeSet.escalation);
                } catch (escalationError) {
                  if (!(escalationError instanceof ModelProviderError)
                    || escalationError.kind !== "protocol") {
                    throw escalationError;
                  }
                  outcome = await preferredOrSourceFallback(runtimeSet.escalation);
                }
              }
            }
          } else {
            try {
              outcome = await resolveAnchors(anchorRuntime);
            } catch (error) {
              throwIfAborted(options.signal);
              if (!(error instanceof ModelProviderError) || error.kind !== "protocol") {
                throw error;
              }
              try {
                outcome = await preferredOrSourceFallback(anchorRuntime);
              } catch (fallbackError) {
                throw fallbackError;
              }
            }
          }
          waveAnchorSnapshot = {
            schemaVersion: "v5-wave-anchor-1",
            inputHash,
            anchors: outcome.anchors,
            entityLinks: outcome.entityLinks,
            terms: outcome.terms,
          };
          const projectedForms = new Set(outcome.terms.map((term) =>
            context.languageProfile.normalizeSourceForm(term.sourceForm)));
          const anchorByForm = new Map(outcome.anchors.map((anchor) => [
            context.languageProfile.normalizeSourceForm(anchor.sourceForm),
            anchor,
          ]));
          const reusableDecision = anchorCandidates.every((candidate) => {
            const normalized = context.languageProfile.normalizeSourceForm(candidate.sourceForm);
            const anchor = anchorByForm.get(normalized);
            return anchor !== undefined
              && (anchor.mode === "contextual" || projectedForms.has(normalized));
          });
          if (reusableDecision) {
            store.cacheWaveAnchorDecision(runId, inputHash, waveAnchorSnapshot);
          }
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
          commitPromotion: (promotion) =>
            store.promoteStagedWindow(promotion),
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
      let freshWaveRequired = false;
      let initialRequestCount = 0;
      let retryRound = 0;
      const acceptedWaveTranslations = new Map<string, {
        blockId: string;
        text: string;
        windowId: string;
      }>();
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
        const buildTranslationInput = (request: PhysicalRequestPlan): TranslationRequestInput => ({
          request,
          blocks: context.losslessBlocks,
          stableTerms: termsForWindows(
            activeTerms,
            request.windows,
            context,
            options.glossary,
          ),
          snapshot,
          styleState: requestStyle,
          sourceLanguageProfile: context.languageProfile,
          entityLinkWarnings,
          effectiveStyleByWindow: Object.fromEntries(request.windows.map((window) => [
            window.windowId,
            effectiveStyleByWindow[window.windowId] as EffectiveStyleProjection,
          ])),
          responseProtocol: runtimeSet.mode === "fast" ? "framed_text" : "typed_tool",
        });
        const requestInputs = admitTranslationRequests(
          requests,
          executionRuntime,
          estimator,
          blockById,
          buildTranslationInput,
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
            if (!claimed.has(window.windowId)) {
              claimed.set(window.windowId, store.claimWindow(runId, window.windowId));
            }
          }
        }

        type CompletedRequest = {
          request: PhysicalRequestPlan;
          budget: Readonly<Record<string, number>>;
          result?: MergedTranslationResult;
          error?: unknown;
        };
        const completionOrder = await runWithAdaptiveScheduler(
          requestInputs,
          scheduler,
          async ({ request, fragments }): Promise<ScheduledResult<CompletedRequest>> => {
            // Fragments are one logical provider operation. Reusing the ledger
            // preserves the original hard ceilings instead of multiplying every
            // budget limit by the number of context-recovery fragments.
            const budget = new BudgetLedger();
            let observedContextOverflow = false;
            let contextSplitAttempts = 0;
            let protocolSplitAttempts = 0;
            const executeFragments = async (
              pendingFragments: readonly AdmittedRequestFragment<TranslationRequestInput>[],
              activeRuntime: TranslationRuntime = executionRuntime,
              retryingLocally = false,
            ): Promise<FragmentTranslationExecution[]> => {
              const completed: FragmentTranslationExecution[] = [];
              for (const fragment of pendingFragments) {
                try {
                  throwIfAborted(options.signal);
                  const result = await runTranslationBatch({
                    ...fragment.input,
                    model: activeRuntime.model,
                    streamFn: activeRuntime.streamFn,
                    thinkingLevel: activeRuntime.thinkingLevel,
                    repairRuntime: retryingLocally
                      ? {
                        model: activeRuntime.model,
                        streamFn: activeRuntime.streamFn,
                        thinkingLevel: activeRuntime.thinkingLevel,
                      }
                      : {
                        model: runtimeSet.escalation.model,
                        streamFn: runtimeSet.escalation.streamFn,
                        thinkingLevel: runtimeSet.escalation.thinkingLevel,
                      },
                    budget,
                    signal: options.signal,
                    deadlineMs: options.hardDeadlineMs,
                  });
                  if (result.run.modelCalls === 1
                    && fragment.assessment.inputTokens > 0
                    && result.run.usage.input > 0) {
                    const observation: UsageObservation = {
                      modelId: activeRuntime.model.id,
                      profile: context.languageProfile,
                      estimatedTokens: fragment.assessment.inputTokens,
                      actualInputTokens: result.run.usage.input,
                    };
                    estimator.observeUsage(observation);
                  }
                  const structuralWindowIds = recoverableStructuralWindowIds(result);
                  const degenerationWindowIds = recoverableDegenerationWindowIds(result);
                  const recoverableWindowIds = new Set([
                    ...structuralWindowIds,
                    ...degenerationWindowIds,
                  ]);
                  if (recoverableWindowIds.size > 0
                    && fragment.input.responseProtocol === "typed_tool"
                    && protocolSplitAttempts < MAX_PROTOCOL_SPLIT_ATTEMPTS) {
                    const failedRequest = requestWithWindows(
                      fragment.request,
                      recoverableWindowIds,
                    );
                    if (failedRequest.windows.length > 0) {
                      protocolSplitAttempts += 1;
                      const validWindows = result.windows.filter((window) =>
                        !recoverableWindowIds.has(window.windowId));
                      if (validWindows.length > 0) {
                        completed.push({
                          fragment,
                          result: { ...result, windows: validWindows },
                        });
                      }
                      const buildFramedTranslationInput = (
                        request: PhysicalRequestPlan,
                      ): TranslationRequestInput => ({
                        ...buildTranslationInput(request),
                        responseProtocol: "framed_text",
                      });
                      const admittedFallback = admitTranslationFragments(
                        [failedRequest],
                        runtimeSet.escalation,
                        estimator,
                        blockById,
                        buildFramedTranslationInput,
                        fragment.depth,
                      );
                      completed.push(...await executeFragments(
                        admittedFallback,
                        runtimeSet.escalation,
                        true,
                      ));
                      continue;
                    }
                  }
                  if (recoverableWindowIds.size > 0
                    && (fragment.request.windows.length === 1
                      || degenerationWindowIds.size > 0)
                    && fragment.depth < MAX_CONTEXT_SPLIT_DEPTH
                    && protocolSplitAttempts < MAX_PROTOCOL_SPLIT_ATTEMPTS) {
                    const failedRequest = requestWithWindows(
                      fragment.request,
                      recoverableWindowIds,
                    );
                    const children = splitPhysicalRequestAtBoundaries(
                      failedRequest,
                      blockById,
                    );
                    if (children.length > 0) {
                      protocolSplitAttempts += 1;
                      const validWindows = result.windows.filter((window) =>
                        !recoverableWindowIds.has(window.windowId));
                      if (validWindows.length > 0) {
                        completed.push({
                          fragment,
                          result: { ...result, windows: validWindows },
                        });
                      }
                      const admittedChildren = admitTranslationFragments(
                        children,
                        runtimeSet.escalation,
                        estimator,
                        blockById,
                        buildTranslationInput,
                        fragment.depth + 1,
                      );
                      completed.push(...await executeFragments(
                        admittedChildren,
                        runtimeSet.escalation,
                        true,
                      ));
                      continue;
                    }
                  }
                  completed.push({ fragment, result });
                } catch (error) {
                  if (!(error instanceof ModelProviderError) || error.kind !== "context") {
                    throw error;
                  }
                  observedContextOverflow = true;
                  contextSplitAttempts += 1;
                  const children = splitPhysicalRequestAtBoundaries(
                    fragment.request,
                    blockById,
                  );
                  if (children.length === 0
                    || fragment.depth >= MAX_CONTEXT_SPLIT_DEPTH
                    || contextSplitAttempts > MAX_CONTEXT_SPLIT_ATTEMPTS) {
                    throw new BookRequestCapacityError(
                      fragment.request.requestId,
                      capacityAssessmentForProviderContext(fragment.assessment),
                      children.length === 0
                        ? "provider rejected one indivisible source block for context capacity"
                        : fragment.depth >= MAX_CONTEXT_SPLIT_DEPTH
                          ? `provider context recovery reached ${MAX_CONTEXT_SPLIT_DEPTH} levels`
                          : `provider context recovery exceeded ${MAX_CONTEXT_SPLIT_ATTEMPTS} retries`,
                    );
                  }
                  const admittedChildren = admitTranslationFragments(
                    children,
                    activeRuntime,
                    estimator,
                    blockById,
                    buildTranslationInput,
                    fragment.depth + 1,
                  );
                  completed.push(...await executeFragments(
                    admittedChildren,
                    activeRuntime,
                    true,
                  ));
                }
              }
              return completed;
            };
            try {
              const executions = await executeFragments(fragments);
              return {
                value: {
                  request,
                  budget: budget.snapshot(),
                  result: mergeFragmentTranslationResults(request, executions),
                },
                status: observedContextOverflow ? "context" : "success",
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
                value: { request, budget: budget.snapshot(), error },
                status,
              };
            }
          },
          options.signal,
        );
        store.saveSchedulerSnapshot(runId, scheduler.snapshot());

        throwIfAborted(options.signal);

        const currentTranslations = completionOrder.flatMap((completed) =>
          completed.result?.windows.flatMap((window) => window.status === "failed"
            ? []
            : window.translations.map((translation) => ({
              ...translation,
              windowId: window.windowId,
            }))) ?? []);
        const currentBlockIds = new Set(currentTranslations.map((item) => item.blockId));
        const currentIndexes = currentTranslations.map((item) =>
          blockById.get(item.blockId)?.globalIndex)
          .filter((index): index is number => index !== undefined);
        const earliestCurrentIndex = currentIndexes.length === 0
          ? Number.POSITIVE_INFINITY
          : Math.min(...currentIndexes);
        const priorActive = store.activeTranslations(runId)
          .filter((translation) =>
            (blockById.get(translation.blockId)?.globalIndex ?? Number.POSITIVE_INFINITY)
              < earliestCurrentIndex)
          .at(-1);
        const boundaryTranslations = [
          ...(priorActive === undefined ? [] : [{
            blockId: priorActive.blockId,
            text: priorActive.text,
          }]),
          ...[...acceptedWaveTranslations.values()].map(({ blockId, text }) => ({
            blockId,
            text,
          })),
          ...currentTranslations.map(({ blockId, text }) => ({ blockId, text })),
        ];
        const uniqueBoundaryTranslations = [...new Map(boundaryTranslations.map((item) => [
          item.blockId,
          item,
        ])).values()];
        const boundaryBlocks = uniqueBoundaryTranslations
          .map((translation) => blockById.get(translation.blockId))
          .filter((block): block is BookContext["losslessBlocks"][number] => block !== undefined)
          .map(losslessAsV4);
        const boundaryValidation = new TranslationValidator().validateCrossBlockAlignment(
          boundaryBlocks,
          {
            translations: uniqueBoundaryTranslations,
            notes: [],
            repaired: false,
          },
        );
        const boundaryFailuresByWindow = new Map<string, string[]>();
        for (const failure of boundaryValidation.failures) {
          if (failure.blockId === undefined || !currentBlockIds.has(failure.blockId)) {
            continue;
          }
          const windowId = currentTranslations.find((translation) =>
            translation.blockId === failure.blockId)?.windowId;
          if (windowId === undefined) {
            continue;
          }
          const messages = boundaryFailuresByWindow.get(windowId) ?? [];
          messages.push(`${failure.code}: ${failure.message}`);
          boundaryFailuresByWindow.set(windowId, messages);
        }

        const successfulWindowIds = new Set(completionOrder.flatMap((completed) =>
          completed.result?.windows
            .filter((window) => window.status !== "failed"
              && !boundaryFailuresByWindow.has(window.windowId))
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
        let capacityFailure: BookRequestCapacityError | undefined;
        for (const completed of completionOrder) {
          if (completed.error !== undefined) {
            const completedError = completed.error;
            const message = completedError instanceof Error
              ? completedError.message
              : String(completedError);
            const capacityError = completedError instanceof BookRequestCapacityError;
            for (const requestWindow of completed.request.windows) {
              const window = claimed.get(requestWindow.windowId) as PersistedLosslessWindow;
              const external = !capacityError && completedError instanceof ModelProviderError;
              if (external && firstProviderFailure === undefined) {
                firstProviderFailure = completedError;
              }
              const boundedProviderRetry = external
                && completedError.retryable
                && window.attemptCount < maxAttempts;
              const retry = !capacityError && (external || window.attemptCount < maxAttempts);
              store.failWindow(runId, window.windowId, {
                error: message,
                retry,
                budget: persistedBudgetFor(
                  window,
                  completed.budget,
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
            if (capacityError) {
              capacityFailure ??= completedError;
              continue;
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

          const result = completed.result as MergedTranslationResult;
          for (const windowResult of result.windows) {
            const window = claimed.get(windowResult.windowId) as PersistedLosslessWindow;
            const boundaryErrors = boundaryFailuresByWindow.get(window.windowId);
            if (windowResult.status === "failed" || boundaryErrors !== undefined) {
              const error = boundaryErrors === undefined
                ? windowResult.error ?? "invalid batch window submission"
                : `cross-request boundary validation failed: ${boundaryErrors.join("; ")}`;
              const retry = window.attemptCount < maxAttempts;
              store.failWindow(runId, window.windowId, {
                error,
                retry,
                budget: persistedBudgetFor(
                  window,
                  completed.budget,
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

            for (const translation of windowResult.translations) {
              acceptedWaveTranslations.set(translation.blockId, {
                ...translation,
                windowId: window.windowId,
              });
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
                completed.budget,
                completed.request.windows[0]?.windowId === window.windowId,
              ),
              warnings,
              conceptBindings: {
                usages: windowResult.termUsages,
                concepts: conceptsFromStableTerms(activeTerms),
              },
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
            if (coordinator.takeRetryWindowIds().length > 0) {
              freshWaveRequired = true;
            }
          }
        }
        if (capacityFailure !== undefined) {
          throw capacityFailure;
        }
        if (freshWaveRequired) {
          store.recoverInterruptedWindows(runId);
          retryWindows = [];
        } else {
          retryWindows = nextRetries;
        }
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
      const completedWindowIds = freshWaveRequired
        ? new Set(store.allWindows(runId)
            .filter((window) =>
              window.status === "completed"
              || window.status === "completed_with_warnings")
            .map((window) => window.windowId))
        : undefined;
      processedWindows += completedWindowIds === undefined
        ? selected.length
        : selected.filter((window) =>
            completedWindowIds.has(window.windowId)).length;
      if (runtimeSet.mode === "fast") {
        const hasUnresolvedKnowledge = store.latestKnowledgeSnapshot(runId).revisions
          .some((revision) => revision.status === "needs_revalidate");
        const waveWasUnstable = retryRound > 1
          || freshWaveRequired
          || entityLinkWarnings.length > 0
          || hasUnresolvedKnowledge;
        fastWaveHorizonMultiplier = waveWasUnstable ? 1 : 2;
      }
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
      revalidationOverhead: {
        coverageScan,
        drain: revalidationDrain,
      },
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
