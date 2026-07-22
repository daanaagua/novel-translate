import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import type {
  DesktopDoctorReport,
  DesktopProjectRequest,
  DesktopProjectSnapshot,
  DesktopResult,
} from "../src/desktop/contracts.js";
import {
  registerDesktopIpc,
  type DesktopIpcDependencies,
  type DesktopIpcHandler,
  type DesktopIpcModelSnapshot,
  type DesktopIpcModelTestResult,
} from "../src/desktop/main/ipc.js";

const EMPTY_DOCTOR_REPORT: DesktopDoctorReport = {
  sourceVersion: "source-v1",
  sourceChars: 1,
  coveredChars: 1,
  annotationCount: 0,
  blockCount: 0,
  windowCount: 0,
  incidentCodes: [],
  anomalyCount: 0,
};

function ok<T>(value: T): DesktopResult<T> {
  return { ok: true, value };
}

function fail<T = never>(code: string, message: string): DesktopResult<T> {
  return { ok: false, error: { code, message, retryable: false } };
}

function snapshotFor(
  manifestPath: string,
  activeRunIds: readonly string[],
  request: DesktopProjectRequest,
): DesktopProjectSnapshot {
  const runs = activeRunIds.map((runId) => ({
    runId,
    sourceVersion: "source-v1",
    modelId: "desktop-test-model",
    status: "running",
    progress: {
      totalWindows: 1,
      pendingWindows: 1,
      completedWindows: 0,
      warningWindows: 0,
      humanRequiredWindows: 0,
      failedWindows: 0,
    },
  }));
  return {
    manifestPath,
    title: "fixture",
    sourceLanguage: "en",
    sourceChars: 1,
    sourceVersion: "source-v1",
    store: { state: "ready", ...(request.storePath === undefined ? {} : { path: request.storePath }) },
    runs,
    ...(runs.length === 1 ? { selectedRunId: runs[0]!.runId } : {}),
    runSelection: runs.length <= 1 ? "selected" : "required",
  };
}

interface IpcFixtureOptions {
  activeRunIds?: readonly string[];
  pickedStore?: "directory" | "text";
  pickedSource?: "cancel" | "source" | "invalid";
  existingReadyModel?: boolean;
  testStatus?: "ready" | "limited" | "failed";
}

interface IpcFixture {
  directory: string;
  manifestPath: string;
  sourcePath: string;
  textPath: string;
  handlers: Map<string, DesktopIpcHandler>;
  trustedEvent: unknown;
  snapshotCalls: number;
  sourceImports: readonly string[];
  modelCalls: {
    discoveries: readonly unknown[];
    tests: readonly unknown[];
    forgotten: readonly unknown[];
  };
  dialogFilters: ReadonlyArray<{ name: string; extensions: string[] }>;
  currentRequest: DesktopProjectRequest | undefined;
}

function registerFixtureHandlers(options: IpcFixtureOptions = {}): IpcFixture {
  const directory = mkdtempSync(join(tmpdir(), "folioloom-desktop-ipc-"));
  const manifestPath = join(directory, "source_manifest.json");
  const importedManifestPath = join(directory, "Imported", "source_manifest.json");
  const sourcePath = join(directory, "chapter.epub");
  const invalidSourcePath = join(directory, "chapter.exe");
  const textPath = join(directory, "not-a-store.txt");
  const activeRunIds = options.activeRunIds ?? [];
  const handlers = new Map<string, DesktopIpcHandler>();
  const sourceImports: string[] = [];
  const discoveries: unknown[] = [];
  const tests: unknown[] = [];
  const forgotten: unknown[] = [];
  const dialogFilters: Array<{ name: string; extensions: string[] }> = [];
  const trustedEvent = {
    sender: { id: 7 },
    senderFrame: { url: "file:///folioloom/index.html", parent: null },
  };
  let snapshotCalls = 0;
  let currentRequest: DesktopProjectRequest | undefined = { manifestPath };
  let activeModelProfile: {
    providerId: string;
    modelId: string;
    reasoningEffort?: string;
    customBaseUrl?: string;
  } | undefined = options.existingReadyModel
    ? { providerId: "deepseek", modelId: "saved-ready-model" }
    : undefined;
  let latestProbe: {
    status: "ready" | "limited" | "failed";
    code?: string;
    message?: string;
    retryable?: boolean;
    checkedAt?: string;
  } | undefined = options.existingReadyModel
    ? {
      status: "ready",
      code: "READY",
      message: "Saved model was verified.",
      retryable: false,
      checkedAt: "2026-07-22T00:00:00.000Z",
    }
    : undefined;

  const modelSnapshot = (): DesktopIpcModelSnapshot => ({
    providers: [{
      id: "deepseek",
      displayName: "DeepSeek",
      keyPlaceholder: "DeepSeek API Key",
      efforts: ["off", "high", "max"],
      fallbackModelIds: ["deepseek-chat"],
      allowManualModel: true,
      allowCustomBaseUrl: false,
      credentialStatus: activeModelProfile === undefined ? "missing" as const : "available" as const,
      ...(activeModelProfile === undefined ? {} : { credentialPersistence: "encrypted" as const }),
    }],
    ...(activeModelProfile === undefined ? {} : { activeModelProfile }),
    ...(latestProbe === undefined ? {} : { latestProbe }),
  });

  const dependencies: DesktopIpcDependencies = {
    ipcMain: {
      handle(channel, handler) {
        handlers.set(channel, handler);
      },
    },
    dialog: {
      async showOpenDialog(dialogOptions) {
        dialogFilters.push(...dialogOptions.filters.map((filter) => ({
          name: filter.name,
          extensions: [...filter.extensions],
        })));
        const extension = dialogOptions.filters[0]?.extensions[0];
        const selected = extension === "txt"
          ? options.pickedSource === "source"
            ? sourcePath
            : options.pickedSource === "invalid"
              ? invalidSourcePath
            : undefined
          : extension !== "db"
          ? manifestPath
          : options.pickedStore === "directory"
            ? directory
            : options.pickedStore === "text"
              ? textPath
              : undefined;
        return selected === undefined
          ? { canceled: true, filePaths: [] }
          : { canceled: false, filePaths: [selected] };
      },
    },
    projectService: {
      snapshot(request) {
        snapshotCalls += 1;
        if (request.storePath === directory || request.storePath === textPath) {
          return fail("DESKTOP_INPUT_INVALID", "storePath must identify a .db file");
        }
        return ok(snapshotFor(manifestPath, activeRunIds, request));
      },
      doctor() {
        return ok(EMPTY_DOCTOR_REPORT);
      },
    },
    sourceService: {
      async importSource(request) {
        sourceImports.push(request.sourcePath);
        return { manifestPath: importedManifestPath };
      },
    },
    modelService: {
      snapshot: modelSnapshot,
      async discoverModels(request) {
        discoveries.push(request);
        return [{ id: "deepseek-chat", displayName: "DeepSeek Chat" }];
      },
      async testAndSave(request): Promise<DesktopIpcModelTestResult> {
        tests.push(request);
        const status = options.testStatus ?? "ready";
        if (status === "ready") {
          activeModelProfile = {
            providerId: request.providerId,
            modelId: request.modelId,
            ...(request.reasoningEffort === undefined ? {} : { reasoningEffort: request.reasoningEffort }),
            ...(request.customBaseUrl === undefined ? {} : { customBaseUrl: request.customBaseUrl }),
          };
        }
        latestProbe = {
          status,
          code: status === "ready" ? "READY" : "TOOL_CALL_UNSUPPORTED",
          message: status === "ready" ? "Compatibility passed." : "Compatibility failed.",
          retryable: false,
          checkedAt: "2026-07-22T00:00:00.000Z",
        };
        return { report: latestProbe, snapshot: modelSnapshot() };
      },
      forgetCredential(providerId) {
        forgotten.push(providerId);
        activeModelProfile = undefined;
      },
    },
    isTrustedEvent(event) {
      return event === trustedEvent;
    },
    getCurrentRequest() {
      return currentRequest;
    },
    setCurrentRequest(request) {
      currentRequest = request;
    },
  };
  registerDesktopIpc(dependencies);
  return {
    directory,
    manifestPath,
    sourcePath,
    textPath,
    handlers,
    trustedEvent,
    get snapshotCalls() {
      return snapshotCalls;
    },
    get sourceImports() {
      return sourceImports;
    },
    get modelCalls() {
      return { discoveries, tests, forgotten };
    },
    get dialogFilters() {
      return dialogFilters;
    },
    get currentRequest() {
      return currentRequest;
    },
  };
}

function handler(fixture: IpcFixture, channel: string): DesktopIpcHandler {
  const registered = fixture.handlers.get(channel);
  if (registered === undefined) {
    throw new Error(`missing handler for ${channel}`);
  }
  return registered;
}

test("IPC only registers the desktop allowlist", () => {
  const fixture = registerFixtureHandlers();
  try {
    assert.deepEqual([...fixture.handlers.keys()].sort(), [
      "folioloom:choose-source",
      "folioloom:choose-project",
      "folioloom:choose-store",
      "folioloom:discover-models",
      "folioloom:doctor",
      "folioloom:forget-credential",
      "folioloom:onboarding-state",
      "folioloom:refresh-project",
      "folioloom:select-run",
      "folioloom:test-model",
    ].sort());
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("choose-source accepts only manuscript formats and cancellation preserves the active project", async () => {
  const fixture = registerFixtureHandlers({ pickedSource: "cancel" });
  try {
    const before = fixture.currentRequest;
    const result = await handler(fixture, "folioloom:choose-source")(fixture.trustedEvent) as DesktopResult<DesktopProjectSnapshot>;
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.code, "DESKTOP_SELECTION_CANCELLED");
    }
    assert.deepEqual(fixture.dialogFilters, [{
      name: "书稿",
      extensions: ["txt", "md", "markdown", "epub", "docx"],
    }]);
    assert.equal(fixture.sourceImports.length, 0);
    assert.deepEqual(fixture.currentRequest, before);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("choose-source imports a selected manuscript without exposing a path-taking renderer API", async () => {
  const fixture = registerFixtureHandlers({ pickedSource: "source" });
  try {
    const result = await handler(fixture, "folioloom:choose-source")(fixture.trustedEvent) as DesktopResult<DesktopProjectSnapshot>;
    assert.equal(result.ok, true);
    assert.deepEqual(fixture.sourceImports, [fixture.sourcePath]);
    assert.equal(fixture.currentRequest?.manifestPath.endsWith("Imported\\source_manifest.json"), true);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("choose-source refuses an unexpected dialog result before importing", async () => {
  const fixture = registerFixtureHandlers({ pickedSource: "invalid" });
  try {
    const result = await handler(fixture, "folioloom:choose-source")(fixture.trustedEvent) as DesktopResult<DesktopProjectSnapshot>;
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.code, "DESKTOP_INPUT_INVALID");
    }
    assert.equal(fixture.sourceImports.length, 0);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("onboarding IPC projects source/model readiness and never returns the submitted API Key", async () => {
  const fixture = registerFixtureHandlers();
  const apiKey = "desktop-ipc-secret";
  try {
    const testResult = await handler(fixture, "folioloom:test-model")(fixture.trustedEvent, {
      providerId: "deepseek",
      modelId: "deepseek-chat",
      reasoningEffort: "max",
      apiKey,
    }) as DesktopResult<unknown>;
    assert.equal(testResult.ok, true);
    assert.doesNotMatch(JSON.stringify(testResult), new RegExp(apiKey));
    assert.equal(fixture.modelCalls.tests.length, 1);

    const onboarding = await handler(fixture, "folioloom:onboarding-state")(fixture.trustedEvent) as DesktopResult<{
      readiness: { source: boolean; model: boolean; trial: boolean };
      activeModel?: { providerId: string; modelId: string; capability: string };
      providers: readonly { id: string; [key: string]: unknown }[];
    }>;
    assert.equal(onboarding.ok, true);
    if (!onboarding.ok) {
      throw new Error("onboarding state should succeed");
    }
    assert.deepEqual(onboarding.value.readiness, { source: true, model: true, trial: true });
    assert.deepEqual(onboarding.value.activeModel, {
      providerId: "deepseek",
      modelId: "deepseek-chat",
      reasoningEffort: "max",
      capability: "ready",
    });
    assert.equal(onboarding.value.providers[0]?.id, "deepseek");
    assert.doesNotMatch(JSON.stringify(onboarding.value), /apiKey|desktop-ipc-secret|https:\/\//u);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("onboarding keeps a previously saved ready model usable after another model test fails", async () => {
  const fixture = registerFixtureHandlers({ existingReadyModel: true, testStatus: "failed" });
  try {
    const result = await handler(fixture, "folioloom:test-model")(fixture.trustedEvent, {
      providerId: "deepseek",
      modelId: "unverified-model",
      apiKey: "failed-model-secret",
    }) as DesktopResult<{
      report: { status: string };
      onboarding: {
        readiness: { source: boolean; model: boolean; trial: boolean };
        activeModel?: { modelId: string; capability: string };
        latestProbe?: { status: string };
      };
    }>;
    assert.equal(result.ok, true);
    if (!result.ok) {
      throw new Error("model result should be projected");
    }
    assert.equal(result.value.report.status, "failed");
    assert.equal(result.value.onboarding.latestProbe?.status, "failed");
    assert.equal(result.value.onboarding.activeModel?.modelId, "saved-ready-model");
    assert.equal(result.value.onboarding.activeModel?.capability, "ready");
    assert.deepEqual(result.value.onboarding.readiness, { source: true, model: true, trial: true });
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("model IPC validates exact payloads before reaching services", async () => {
  const fixture = registerFixtureHandlers();
  try {
    const malformedTest = await handler(fixture, "folioloom:test-model")(fixture.trustedEvent, {
      providerId: "deepseek",
      modelId: "deepseek-chat",
      apiKey: 42,
    }) as DesktopResult<unknown>;
    const malformedDiscovery = await handler(fixture, "folioloom:discover-models")(fixture.trustedEvent, {
      providerId: "deepseek",
      sourcePath: "C:\\outside.txt",
    }) as DesktopResult<unknown>;
    const malformedForget = await handler(fixture, "folioloom:forget-credential")(fixture.trustedEvent, { providerId: "deepseek" }) as DesktopResult<unknown>;

    assert.equal(malformedTest.ok, false);
    assert.equal(malformedDiscovery.ok, false);
    assert.equal(malformedForget.ok, false);
    assert.equal(fixture.modelCalls.tests.length, 0);
    assert.equal(fixture.modelCalls.discoveries.length, 0);
    assert.equal(fixture.modelCalls.forgotten.length, 0);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("discover-models and forget-credential use fixed operations only", async () => {
  const fixture = registerFixtureHandlers();
  try {
    const discovered = await handler(fixture, "folioloom:discover-models")(fixture.trustedEvent, {
      providerId: "deepseek",
      apiKey: "discovery-only-secret",
    }) as DesktopResult<unknown>;
    const forgotten = await handler(fixture, "folioloom:forget-credential")(fixture.trustedEvent, "deepseek") as DesktopResult<unknown>;
    assert.equal(discovered.ok, true);
    assert.doesNotMatch(JSON.stringify(discovered), /discovery-only-secret/);
    assert.equal(forgotten.ok, true);
    assert.equal(fixture.modelCalls.discoveries.length, 1);
    assert.deepEqual(fixture.modelCalls.forgotten, ["deepseek"]);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("select-run accepts only a run id from the active snapshot", async () => {
  const fixture = registerFixtureHandlers({ activeRunIds: ["run-a"] });
  try {
    await handler(fixture, "folioloom:refresh-project")(fixture.trustedEvent);
    const selected = await handler(fixture, "folioloom:select-run")(fixture.trustedEvent, "run-a") as DesktopResult<DesktopProjectSnapshot>;
    assert.equal(selected.ok, true);

    const rejected = await handler(fixture, "folioloom:select-run")(fixture.trustedEvent, "..\\outside") as DesktopResult<DesktopProjectSnapshot>;
    assert.equal(rejected.ok, false);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("choose-store rejects a directory and a non-db selection", async () => {
  const directoryFixture = registerFixtureHandlers({ pickedStore: "directory" });
  try {
    const directoryResult = await handler(directoryFixture, "folioloom:choose-store")(directoryFixture.trustedEvent) as DesktopResult<DesktopProjectSnapshot>;
    assert.equal(directoryResult.ok, false);
  } finally {
    rmSync(directoryFixture.directory, { recursive: true, force: true });
  }

  const textFixture = registerFixtureHandlers({ pickedStore: "text" });
  try {
    const textResult = await handler(textFixture, "folioloom:choose-store")(textFixture.trustedEvent) as DesktopResult<DesktopProjectSnapshot>;
    assert.equal(textResult.ok, false);
  } finally {
    rmSync(textFixture.directory, { recursive: true, force: true });
  }
});

test("IPC rejects a foreign renderer event before it reads project state", async () => {
  const fixture = registerFixtureHandlers();
  try {
    const result = await handler(fixture, "folioloom:refresh-project")({
      sender: { id: 999 },
      senderFrame: { url: "https://attacker.example/", parent: null },
    }) as DesktopResult<DesktopProjectSnapshot>;
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.code, "DESKTOP_UNTRUSTED_IPC");
    }
    assert.equal(fixture.snapshotCalls, 0);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});
