import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { basename, dirname, extname, isAbsolute, join, resolve } from "node:path";

import {
  LOSSLESS_BOOK_PROTOCOL_VERSION,
  runBook,
  type LosslessBookRunOptions,
  type LosslessBookRunResult,
} from "../fullbook/book-runner.js";
import { SourceLedger } from "../source/source-ledger.js";
import {
  LosslessBookStore,
  type LosslessBookStatusSummary,
  type StoredTranslationRun,
} from "../storage/lossless-book-store.js";
import type {
  DesktopError,
  DesktopFullBookPhase,
  DesktopFullBookProgress,
  DesktopFullBookRunSnapshot,
  DesktopFullBookSnapshot,
  DesktopProjectRequest,
  DesktopResumeFullBookRequest,
  DesktopStartFullBookRequest,
  DesktopTrialMode,
} from "./contracts.js";
import { toDesktopError } from "./desktop-errors.js";
import {
  buildDesktopRuntimePlan,
  DesktopRuntimePlanError,
  serializeDesktopRuntimeFingerprint,
  type DesktopRuntimeFingerprint,
  type DesktopRuntimePlan,
  type DesktopRuntimeResolver,
} from "./desktop-runtime-plan.js";

const FULLBOOK_SCHEMA = "folioloom-desktop-fullbook-1";
const DEFAULT_STYLE_PROFILE_HASH = "desktop-default-style-1";
const DEFAULT_POLL_INTERVAL_MS = 750;

export type DesktopFullBookErrorCode =
  | "DESKTOP_FULLBOOK_ALREADY_RUNNING"
  | "DESKTOP_FULLBOOK_INPUT_INVALID"
  | "DESKTOP_FULLBOOK_MODEL_NOT_READY"
  | "DESKTOP_FULLBOOK_PAUSED"
  | "DESKTOP_FULLBOOK_RUN_NOT_FOUND"
  | "DESKTOP_FULLBOOK_RUNTIME_MISMATCH"
  | "DESKTOP_FULLBOOK_SOURCE_CHANGED";

export class DesktopFullBookError extends Error {
  constructor(readonly code: DesktopFullBookErrorCode, message: string) {
    super(message);
    this.name = "DesktopFullBookError";
  }
}

export interface DesktopFullBookServiceOptions {
  runtime: DesktopRuntimeResolver;
  runBook?: (options: LosslessBookRunOptions) => Promise<LosslessBookRunResult>;
  createRunId?: () => string;
  onProgress?: (progress: DesktopFullBookProgress) => void;
  pollIntervalMs?: number;
}

interface NormalizedProject {
  request: DesktopProjectRequest;
  manifestPath: string;
  projectDirectory: string;
  storePath: string;
  sourceVersion: string;
}

interface FullBookMetadata {
  schema: typeof FULLBOOK_SCHEMA;
  mode: DesktopTrialMode;
  runtimeFingerprint: string;
  styleProfileHash: string;
}

interface RunOverlay {
  projectKey: string;
  runId: string;
  sourceVersion: string;
  modelId: string;
  mode: DesktopTrialMode;
  phase: DesktopFullBookPhase;
  progress: DesktopFullBookRunSnapshot["progress"];
  error?: DesktopError;
}

interface ActiveFullBookTask {
  owner: DesktopFullBookService;
  project: NormalizedProject;
  runId: string;
  mode: DesktopTrialMode;
  controller: AbortController;
  phase: "preparing" | "running" | "pausing";
  modelId: string;
  settled: Promise<void>;
  poller?: ReturnType<typeof setInterval>;
}

let ACTIVE_FULLBOOK_TASK: ActiveFullBookTask | undefined;

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function emptyProgress(): DesktopFullBookRunSnapshot["progress"] {
  return {
    totalWindows: 0,
    pendingWindows: 0,
    runningWindows: 0,
    stagedWindows: 0,
    completedWindows: 0,
    warningWindows: 0,
    humanRequiredWindows: 0,
    failedWindows: 0,
  };
}

function publicProgress(
  status: LosslessBookStatusSummary,
): DesktopFullBookRunSnapshot["progress"] {
  return {
    totalWindows: status.totalWindows,
    pendingWindows: status.pendingWindows,
    runningWindows: status.runningWindows,
    stagedWindows: status.stagedWindows,
    completedWindows: status.completedWindows,
    warningWindows: status.warningWindows,
    humanRequiredWindows: status.humanRequiredWindows,
    failedWindows: status.failedWindows,
  };
}

function validRunId(value: unknown): value is string {
  return typeof value === "string"
    && value.trim().length > 0
    && value.length <= 200
    && !/[\u0000-\u001f]/u.test(value);
}

function requireMode(value: unknown): DesktopTrialMode {
  if (value !== "quality" && value !== "fast") {
    throw new DesktopFullBookError(
      "DESKTOP_FULLBOOK_INPUT_INVALID",
      "full-book mode must be quality or fast",
    );
  }
  return value;
}

function normalizeProject(project: DesktopProjectRequest): NormalizedProject {
  if (typeof project?.manifestPath !== "string"
    || !isAbsolute(project.manifestPath)) {
    throw new DesktopFullBookError(
      "DESKTOP_FULLBOOK_INPUT_INVALID",
      "full-book translation requires an absolute source manifest path",
    );
  }
  const manifestPath = resolve(project.manifestPath);
  if (basename(manifestPath) !== "source_manifest.json") {
    throw new DesktopFullBookError(
      "DESKTOP_FULLBOOK_INPUT_INVALID",
      "full-book translation requires source_manifest.json",
    );
  }
  const ledger = SourceLedger.open(manifestPath);
  let storePath: string;
  if (project.storePath === undefined) {
    storePath = join(dirname(manifestPath), "artifacts", "folioloom", "book.db");
  } else {
    if (!isAbsolute(project.storePath) || extname(project.storePath) !== ".db") {
      throw new DesktopFullBookError(
        "DESKTOP_FULLBOOK_INPUT_INVALID",
        "full-book store must be an absolute .db path",
      );
    }
    storePath = resolve(project.storePath);
  }
  return {
    request: { ...project, manifestPath, storePath },
    manifestPath,
    projectDirectory: resolve(dirname(manifestPath)),
    storePath,
    sourceVersion: ledger.sourceVersion,
  };
}

function fullBookMetadata(value: unknown): FullBookMetadata | undefined {
  const root = record(value);
  const item = record(root?.desktopFullBook);
  if (item?.schema !== FULLBOOK_SCHEMA
    || (item.mode !== "quality" && item.mode !== "fast")
    || typeof item.runtimeFingerprint !== "string"
    || item.runtimeFingerprint.length === 0
    || typeof item.styleProfileHash !== "string"
    || item.styleProfileHash.length === 0) {
    return undefined;
  }
  return {
    schema: FULLBOOK_SCHEMA,
    mode: item.mode,
    runtimeFingerprint: item.runtimeFingerprint,
    styleProfileHash: item.styleProfileHash,
  };
}

function runMetadata(
  mode: DesktopTrialMode,
  fingerprint: DesktopRuntimeFingerprint,
): { desktopFullBook: FullBookMetadata } {
  return {
    desktopFullBook: {
      schema: FULLBOOK_SCHEMA,
      mode,
      runtimeFingerprint: serializeDesktopRuntimeFingerprint(fingerprint),
      styleProfileHash: DEFAULT_STYLE_PROFILE_HASH,
    },
  };
}

function persistedPhase(
  progress: DesktopFullBookRunSnapshot["progress"],
): DesktopFullBookPhase {
  if (progress.humanRequiredWindows > 0 || progress.failedWindows > 0) {
    return "needs_attention";
  }
  if (progress.totalWindows > 0
    && progress.completedWindows === progress.totalWindows
    && progress.pendingWindows === 0
    && progress.runningWindows === 0
    && progress.stagedWindows === 0) {
    return "completed";
  }
  return "paused";
}

function capabilities(
  phase: DesktopFullBookPhase,
  progress: DesktopFullBookRunSnapshot["progress"],
): Pick<DesktopFullBookRunSnapshot, "canPause" | "canResume" | "canExport"> {
  const complete = progress.totalWindows > 0
    && progress.completedWindows === progress.totalWindows
    && progress.pendingWindows === 0
    && progress.runningWindows === 0
    && progress.stagedWindows === 0
    && progress.humanRequiredWindows === 0
    && progress.failedWindows === 0;
  return {
    canPause: phase === "preparing" || phase === "running",
    canResume: phase === "paused" || phase === "failed",
    canExport: phase === "completed" && complete,
  };
}

function publicRun(
  run: StoredTranslationRun,
  mode: DesktopTrialMode,
  progress: DesktopFullBookRunSnapshot["progress"],
  overlay?: RunOverlay,
): DesktopFullBookRunSnapshot {
  const phase = overlay?.phase ?? persistedPhase(progress);
  return {
    runId: run.runId,
    sourceVersion: run.sourceVersion,
    modelId: run.modelId,
    mode,
    phase,
    progress: overlay?.progress ?? progress,
    ...capabilities(phase, overlay?.progress ?? progress),
    ...(overlay?.error === undefined ? {} : { error: overlay.error }),
  };
}

function overlayRun(overlay: RunOverlay): DesktopFullBookRunSnapshot {
  return {
    runId: overlay.runId,
    sourceVersion: overlay.sourceVersion,
    modelId: overlay.modelId,
    mode: overlay.mode,
    phase: overlay.phase,
    progress: overlay.progress,
    ...capabilities(overlay.phase, overlay.progress),
    ...(overlay.error === undefined ? {} : { error: overlay.error }),
  };
}

function runtimePlan(
  mode: DesktopTrialMode,
  runtime: Awaited<ReturnType<DesktopRuntimeResolver["resolve"]>>,
): DesktopRuntimePlan {
  if (runtime === undefined) {
    throw new DesktopFullBookError(
      "DESKTOP_FULLBOOK_MODEL_NOT_READY",
      "a successfully tested model is required before starting a full-book translation",
    );
  }
  try {
    return buildDesktopRuntimePlan(mode, runtime);
  } catch (error) {
    if (error instanceof DesktopRuntimePlanError) {
      throw new DesktopFullBookError(
        "DESKTOP_FULLBOOK_RUNTIME_MISMATCH",
        error.message,
      );
    }
    throw error;
  }
}

export class DesktopFullBookService {
  readonly #runtime: DesktopRuntimeResolver;
  readonly #runBook: (options: LosslessBookRunOptions) => Promise<LosslessBookRunResult>;
  readonly #createRunId: () => string;
  readonly #onProgress: ((progress: DesktopFullBookProgress) => void) | undefined;
  readonly #pollIntervalMs: number;
  readonly #overlays = new Map<string, RunOverlay>();
  #lastProject: NormalizedProject | undefined;

  constructor(options: DesktopFullBookServiceOptions) {
    this.#runtime = options.runtime;
    this.#runBook = options.runBook ?? runBook;
    this.#createRunId = options.createRunId ?? randomUUID;
    this.#onProgress = options.onProgress;
    this.#pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
    if (!Number.isSafeInteger(this.#pollIntervalMs) || this.#pollIntervalMs <= 0) {
      throw new DesktopFullBookError(
        "DESKTOP_FULLBOOK_INPUT_INVALID",
        "pollIntervalMs must be a positive safe integer",
      );
    }
  }

  snapshot(projectRequest: DesktopProjectRequest): DesktopFullBookSnapshot {
    const project = normalizeProject(projectRequest);
    this.#lastProject = project;
    const persisted: DesktopFullBookRunSnapshot[] = [];
    const seen = new Set<string>();
    if (existsSync(project.storePath)) {
      const store = LosslessBookStore.openReadOnly(project.storePath);
      try {
        for (const run of store.listTranslationRuns()) {
          if (run.sourceVersion !== project.sourceVersion) continue;
          const metadata = fullBookMetadata(run.metadata);
          if (metadata === undefined) continue;
          const progress = publicProgress(store.statusSummary(run.runId));
          const overlay = this.#overlays.get(run.runId);
          persisted.push(publicRun(run, metadata.mode, progress, overlay));
          seen.add(run.runId);
        }
      } finally {
        store.close();
      }
    }
    for (const overlay of this.#overlays.values()) {
      if (overlay.projectKey === project.projectDirectory && !seen.has(overlay.runId)) {
        persisted.push(overlayRun(overlay));
      }
    }
    const active = ACTIVE_FULLBOOK_TASK;
    return {
      ...(active?.project.projectDirectory === project.projectDirectory
        ? { activeRunId: active.runId }
        : {}),
      runs: persisted,
    };
  }

  async start(
    projectRequest: DesktopProjectRequest,
    request: DesktopStartFullBookRequest,
  ): Promise<DesktopFullBookSnapshot> {
    const mode = requireMode(request?.mode);
    const project = normalizeProject(projectRequest);
    const runId = this.#createRunId();
    if (!validRunId(runId)) {
      throw new DesktopFullBookError(
        "DESKTOP_FULLBOOK_INPUT_INVALID",
        "the generated full-book run id is invalid",
      );
    }
    const task = this.#reserve(project, runId, mode);
    try {
      const beforeVersion = project.sourceVersion;
      const plan = runtimePlan(mode, await this.#runtime.resolve());
      task.controller.signal.throwIfAborted();
      const afterVersion = SourceLedger.open(project.manifestPath).sourceVersion;
      if (afterVersion !== beforeVersion) {
        throw new DesktopFullBookError(
          "DESKTOP_FULLBOOK_SOURCE_CHANGED",
          "the manuscript changed while the translation runtime was being prepared",
        );
      }
      task.modelId = plan.runtimeSet.primary.model.id;
      const metadata = runMetadata(mode, plan.fingerprint);
      return this.#launch(task, plan, {
        runId,
        protocolVersion: LOSSLESS_BOOK_PROTOCOL_VERSION,
        modelId: task.modelId,
        metadata,
      });
    } catch (error) {
      this.#releasePreparation(task);
      throw error;
    }
  }

  async resume(
    projectRequest: DesktopProjectRequest,
    request: DesktopResumeFullBookRequest,
  ): Promise<DesktopFullBookSnapshot> {
    if (!validRunId(request?.runId)) {
      throw new DesktopFullBookError(
        "DESKTOP_FULLBOOK_INPUT_INVALID",
        "runId must identify a full-book translation",
      );
    }
    const project = normalizeProject(projectRequest);
    if (!existsSync(project.storePath)) {
      throw new DesktopFullBookError(
        "DESKTOP_FULLBOOK_RUN_NOT_FOUND",
        "the full-book translation state database does not exist",
      );
    }
    const store = LosslessBookStore.openReadOnly(project.storePath);
    let storedRun: StoredTranslationRun | undefined;
    let metadata: FullBookMetadata | undefined;
    try {
      storedRun = store.listTranslationRuns().find((candidate) =>
        candidate.runId === request.runId
        && candidate.sourceVersion === project.sourceVersion);
      metadata = fullBookMetadata(storedRun?.metadata);
    } finally {
      store.close();
    }
    if (storedRun === undefined || metadata === undefined) {
      throw new DesktopFullBookError(
        "DESKTOP_FULLBOOK_RUN_NOT_FOUND",
        "the selected run is not a resumable desktop full-book translation",
      );
    }
    const task = this.#reserve(project, storedRun.runId, metadata.mode, storedRun.modelId);
    try {
      const plan = runtimePlan(metadata.mode, await this.#runtime.resolve());
      task.controller.signal.throwIfAborted();
      if (metadata.runtimeFingerprint
        !== serializeDesktopRuntimeFingerprint(plan.fingerprint)
        || storedRun.modelId !== plan.runtimeSet.primary.model.id) {
        throw new DesktopFullBookError(
          "DESKTOP_FULLBOOK_RUNTIME_MISMATCH",
          "the active model configuration does not match this full-book run",
        );
      }
      if (SourceLedger.open(project.manifestPath).sourceVersion !== project.sourceVersion) {
        throw new DesktopFullBookError(
          "DESKTOP_FULLBOOK_SOURCE_CHANGED",
          "the manuscript changed while the translation runtime was being prepared",
        );
      }
      return this.#launch(task, plan, {
        runId: storedRun.runId,
        protocolVersion: storedRun.protocolVersion,
        modelId: storedRun.modelId,
        metadata: storedRun.metadata,
      });
    } catch (error) {
      this.#releasePreparation(task);
      throw error;
    }
  }

  async pause(): Promise<DesktopFullBookSnapshot> {
    const task = ACTIVE_FULLBOOK_TASK;
    if (task === undefined) {
      return this.#lastProject === undefined
        ? { runs: [] }
        : this.snapshot(this.#lastProject.request);
    }
    task.phase = "pausing";
    this.#setOverlay(task, "pausing");
    this.#emit(task);
    if (!task.controller.signal.aborted) {
      task.controller.abort(new DesktopFullBookError(
        "DESKTOP_FULLBOOK_PAUSED",
        "full-book translation paused at a durable boundary",
      ));
    }
    await task.settled;
    return this.snapshot(task.project.request);
  }

  hasActiveTask(): boolean {
    return ACTIVE_FULLBOOK_TASK !== undefined;
  }

  async settleForShutdown(): Promise<void> {
    if (ACTIVE_FULLBOOK_TASK !== undefined) {
      await this.pause();
    }
  }

  #reserve(
    project: NormalizedProject,
    runId: string,
    mode: DesktopTrialMode,
    modelId = "",
  ): ActiveFullBookTask {
    if (ACTIVE_FULLBOOK_TASK !== undefined) {
      throw new DesktopFullBookError(
        "DESKTOP_FULLBOOK_ALREADY_RUNNING",
        "another full-book translation is already active",
      );
    }
    const task: ActiveFullBookTask = {
      owner: this,
      project,
      runId,
      mode,
      controller: new AbortController(),
      phase: "preparing",
      modelId,
      settled: Promise.resolve(),
    };
    ACTIVE_FULLBOOK_TASK = task;
    this.#lastProject = project;
    this.#setOverlay(task, "preparing");
    this.#emit(task);
    return task;
  }

  #launch(
    task: ActiveFullBookTask,
    plan: DesktopRuntimePlan,
    runMeta: LosslessBookRunOptions["runMeta"],
  ): DesktopFullBookSnapshot {
    task.phase = "running";
    this.#setOverlay(task, "running");
    this.#emit(task);
    task.poller = setInterval(() => this.#emit(task), this.#pollIntervalMs);
    task.poller.unref?.();

    let running: Promise<LosslessBookRunResult>;
    try {
      running = this.#runBook({
        manifestPath: task.project.manifestPath,
        storePath: task.project.storePath,
        runMeta,
        model: plan.runtimeSet.primary.model,
        streamFn: plan.runtimeSet.primary.streamFn,
        runtimeSet: plan.runtimeSet,
        signal: task.controller.signal,
      });
    } catch (error) {
      running = Promise.reject(error);
    }
    task.settled = running.then(
      (result) => {
        if (task.controller.signal.aborted) {
          this.#setOverlay(task, "paused", result.status);
        } else {
          const phase: DesktopFullBookPhase = result.outcome === "human_required"
            ? "needs_attention"
            : result.outcome === "partial"
              ? "paused"
              : "completed";
          this.#setOverlay(task, phase, result.status);
        }
      },
      (error: unknown) => {
        if (task.controller.signal.aborted) {
          this.#setOverlay(task, "paused");
        } else {
          this.#setOverlay(task, "failed", undefined, toDesktopError(error));
        }
      },
    ).finally(() => {
      if (task.poller !== undefined) {
        clearInterval(task.poller);
      }
      if (ACTIVE_FULLBOOK_TASK === task) {
        ACTIVE_FULLBOOK_TASK = undefined;
      }
      this.#emit(task);
    });
    return this.snapshot(task.project.request);
  }

  #releasePreparation(task: ActiveFullBookTask): void {
    if (task.poller !== undefined) {
      clearInterval(task.poller);
    }
    if (ACTIVE_FULLBOOK_TASK === task) {
      ACTIVE_FULLBOOK_TASK = undefined;
    }
    this.#overlays.delete(task.runId);
  }

  #setOverlay(
    task: ActiveFullBookTask,
    phase: DesktopFullBookPhase,
    status?: LosslessBookStatusSummary,
    error?: DesktopError,
  ): void {
    let progress = status === undefined ? emptyProgress() : publicProgress(status);
    if (status === undefined && existsSync(task.project.storePath)) {
      try {
        const store = LosslessBookStore.openReadOnly(task.project.storePath);
        try {
          const run = store.listTranslationRuns().find((item) => item.runId === task.runId);
          if (run !== undefined) {
            progress = publicProgress(store.statusSummary(task.runId));
          }
        } finally {
          store.close();
        }
      } catch {
        // The writer can be between atomic SQLite operations. Keep the last
        // public projection and let the next poll refresh it.
        progress = this.#overlays.get(task.runId)?.progress ?? progress;
      }
    }
    this.#overlays.set(task.runId, {
      projectKey: task.project.projectDirectory,
      runId: task.runId,
      sourceVersion: task.project.sourceVersion,
      modelId: task.modelId,
      mode: task.mode,
      phase,
      progress,
      ...(error === undefined ? {} : { error }),
    });
  }

  #emit(task: ActiveFullBookTask): void {
    try {
      const snapshot = this.snapshot(task.project.request);
      const run = snapshot.runs.find((item) => item.runId === task.runId);
      if (run === undefined) return;
      this.#onProgress?.({
        runId: run.runId,
        phase: run.phase,
        progress: run.progress,
      });
    } catch {
      // Progress is presentation plumbing. Durable execution and pause
      // semantics must not depend on an observer or a temporary read failure.
    }
  }
}
