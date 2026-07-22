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
}

interface IpcFixture {
  directory: string;
  textPath: string;
  handlers: Map<string, DesktopIpcHandler>;
  trustedEvent: unknown;
  snapshotCalls: number;
}

function registerFixtureHandlers(options: IpcFixtureOptions = {}): IpcFixture {
  const directory = mkdtempSync(join(tmpdir(), "folioloom-desktop-ipc-"));
  const manifestPath = join(directory, "source_manifest.json");
  const textPath = join(directory, "not-a-store.txt");
  const activeRunIds = options.activeRunIds ?? [];
  const handlers = new Map<string, DesktopIpcHandler>();
  const trustedEvent = {
    sender: { id: 7 },
    senderFrame: { url: "file:///folioloom/index.html", parent: null },
  };
  let snapshotCalls = 0;
  let currentRequest: DesktopProjectRequest | undefined = { manifestPath };

  const dependencies: DesktopIpcDependencies = {
    ipcMain: {
      handle(channel, handler) {
        handlers.set(channel, handler);
      },
    },
    dialog: {
      async showOpenDialog(dialogOptions) {
        const extension = dialogOptions.filters[0]?.extensions[0];
        const selected = extension !== "db"
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
    textPath,
    handlers,
    trustedEvent,
    get snapshotCalls() {
      return snapshotCalls;
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
      "folioloom:choose-project",
      "folioloom:choose-store",
      "folioloom:doctor",
      "folioloom:refresh-project",
      "folioloom:select-run",
    ]);
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
