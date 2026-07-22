import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";

import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

import {
  LOSSLESS_BOOK_PROTOCOL_VERSION,
  runBook,
  type LosslessBookRunOptions,
  type LosslessBookRunResult,
} from "../fullbook/book-runner.js";
import type { TranslationRuntime, TranslationRuntimeSet } from "../fullbook/types.js";
import type { ModelProfile, ProviderEffort } from "../providers/types.js";
import { SourceLedger } from "../source/source-ledger.js";
import {
  LosslessBookStore,
  type LosslessAuditState,
  type StoredTranslationRun,
} from "../storage/lossless-book-store.js";
import type { DesktopTrialMode } from "./contracts.js";
import {
  explicitThinkingLevel,
  isProviderEffort,
  lowestLegalFastEffort,
} from "./trial-runtime-policy.js";

const TRIAL_SCHEMA = "folioloom-desktop-trial-2";

export type DesktopTrialErrorCode =
  | "DESKTOP_TRIAL_ALREADY_RUNNING"
  | "DESKTOP_TRIAL_CANCELLED"
  | "DESKTOP_TRIAL_INPUT_INVALID"
  | "DESKTOP_TRIAL_MODEL_NOT_READY"
  | "DESKTOP_TRIAL_RUNTIME_MISMATCH"
  | "DESKTOP_TRIAL_SOURCE_CHANGED"
  | "DESKTOP_TRIAL_RESULT_UNAVAILABLE";

export class DesktopTrialError extends Error {
  constructor(readonly code: DesktopTrialErrorCode, message: string) {
    super(message);
    this.name = "DesktopTrialError";
  }
}

/**
 * This boundary is intentionally main-process only. It supplies the already
 * tested active model without exposing a credential, provider URL, or runtime
 * implementation to the renderer-facing service contract.
 */
export interface DesktopTrialRuntime {
  profile: ModelProfile;
  model: Model<any>;
  streamFn: StreamFn;
  /** Efforts the selected provider has declared legal for this profile. */
  supportedEfforts: readonly ProviderEffort[];
  /** Create an isolated runtime projection without exposing credentials. */
  createWithProfile(profile: ModelProfile): DesktopTrialRuntime;
}

export interface DesktopTrialRuntimeResolver {
  resolve(): Promise<DesktopTrialRuntime | undefined>;
}

export interface DesktopTrialStartRequest {
  manifestPath: string;
  mode: DesktopTrialMode;
}

export interface DesktopTrialResult {
  runId: string;
  sourceText: string;
  translationText: string;
}

export type DesktopTrialProgressStage =
  | "preparing"
  | "translating"
  | "checking"
  | "completed"
  | "failed";

export type DesktopTrialBookRunner = (
  options: LosslessBookRunOptions,
) => Promise<LosslessBookRunResult>;

export interface DesktopTrialServiceOptions {
  runtime: DesktopTrialRuntimeResolver;
  runBook?: DesktopTrialBookRunner;
  createRunId?: () => string;
  onProgress?: (stage: DesktopTrialProgressStage) => void;
}

interface ActiveTrial {
  controller: AbortController;
  settled: Promise<void>;
}

interface NormalizedTrialProfile {
  providerId: string;
  modelId: string;
  reasoningEffort?: string;
  customBaseUrl?: string;
}

interface TrialRuntimeFingerprint {
  mode: DesktopTrialMode;
  primary: NormalizedTrialProfile;
  escalation: NormalizedTrialProfile;
}

interface TrialRuntimePlan {
  runtimeSet: TranslationRuntimeSet;
  fingerprint: TrialRuntimeFingerprint;
}

const ACTIVE_TRIALS = new Map<string, ActiveTrial>();

function nonempty(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new DesktopTrialError("DESKTOP_TRIAL_RUNTIME_MISMATCH", `${label} must be non-empty`);
  }
  return value.trim();
}

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function manifestAndProject(request: DesktopTrialStartRequest): {
  manifestPath: string;
  projectDirectory: string;
  mode: DesktopTrialMode;
} {
  if (typeof request?.manifestPath !== "string" || !isAbsolute(request.manifestPath)) {
    throw new DesktopTrialError(
      "DESKTOP_TRIAL_INPUT_INVALID",
      "desktop trial requires an absolute source manifest path",
    );
  }
  const manifestPath = resolve(request.manifestPath);
  if (basename(manifestPath) !== "source_manifest.json") {
    throw new DesktopTrialError(
      "DESKTOP_TRIAL_INPUT_INVALID",
      "desktop trial requires source_manifest.json",
    );
  }
  if (request?.mode !== "quality" && request?.mode !== "fast") {
    throw new DesktopTrialError(
      "DESKTOP_TRIAL_INPUT_INVALID",
      "desktop trial mode must be quality or fast",
    );
  }
  return { manifestPath, projectDirectory: resolve(dirname(manifestPath)), mode: request.mode };
}

function normalizedModelProfile(
  raw: ModelProfile,
  label = "active",
): NormalizedTrialProfile {
  return {
    providerId: nonempty(raw?.providerId, `${label} provider id`),
    modelId: nonempty(raw?.modelId, `${label} model id`),
    ...(raw?.reasoningEffort === undefined ? {} : {
      reasoningEffort: nonempty(raw.reasoningEffort, `${label} reasoning effort`),
    }),
    ...(raw?.customBaseUrl === undefined ? {} : {
      customBaseUrl: nonempty(raw.customBaseUrl, `${label} custom base URL`),
    }),
  };
}

function normalizedProfile(runtime: DesktopTrialRuntime): NormalizedTrialProfile {
  const profile = normalizedModelProfile(runtime.profile);
  if (runtime.model === undefined || runtime.model.id !== profile.modelId) {
    throw new DesktopTrialError(
      "DESKTOP_TRIAL_RUNTIME_MISMATCH",
      "active model profile does not match the resolved translation runtime",
    );
  }
  if (typeof runtime.streamFn !== "function") {
    throw new DesktopTrialError(
      "DESKTOP_TRIAL_RUNTIME_MISMATCH",
      "active translation runtime has no stream function",
    );
  }
  return profile;
}

function profileWithEffort(
  profile: ModelProfile,
  effort: ProviderEffort | undefined,
): ModelProfile {
  return {
    providerId: profile.providerId,
    modelId: profile.modelId,
    ...(effort === undefined ? {} : { reasoningEffort: effort }),
    ...(profile.customBaseUrl === undefined ? {} : { customBaseUrl: profile.customBaseUrl }),
  };
}

function sameProfile(left: NormalizedTrialProfile, right: NormalizedTrialProfile): boolean {
  return left.providerId === right.providerId
    && left.modelId === right.modelId
    && left.reasoningEffort === right.reasoningEffort
    && left.customBaseUrl === right.customBaseUrl;
}

function translationRuntime(runtime: DesktopTrialRuntime): TranslationRuntime {
  return {
    model: runtime.model,
    streamFn: runtime.streamFn,
    ...(runtime.profile.reasoningEffort === undefined ? {} : {
      effort: runtime.profile.reasoningEffort,
    }),
    thinkingLevel: explicitThinkingLevel(runtime.profile.reasoningEffort),
  };
}

function runtimeFingerprint(fingerprint: TrialRuntimeFingerprint): string {
  return JSON.stringify(fingerprint);
}

function requireSupportedEfforts(runtime: DesktopTrialRuntime): readonly ProviderEffort[] {
  if (!Array.isArray(runtime.supportedEfforts)
    || runtime.supportedEfforts.some((effort) => !isProviderEffort(effort))) {
    throw new DesktopTrialError(
      "DESKTOP_TRIAL_RUNTIME_MISMATCH",
      "active translation runtime has invalid provider effort capabilities",
    );
  }
  return runtime.supportedEfforts;
}

function derivedRuntime(
  qualityRuntime: DesktopTrialRuntime,
  profile: ModelProfile,
): DesktopTrialRuntime {
  if (typeof qualityRuntime.createWithProfile !== "function") {
    throw new DesktopTrialError(
      "DESKTOP_TRIAL_RUNTIME_MISMATCH",
      "active translation runtime cannot derive a provider-safe effort projection",
    );
  }
  const derived = qualityRuntime.createWithProfile(profile);
  const expected = normalizedModelProfile(profile, "derived");
  const actual = normalizedProfile(derived);
  if (!sameProfile(actual, expected)) {
    throw new DesktopTrialError(
      "DESKTOP_TRIAL_RUNTIME_MISMATCH",
      "derived translation runtime does not match the requested provider profile",
    );
  }
  return derived;
}

function buildRuntimePlan(
  mode: DesktopTrialMode,
  qualityRuntime: DesktopTrialRuntime,
): TrialRuntimePlan {
  const qualityProfile = normalizedProfile(qualityRuntime);
  const quality = translationRuntime(qualityRuntime);
  if (mode === "quality") {
    const fingerprint: TrialRuntimeFingerprint = {
      mode,
      primary: qualityProfile,
      escalation: qualityProfile,
    };
    return {
      runtimeSet: { mode, primary: quality, escalation: quality },
      fingerprint,
    };
  }

  const primaryEffort = lowestLegalFastEffort(requireSupportedEfforts(qualityRuntime));
  const primaryProfile = profileWithEffort(qualityRuntime.profile, primaryEffort);
  const primaryRuntime = primaryEffort === qualityRuntime.profile.reasoningEffort
    ? qualityRuntime
    : derivedRuntime(qualityRuntime, primaryProfile);
  const primary = translationRuntime(primaryRuntime);
  const fingerprint: TrialRuntimeFingerprint = {
    mode,
    primary: normalizedProfile(primaryRuntime),
    escalation: qualityProfile,
  };
  return {
    runtimeSet: { mode, primary, escalation: quality },
    fingerprint,
  };
}

function isCompletedTrial(
  run: StoredTranslationRun,
  sourceVersion: string,
  fingerprint: TrialRuntimeFingerprint,
): boolean {
  if (run.sourceVersion !== sourceVersion || run.modelId !== fingerprint.primary.modelId) {
    return false;
  }
  const metadata = record(run.metadata);
  const trial = record(metadata?.desktopTrial);
  return trial?.schema === TRIAL_SCHEMA
    && trial.runtimeFingerprint === runtimeFingerprint(fingerprint);
}

function projectResult(state: LosslessAuditState): DesktopTrialResult | undefined {
  const translation = state.translations.find((entry) => entry.active && entry.text.trim().length > 0);
  if (translation === undefined) {
    return undefined;
  }
  const source = state.blocks.find((block) => block.blockId === translation.blockId);
  if (source === undefined) {
    return undefined;
  }
  return {
    runId: state.runId,
    sourceText: source.sourceText,
    translationText: translation.text,
  };
}

function storedTrialResult(
  storePath: string,
  sourceVersion: string,
  fingerprint: TrialRuntimeFingerprint,
): DesktopTrialResult | undefined {
  if (!existsSync(storePath)) {
    return undefined;
  }
  const store = LosslessBookStore.openReadOnly(storePath);
  try {
    const matchingRuns = store.listTranslationRuns()
      .filter((run) => isCompletedTrial(run, sourceVersion, fingerprint));
    for (let index = matchingRuns.length - 1; index >= 0; index -= 1) {
      const result = projectResult(store.auditState(matchingRuns[index]!.runId));
      if (result !== undefined) {
        return result;
      }
    }
    return undefined;
  } finally {
    store.close();
  }
}

/**
 * Runs exactly one lossless window for a project. The per-project in-process
 * lease complements the runner's file lease so a double click cannot begin two
 * incompatible trial tasks before SQLite receives either one.
 */
export class DesktopTrialService {
  readonly #runtime: DesktopTrialRuntimeResolver;
  readonly #runBook: DesktopTrialBookRunner;
  readonly #createRunId: () => string;
  readonly #onProgress: ((stage: DesktopTrialProgressStage) => void) | undefined;
  readonly #ownedTrials = new Set<ActiveTrial>();

  constructor(options: DesktopTrialServiceOptions) {
    this.#runtime = options.runtime;
    this.#runBook = options.runBook ?? runBook;
    this.#createRunId = options.createRunId ?? randomUUID;
    this.#onProgress = options.onProgress;
  }

  start(request: DesktopTrialStartRequest): Promise<DesktopTrialResult> {
    let paths: ReturnType<typeof manifestAndProject>;
    try {
      paths = manifestAndProject(request);
    } catch (error) {
      return Promise.reject(error);
    }
    if (ACTIVE_TRIALS.has(paths.projectDirectory)) {
      return Promise.reject(new DesktopTrialError(
        "DESKTOP_TRIAL_ALREADY_RUNNING",
        "a desktop trial is already running for this project",
      ));
    }
    const active: ActiveTrial = {
      controller: new AbortController(),
      settled: Promise.resolve(),
    };
    ACTIVE_TRIALS.set(paths.projectDirectory, active);
    this.#ownedTrials.add(active);
    this.#emitProgress("preparing");
    const running = this.#start(paths.manifestPath, paths.projectDirectory, paths.mode, active.controller)
      .then((result) => {
        this.#emitProgress("completed");
        return result;
      }, (error: unknown) => {
        this.#emitProgress("failed");
        throw error;
      })
      .finally(() => {
        if (ACTIVE_TRIALS.get(paths.projectDirectory) === active) {
          ACTIVE_TRIALS.delete(paths.projectDirectory);
        }
        this.#ownedTrials.delete(active);
      });
    active.settled = running.then(
      () => undefined,
      () => undefined,
    );
    return running;
  }

  async cancel(): Promise<void> {
    const activeTrials = [...this.#ownedTrials];
    for (const trial of activeTrials) {
      if (!trial.controller.signal.aborted) {
        trial.controller.abort(new DesktopTrialError(
          "DESKTOP_TRIAL_CANCELLED",
          "desktop trial cancelled",
        ));
      }
    }
    await Promise.all(activeTrials.map((trial) => trial.settled));
  }

  async #start(
    manifestPath: string,
    projectDirectory: string,
    mode: DesktopTrialMode,
    controller: AbortController,
  ): Promise<DesktopTrialResult> {
    controller.signal.throwIfAborted();
    const beforeRuntime = SourceLedger.open(manifestPath);
    const runtime = await this.#runtime.resolve();
    controller.signal.throwIfAborted();
    if (runtime === undefined) {
      throw new DesktopTrialError(
        "DESKTOP_TRIAL_MODEL_NOT_READY",
        "a successfully tested model is required before starting a desktop trial",
      );
    }
    const plan = buildRuntimePlan(mode, runtime);
    const afterRuntime = SourceLedger.open(manifestPath);
    if (beforeRuntime.sourceVersion !== afterRuntime.sourceVersion) {
      throw new DesktopTrialError(
        "DESKTOP_TRIAL_SOURCE_CHANGED",
        "the manuscript changed while the translation runtime was being prepared",
      );
    }
    const storePath = join(projectDirectory, "artifacts", "folioloom", "book.db");
    let previous: DesktopTrialResult | undefined;
    if (existsSync(storePath)) {
      this.#emitProgress("checking");
      previous = storedTrialResult(storePath, afterRuntime.sourceVersion, plan.fingerprint);
    }
    controller.signal.throwIfAborted();
    if (previous !== undefined) {
      return previous;
    }

    mkdirSync(dirname(storePath), { recursive: true });
    const runId = this.#createRunId();
    controller.signal.throwIfAborted();
    this.#emitProgress("translating");
    const result = await this.#runBook({
      manifestPath,
      storePath,
      runMeta: {
        runId,
        protocolVersion: LOSSLESS_BOOK_PROTOCOL_VERSION,
        modelId: plan.runtimeSet.primary.model.id,
        metadata: {
          desktopTrial: {
            schema: TRIAL_SCHEMA,
            runtimeFingerprint: runtimeFingerprint(plan.fingerprint),
          },
        },
      },
      model: plan.runtimeSet.primary.model,
      streamFn: plan.runtimeSet.primary.streamFn,
      runtimeSet: plan.runtimeSet,
      maxWindows: 1,
      maxConcurrency: 1,
      maxWindowsPerRequest: 1,
      signal: controller.signal,
    });
    controller.signal.throwIfAborted();
    this.#emitProgress("checking");
    const store = LosslessBookStore.openReadOnly(storePath);
    try {
      const projected = projectResult(store.auditState(result.runId));
      if (projected === undefined) {
        throw new DesktopTrialError(
          "DESKTOP_TRIAL_RESULT_UNAVAILABLE",
          "the trial finished without a committed translation",
        );
      }
      return projected;
    } finally {
      store.close();
    }
  }

  #emitProgress(stage: DesktopTrialProgressStage): void {
    try {
      this.#onProgress?.(stage);
    } catch {
      // Progress observers are presentation plumbing and must never alter a
      // durable trial's control flow.
    }
  }
}
