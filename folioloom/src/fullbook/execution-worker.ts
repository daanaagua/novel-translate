import { createHash } from "node:crypto";

import {
  ModelProviderError,
  type PiRunResult,
} from "../agents/pi-runtime.js";
import {
  runTranslationBatch,
  type TranslationBatchResult,
  type TranslationBatchWindowResult,
} from "../agents/translation-batch.js";
import type { TranslationRequestInput } from "../agents/translation-request.js";
import { BudgetLedger } from "../kernel/budget.js";
import type { SourceLanguageProfile } from "../language/types.js";
import type { LosslessBlock } from "../source/types.js";
import {
  type UsageObservation,
  type WeightedTokenEstimator,
} from "../source/token-estimator.js";
import type { SchedulerObservationStatus } from "./adaptive-scheduler.js";
import {
  AdmissionController,
  BookTokenEnvelopeExceededError,
} from "./admission-controller.js";
import { packPhysicalRequests } from "./request-batcher.js";
import {
  RequestBudgeter,
  type RequestBudgetAssessment,
} from "./request-budgeter.js";
import type { RuntimeFeatures } from "./runtime-cost-model.js";
import type { TaskExecutionVariant } from "./rolling-horizon-planner.js";
import {
  normalizeRuntimeUsage,
  type NormalizedRuntimeUsage,
  type RuntimeObservationStatus,
} from "./runtime-telemetry.js";
import type {
  PhysicalRequestPlan,
  RequestBatchWindow,
  TranslationRuntime,
  TranslationRuntimeSet,
} from "./types.js";

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

const CONTEXT_FRAGMENT_PROTOCOL_VERSION = "v5-context-fragment-1";
export const MAX_CONTEXT_SPLIT_DEPTH = 16;
export const MAX_CONTEXT_SPLIT_ATTEMPTS = 32;
export const MAX_PROTOCOL_SPLIT_ATTEMPTS = 16;

export interface AdmittedRequestFragment<TInput> {
  request: PhysicalRequestPlan;
  input: TInput;
  assessment: RequestBudgetAssessment;
  /** Every split raises this number, so recovery cannot re-enqueue the same shape forever. */
  depth: number;
}

export interface AdmittedTranslationRequest<TInput> {
  /** Original physical grouping: the durable claim/stage/promote boundary. */
  readonly request: PhysicalRequestPlan;
  /** Context-safe provider calls executed serially under one scheduler permit. */
  readonly fragments: readonly AdmittedRequestFragment<TInput>[];
  /** Largest serial fragment reservation used by the outer schedulers. */
  readonly assessment: RequestBudgetAssessment;
}

export interface PlannedTranslationExecution {
  readonly admitted: AdmittedTranslationRequest<TranslationRequestInput>;
  readonly runtime: TranslationRuntime;
  readonly buildInput: (
    request: PhysicalRequestPlan,
  ) => TranslationRequestInput;
  readonly features: RuntimeFeatures;
  readonly variant: TaskExecutionVariant;
}

export interface MergedTranslationResult {
  windows: TranslationBatchWindowResult[];
  responseErrors: string[];
}

export interface CompletedTranslationRequest {
  request: PhysicalRequestPlan;
  budget: Readonly<Record<string, number>>;
  result?: MergedTranslationResult;
  error?: unknown;
  runtime: {
    readonly durationMs: number;
    readonly usage: NormalizedRuntimeUsage;
    readonly observationDurationMs: number;
    readonly observationUsage: NormalizedRuntimeUsage;
    readonly status: RuntimeObservationStatus;
    readonly features: RuntimeFeatures;
    readonly variant: TaskExecutionVariant;
    readonly recoveries: readonly {
      readonly durationMs: number;
      readonly usage: NormalizedRuntimeUsage;
      readonly status: Exclude<RuntimeObservationStatus, "success">;
      readonly protocol: "typed_tool" | "framed_text";
    }[];
  };
}

export interface ScheduledResult<T> {
  value: T;
  status: SchedulerObservationStatus;
}

export interface TranslationExecutionDeps {
  readonly admission: AdmissionController;
  readonly runtimeSet: TranslationRuntimeSet;
  readonly estimator: Pick<WeightedTokenEstimator, "observeUsage"> & WeightedTokenEstimator;
  readonly languageProfile: SourceLanguageProfile;
  readonly blockById: ReadonlyMap<string, LosslessBlock>;
  readonly signal?: AbortSignal;
  readonly hardDeadlineMs?: number;
  readonly retryRound: number;
}

interface FragmentTranslationExecution {
  fragment: AdmittedRequestFragment<TranslationRequestInput>;
  result: TranslationBatchResult;
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  signal?.throwIfAborted();
}

export function reasoningReserveTokens(runtime: TranslationRuntime): number {
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

export function outputReserveTokens(
  request: PhysicalRequestPlan,
  runtime: TranslationRuntime,
): number {
  return Math.min(
    runtime.model.maxTokens,
    Math.max(768, Math.ceil(request.sourceTokens * 1.6) + 512),
  );
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

export function splitSingleWindowRequestAtBlocks(
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
export function splitPhysicalRequestAtBoundaries(
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

export function assessTranslationFragment<TInput extends TranslationRequestInput>(
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

export function capacityAssessmentForProviderContext(
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

export function admitTranslationFragments<TInput extends TranslationRequestInput>(
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

export function admitTranslationRequests<TInput extends TranslationRequestInput>(
  requests: readonly PhysicalRequestPlan[],
  runtime: TranslationRuntime,
  estimator: WeightedTokenEstimator,
  blockById: ReadonlyMap<string, LosslessBlock>,
  buildInput: (request: PhysicalRequestPlan) => TInput,
): AdmittedTranslationRequest<TInput>[] {
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

export function runtimeUsageForRuns(
  runs: readonly PiRunResult[],
): NormalizedRuntimeUsage {
  if (runs.length === 0) {
    return normalizeRuntimeUsage({});
  }
  return normalizeRuntimeUsage({
    input: runs.reduce((total, run) => total + run.usage.input, 0),
    output: runs.reduce((total, run) => total + run.usage.output, 0),
    cacheRead: runs.reduce((total, run) => total + run.usage.cacheRead, 0),
    cacheWrite: runs.reduce((total, run) => total + run.usage.cacheWrite, 0),
    reasoning: runs.reduce(
      (total, run) => total + (run.usage.reasoning ?? 0),
      0,
    ),
    totalTokens: runs.reduce(
      (total, run) => total + run.usage.totalTokens,
      0,
    ),
  });
}

export function runtimeObservationStatus(
  status: SchedulerObservationStatus,
): RuntimeObservationStatus {
  return status === "busy" ? "throttled" : status;
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

export function recoverableStructuralWindowIds(result: TranslationBatchResult): Set<string> {
  return failedWindowIdsMatching(result, RECOVERABLE_STRUCTURAL_SUBMISSION_ERROR);
}

export function recoverableDegenerationWindowIds(result: TranslationBatchResult): Set<string> {
  return failedWindowIdsMatching(result, RECOVERABLE_LOCAL_DEGENERATION_ERROR);
}

export function requestWithWindows(
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

export function mergeFragmentTranslationResults(
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

export async function executePlannedTranslationRequest(
  execution: PlannedTranslationExecution,
  deps: TranslationExecutionDeps,
): Promise<ScheduledResult<CompletedTranslationRequest>> {
  const {
    admission,
    runtimeSet,
    estimator,
    languageProfile,
    blockById,
    signal,
    hardDeadlineMs,
    retryRound,
  } = deps;
  const {
    admitted: { request, fragments },
    runtime: selectedRuntime,
    buildInput: selectedBuildInput,
    features,
    variant,
  } = execution;
  const requestStartedAt = performance.now();
  const observedRuns: PiRunResult[] = [];
  const recoveryRuns = new Set<PiRunResult>();
  const recoveries: Array<{
    durationMs: number;
    usage: NormalizedRuntimeUsage;
    status: Exclude<RuntimeObservationStatus, "success">;
    protocol: "typed_tool" | "framed_text";
  }> = [];
  // Fragments are one logical provider operation. Reusing the ledger
  // preserves the original hard ceilings instead of multiplying every
  // budget limit by the number of context-recovery fragments.
  const budget = new BudgetLedger();
  let observedContextOverflow = false;
  let contextSplitAttempts = 0;
  let protocolSplitAttempts = 0;
  let secondaryChargeOrdinal = 0;
  const chargeSecondaryFragments = async (
    purpose: "repair" | "protocol_switch" | "context_split",
    admitted: readonly AdmittedRequestFragment<TranslationRequestInput>[],
    run: () => Promise<FragmentTranslationExecution[]>,
  ): Promise<FragmentTranslationExecution[]> => {
    const predictedTokens = Math.max(
      1,
      admitted.reduce(
        (total, item) => total + item.assessment.totalReserved,
        0,
      ),
    );
    const secondaryId = `${request.requestId}:${purpose}:${secondaryChargeOrdinal}`;
    secondaryChargeOrdinal += 1;
    const hold = admission.holdSecondary({
      requestId: secondaryId,
      purpose,
      taskIds: [request.requestId],
      predictedTokens,
      attempt: retryRound,
    });
    try {
      return await run();
    } finally {
      hold.release();
    }
  };
  const executeFragments = async (
    pendingFragments: readonly AdmittedRequestFragment<TranslationRequestInput>[],
    activeRuntime: TranslationRuntime = selectedRuntime,
    retryingLocally = false,
  ): Promise<FragmentTranslationExecution[]> => {
    const completed: FragmentTranslationExecution[] = [];
    for (const fragment of pendingFragments) {
      const fragmentStartedAt = performance.now();
      try {
        throwIfAborted(signal);
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
          signal,
          deadlineMs: hardDeadlineMs,
        });
        observedRuns.push(result.run, ...result.repairRuns);
        if (result.run.modelCalls === 1
          && fragment.assessment.inputTokens > 0
          && result.run.usage.input > 0) {
          const observation: UsageObservation = {
            modelId: activeRuntime.model.id,
            profile: languageProfile,
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
            const failedRuns = [
              result.run,
              ...result.repairRuns,
            ];
            failedRuns.forEach((run) => recoveryRuns.add(run));
            recoveries.push({
              durationMs: performance.now() - fragmentStartedAt,
              usage: runtimeUsageForRuns(failedRuns),
              status: "protocol",
              protocol: "typed_tool",
            });
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
              ...selectedBuildInput(request),
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
            completed.push(...await chargeSecondaryFragments(
              "protocol_switch",
              admittedFallback,
              () => executeFragments(
                admittedFallback,
                runtimeSet.escalation,
                true,
              ),
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
            const failedRuns = [
              result.run,
              ...result.repairRuns,
            ];
            failedRuns.forEach((run) => recoveryRuns.add(run));
            recoveries.push({
              durationMs: performance.now() - fragmentStartedAt,
              usage: runtimeUsageForRuns(failedRuns),
              status: structuralWindowIds.size > 0
                ? "protocol"
                : "failed",
              protocol: fragment.input.responseProtocol
                ?? "typed_tool",
            });
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
              selectedBuildInput,
              fragment.depth + 1,
            );
            completed.push(...await chargeSecondaryFragments(
              "context_split",
              admittedChildren,
              () => executeFragments(
                admittedChildren,
                runtimeSet.escalation,
                true,
              ),
            ));
            continue;
          }
        }
        completed.push({ fragment, result });
      } catch (error) {
        if (error instanceof BookTokenEnvelopeExceededError) {
          throw error;
        }
        if (error instanceof ModelProviderError
          && error.kind === "protocol"
          && fragment.input.responseProtocol === "typed_tool"
          && protocolSplitAttempts < MAX_PROTOCOL_SPLIT_ATTEMPTS) {
          recoveries.push({
            durationMs: performance.now() - fragmentStartedAt,
            usage: runtimeUsageForRuns([]),
            status: "protocol",
            protocol: "typed_tool",
          });
          protocolSplitAttempts += 1;
          const buildFramedTranslationInput = (
            request: PhysicalRequestPlan,
          ): TranslationRequestInput => ({
            ...selectedBuildInput(request),
            responseProtocol: "framed_text",
          });
          const admittedFallback = admitTranslationFragments(
            [fragment.request],
            activeRuntime,
            estimator,
            blockById,
            buildFramedTranslationInput,
            fragment.depth,
          );
          completed.push(...await chargeSecondaryFragments(
            "protocol_switch",
            admittedFallback,
            () => executeFragments(
              admittedFallback,
              activeRuntime,
              false,
            ),
          ));
          continue;
        }
        if (!(error instanceof ModelProviderError) || error.kind !== "context") {
          throw error;
        }
        observedContextOverflow = true;
        recoveries.push({
          durationMs: performance.now() - fragmentStartedAt,
          usage: runtimeUsageForRuns([]),
          status: "context",
          protocol: fragment.input.responseProtocol ?? "typed_tool",
        });
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
          selectedBuildInput,
          fragment.depth + 1,
        );
        completed.push(...await chargeSecondaryFragments(
          "context_split",
          admittedChildren,
          () => executeFragments(
            admittedChildren,
            activeRuntime,
            true,
          ),
        ));
      }
    }
    return completed;
  };
  const successfulObservationUsage = (): NormalizedRuntimeUsage =>
    runtimeUsageForRuns(observedRuns.filter((run) =>
      !recoveryRuns.has(run)));
  const successfulObservationDuration = (
    totalDurationMs: number,
  ): number => Math.max(
    0,
    totalDurationMs - recoveries.reduce(
      (total, recovery) => total + recovery.durationMs,
      0,
    ),
  );
  try {
    const executions = await executeFragments(fragments);
    const result = mergeFragmentTranslationResults(request, executions);
    const failed = result.windows.some((window) =>
      window.status === "failed");
    const schedulerStatus: SchedulerObservationStatus = failed
      ? "failed"
      : observedContextOverflow ? "context" : "success";
    const durationMs = performance.now() - requestStartedAt;
    const observedUsage = runtimeUsageForRuns(observedRuns);
    const usage = recoveries.some((recovery) =>
      !recovery.usage.complete)
      ? { ...observedUsage, complete: false }
      : observedUsage;
    return {
      value: {
        request,
        budget: budget.snapshot(),
        result,
        runtime: {
          durationMs,
          usage,
          observationDurationMs:
            successfulObservationDuration(durationMs),
          observationUsage: successfulObservationUsage(),
          status: failed ? "failed" : "success",
          features,
          variant,
          recoveries,
        },
      },
      status: schedulerStatus,
    };
  } catch (error) {
    const status: SchedulerObservationStatus = error instanceof ModelProviderError
      && (error.kind === "throttled"
        || error.kind === "timeout"
        || error.kind === "busy"
        || error.kind === "context")
      ? error.kind
      : "failed";
    const observedUsage = runtimeUsageForRuns(observedRuns);
    const usage = error instanceof ModelProviderError
      || recoveries.some((recovery) => !recovery.usage.complete)
      ? { ...observedUsage, complete: false }
      : observedUsage;
    const durationMs = performance.now() - requestStartedAt;
    const observationUsage = successfulObservationUsage();
    return {
      value: {
        request,
        budget: budget.snapshot(),
        error,
        runtime: {
          durationMs,
          usage,
          observationDurationMs:
            successfulObservationDuration(durationMs),
          observationUsage: error instanceof ModelProviderError
            ? { ...observationUsage, complete: false }
            : observationUsage,
          status: runtimeObservationStatus(status),
          features,
          variant,
          recoveries,
        },
      },
      status,
    };
  }
}
