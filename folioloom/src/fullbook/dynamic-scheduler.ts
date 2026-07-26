import type {
  OptimizationProfile,
  SchedulerMode,
} from "./optimization-policy.js";
import {
  planRollingHorizon,
  type PlannedTaskDispatch,
  type RollingPlannerInput,
  type RollingPlannerResult,
  type TaskExecutionVariant,
} from "./rolling-horizon-planner.js";
import type { OnlineRuntimeCostModel } from "./runtime-cost-model.js";
import { TaskGraphIntegrityError } from "./task-graph.js";
import type { RuntimeProfileStore } from "../storage/runtime-profile-store.js";

export interface DynamicSchedulerOptions {
  readonly mode: SchedulerMode;
  readonly profile: OptimizationProfile;
  readonly planner: typeof planRollingHorizon;
  readonly costModel: OnlineRuntimeCostModel;
  readonly profileStore?: RuntimeProfileStore;
}

export interface DynamicSchedulerDecisionMetadata {
  readonly decisionId: string;
  readonly runId: string;
  readonly createdAt: string;
}

export interface DynamicSchedulerDispatchOptions {
  readonly legacyTaskIds: readonly string[];
  readonly decision?: DynamicSchedulerDecisionMetadata;
}

export interface SchedulerDispatchReport {
  readonly mode: SchedulerMode;
  readonly planningStatus:
    | "disabled"
    | "shadow"
    | "optimal"
    | "bounded"
    | "fallback";
  readonly dispatchedTaskIds: readonly string[];
  readonly predictedWallTimeMs: number;
  readonly predictedTokens: number;
  readonly validatorsSkipped: 0;
  readonly contextProfiles: Readonly<Record<
    "lean" | "balanced" | "rich",
    number
  >>;
  readonly shadowDecision?: RollingPlannerResult;
  readonly fallbackReason?: string;
}

export interface SchedulerRunReport {
  readonly mode: SchedulerMode;
  readonly profile: OptimizationProfile;
  readonly decisions: number;
  readonly fallbacks: number;
  readonly predictedWallTimeMs: number;
  readonly actualWallTimeMs: number;
  readonly predictedTokens: number;
  readonly actualTokens: number;
  readonly tokenUsageComplete: boolean;
}

function variantsById(
  variants: readonly TaskExecutionVariant[],
): ReadonlyMap<string, TaskExecutionVariant> {
  return new Map(variants.map((variant) => [variant.variantId, variant]));
}

function firstVariantByTaskId(
  variants: readonly TaskExecutionVariant[],
): ReadonlyMap<string, TaskExecutionVariant> {
  const result = new Map<string, TaskExecutionVariant>();
  for (const variant of [...variants].sort((left, right) =>
    left.variantId.localeCompare(right.variantId, "en"))) {
    if (!result.has(variant.taskId)) {
      result.set(variant.taskId, variant);
    }
  }
  return result;
}

function legacyDispatch(
  input: RollingPlannerInput,
  taskIds: readonly string[],
): readonly PlannedTaskDispatch[] {
  const variantByTaskId = firstVariantByTaskId(input.variants);
  return taskIds.map((taskId) => ({
    taskId,
    variantId: variantByTaskId.get(taskId)?.variantId ?? "",
  }));
}

function contextProfileCounts(
  input: RollingPlannerInput,
  dispatch: readonly PlannedTaskDispatch[],
): Readonly<Record<"lean" | "balanced" | "rich", number>> {
  const result = { lean: 0, balanced: 0, rich: 0 };
  const variantById = variantsById(input.variants);
  const variantByTaskId = firstVariantByTaskId(input.variants);
  for (const item of dispatch) {
    const variant = variantById.get(item.variantId)
      ?? variantByTaskId.get(item.taskId);
    if (variant !== undefined) {
      result[variant.contextProfile] += 1;
    }
  }
  return Object.freeze(result);
}

function legacyProjection(
  input: RollingPlannerInput,
  dispatch: readonly PlannedTaskDispatch[],
): {
  readonly wallTimeMs: number;
  readonly totalTokens: number;
} {
  const variantById = variantsById(input.variants);
  const variantByTaskId = firstVariantByTaskId(input.variants);
  const selected = dispatch
    .map((item) =>
      variantById.get(item.variantId) ?? variantByTaskId.get(item.taskId))
    .filter((variant): variant is TaskExecutionVariant =>
      variant !== undefined);
  return {
    wallTimeMs: selected.reduce(
      (maximum, variant) =>
        Math.max(maximum, variant.predicted.p90DurationMs),
      0,
    ),
    totalTokens: selected.reduce(
      (total, variant) => total + variant.predicted.totalTokens,
      0,
    ),
  };
}

export class DynamicScheduler {
  readonly mode: SchedulerMode;
  readonly profile: OptimizationProfile;
  readonly costModel: OnlineRuntimeCostModel;
  readonly #planner: typeof planRollingHorizon;
  readonly #profileStore?: RuntimeProfileStore;

  constructor(options: DynamicSchedulerOptions) {
    this.mode = options.mode;
    this.profile = options.profile;
    this.costModel = options.costModel;
    this.#planner = options.planner;
    this.#profileStore = options.profileStore;
  }

  dispatch(
    input: RollingPlannerInput,
    options: DynamicSchedulerDispatchOptions,
  ): SchedulerDispatchReport {
    const legacy = legacyDispatch(input, options.legacyTaskIds);
    const legacyPrediction = legacyProjection(input, legacy);
    if (this.mode === "off") {
      return Object.freeze({
        mode: this.mode,
        planningStatus: "disabled",
        dispatchedTaskIds: Object.freeze([...options.legacyTaskIds]),
        predictedWallTimeMs: legacyPrediction.wallTimeMs,
        predictedTokens: legacyPrediction.totalTokens,
        validatorsSkipped: 0,
        contextProfiles: contextProfileCounts(input, legacy),
      });
    }

    try {
      const decision = this.#planner(input);
      const plannedDispatch = decision.firstDispatch;
      const useLegacy = this.mode === "shadow"
        || decision.planningStatus === "fallback";
      const selectedDispatch = useLegacy ? legacy : plannedDispatch;
      const report: SchedulerDispatchReport = Object.freeze({
        mode: this.mode,
        planningStatus: this.mode === "shadow"
          ? "shadow"
          : decision.planningStatus,
        dispatchedTaskIds: Object.freeze(
          selectedDispatch.map((item) => item.taskId),
        ),
        predictedWallTimeMs: decision.predictedWallTimeMs,
        predictedTokens: decision.predictedTotalTokens,
        validatorsSkipped: 0,
        contextProfiles: contextProfileCounts(
          input,
          plannedDispatch.length > 0 ? plannedDispatch : legacy,
        ),
        ...(this.mode === "shadow" ? { shadowDecision: decision } : {}),
        ...(decision.planningStatus === "fallback"
          ? { fallbackReason: "NO_LEGAL_PLAN" }
          : {}),
      });
      this.#recordDecision(report, options);
      return report;
    } catch (error) {
      if (error instanceof TaskGraphIntegrityError) {
        throw error;
      }
      const report: SchedulerDispatchReport = Object.freeze({
        mode: this.mode,
        planningStatus: "fallback",
        dispatchedTaskIds: Object.freeze([...options.legacyTaskIds]),
        predictedWallTimeMs: legacyPrediction.wallTimeMs,
        predictedTokens: legacyPrediction.totalTokens,
        validatorsSkipped: 0,
        contextProfiles: contextProfileCounts(input, legacy),
        fallbackReason: "PLANNER_FAILED",
      });
      this.#recordDecision(report, options);
      return report;
    }
  }

  #recordDecision(
    report: SchedulerDispatchReport,
    options: DynamicSchedulerDispatchOptions,
  ): void {
    if (this.#profileStore === undefined || options.decision === undefined) {
      return;
    }
    this.#profileStore.appendDecision({
      ...options.decision,
      mode: this.mode,
      profile: this.profile,
      predicted: {
        durationMs: report.predictedWallTimeMs,
        totalTokens: report.predictedTokens,
      },
      selected: {
        taskIds: report.shadowDecision?.firstDispatch.map(
          (dispatch) => dispatch.taskId,
        ) ?? report.dispatchedTaskIds,
        concurrency: Math.max(
          1,
          report.shadowDecision?.firstDispatch.length
            ?? report.dispatchedTaskIds.length,
        ),
      },
    });
  }
}
