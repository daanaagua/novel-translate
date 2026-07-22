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
import type { ModelProfile } from "../providers/types.js";
import { SourceLedger } from "../source/source-ledger.js";
import {
  LosslessBookStore,
  type LosslessAuditState,
  type StoredTranslationRun,
} from "../storage/lossless-book-store.js";

const TRIAL_SCHEMA = "folioloom-desktop-trial-1";

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
}

export interface DesktopTrialRuntimeResolver {
  resolve(): Promise<DesktopTrialRuntime | undefined>;
}

export interface DesktopTrialStartRequest {
  manifestPath: string;
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
  return { manifestPath, projectDirectory: resolve(dirname(manifestPath)) };
}

function normalizedProfile(runtime: DesktopTrialRuntime): NormalizedTrialProfile {
  const profile: NormalizedTrialProfile = {
    providerId: nonempty(runtime.profile?.providerId, "active provider id"),
    modelId: nonempty(runtime.profile?.modelId, "active model id"),
    ...(runtime.profile?.reasoningEffort === undefined ? {} : {
      reasoningEffort: nonempty(runtime.profile.reasoningEffort, "active reasoning effort"),
    }),
    ...(runtime.profile?.customBaseUrl === undefined ? {} : {
      customBaseUrl: nonempty(runtime.profile.customBaseUrl, "active custom base URL"),
    }),
  };
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

function profileFingerprint(profile: NormalizedTrialProfile): string {
  return JSON.stringify(profile);
}

function isCompletedTrial(
  run: StoredTranslationRun,
  sourceVersion: string,
  profile: NormalizedTrialProfile,
): boolean {
  if (run.sourceVersion !== sourceVersion || run.modelId !== profile.modelId) {
    return false;
  }
  const metadata = record(run.metadata);
  const trial = record(metadata?.desktopTrial);
  return trial?.schema === TRIAL_SCHEMA
    && trial.profileFingerprint === profileFingerprint(profile);
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
  profile: NormalizedTrialProfile,
): DesktopTrialResult | undefined {
  if (!existsSync(storePath)) {
    return undefined;
  }
  const store = LosslessBookStore.openReadOnly(storePath);
  try {
    const matchingRuns = store.listTranslationRuns()
      .filter((run) => isCompletedTrial(run, sourceVersion, profile));
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
    const running = this.#start(paths.manifestPath, paths.projectDirectory, active.controller)
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
    const profile = normalizedProfile(runtime);
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
      previous = storedTrialResult(storePath, afterRuntime.sourceVersion, profile);
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
        modelId: profile.modelId,
        metadata: {
          desktopTrial: {
            schema: TRIAL_SCHEMA,
            profileFingerprint: profileFingerprint(profile),
          },
        },
      },
      model: runtime.model,
      streamFn: runtime.streamFn,
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
