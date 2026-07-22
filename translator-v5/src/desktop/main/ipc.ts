import { basename } from "node:path";

import type {
  DesktopDoctorReport,
  DesktopProjectRequest,
  DesktopProjectSnapshot,
  DesktopResult,
} from "../contracts.js";
import { fail, toDesktopError } from "../desktop-errors.js";

export const DESKTOP_IPC_CHANNELS = [
  "folioloom:choose-project",
  "folioloom:choose-store",
  "folioloom:refresh-project",
  "folioloom:select-run",
  "folioloom:doctor",
] as const;

export type DesktopIpcChannel = (typeof DESKTOP_IPC_CHANNELS)[number];
export type DesktopIpcHandler = (event: unknown, ...args: unknown[]) => Promise<unknown>;

export interface DesktopIpcMain {
  handle(channel: DesktopIpcChannel, handler: DesktopIpcHandler): void;
}

export interface DesktopOpenDialogOptions {
  properties: "openFile"[];
  filters: Array<{ name: string; extensions: string[] }>;
}

export interface DesktopDialog {
  showOpenDialog(options: DesktopOpenDialogOptions): Promise<{
    canceled: boolean;
    filePaths: string[];
  }>;
}

export interface DesktopIpcProjectService {
  snapshot(request: DesktopProjectRequest): DesktopResult<DesktopProjectSnapshot>;
  doctor(
    request: Pick<DesktopProjectRequest, "manifestPath" | "glossaryPath">,
  ): DesktopResult<DesktopDoctorReport>;
}

export interface DesktopIpcDependencies {
  ipcMain: DesktopIpcMain;
  dialog: DesktopDialog;
  projectService: DesktopIpcProjectService;
  getCurrentRequest(): DesktopProjectRequest | undefined;
  setCurrentRequest(request: DesktopProjectRequest): void;
}

const manifestFilter = [{ name: "FolioLoom 项目", extensions: ["json"] }];
const storeFilter = [{ name: "FolioLoom 状态库", extensions: ["db"] }];

function failure<T = never>(code: string, message: string): DesktopResult<T> {
  return fail({ code, message, retryable: false });
}

function noOpenProject(): DesktopResult<DesktopProjectSnapshot> {
  return failure("DESKTOP_NO_PROJECT", "open an initialized project first");
}

function canceledSelection(): DesktopResult<DesktopProjectSnapshot> {
  return failure("DESKTOP_SELECTION_CANCELLED", "no file was selected");
}

function invalidSelection(message: string): DesktopResult<DesktopProjectSnapshot> {
  return failure("DESKTOP_INPUT_INVALID", message);
}

async function resultFrom<T>(
  operation: () => DesktopResult<T> | Promise<DesktopResult<T>>,
): Promise<DesktopResult<T>> {
  try {
    return await operation();
  } catch (error) {
    return fail(toDesktopError(error));
  }
}

async function chooseSingleFile(
  dialog: DesktopDialog,
  filters: Array<{ name: string; extensions: string[] }>,
): Promise<string | undefined> {
  const selection = await dialog.showOpenDialog({ properties: ["openFile"], filters });
  return selection.canceled || selection.filePaths.length !== 1
    ? undefined
    : selection.filePaths[0];
}

export function registerDesktopIpc(dependencies: DesktopIpcDependencies): void {
  let activeSnapshot: DesktopProjectSnapshot | undefined;

  const snapshot = (request: DesktopProjectRequest): DesktopResult<DesktopProjectSnapshot> => {
    const result = dependencies.projectService.snapshot(request);
    if (result.ok) {
      activeSnapshot = result.value;
    }
    return result;
  };

  dependencies.ipcMain.handle("folioloom:choose-project", async () => resultFrom(async () => {
    const manifestPath = await chooseSingleFile(dependencies.dialog, manifestFilter);
    if (manifestPath === undefined) {
      return canceledSelection();
    }
    if (basename(manifestPath) !== "source_manifest.json") {
      return invalidSelection("manifestPath must identify source_manifest.json");
    }
    const request: DesktopProjectRequest = { manifestPath };
    const result = snapshot(request);
    if (result.ok) {
      dependencies.setCurrentRequest(request);
    }
    return result;
  }));

  dependencies.ipcMain.handle("folioloom:choose-store", async () => resultFrom(async () => {
    const current = dependencies.getCurrentRequest();
    if (current === undefined) {
      return noOpenProject();
    }
    const storePath = await chooseSingleFile(dependencies.dialog, storeFilter);
    if (storePath === undefined) {
      return canceledSelection();
    }
    const request: DesktopProjectRequest = { ...current, storePath, runId: undefined };
    const result = snapshot(request);
    if (result.ok) {
      dependencies.setCurrentRequest(request);
    }
    return result;
  }));

  dependencies.ipcMain.handle("folioloom:refresh-project", async () => resultFrom(() => {
    const current = dependencies.getCurrentRequest();
    return current === undefined ? noOpenProject() : snapshot(current);
  }));

  dependencies.ipcMain.handle("folioloom:select-run", async (_event, runId) => resultFrom(() => {
    const current = dependencies.getCurrentRequest();
    if (current === undefined || activeSnapshot === undefined) {
      return noOpenProject();
    }
    if (typeof runId !== "string" || !activeSnapshot.runs.some((run) => run.runId === runId)) {
      return invalidSelection("runId must identify a run in the active project snapshot");
    }
    const request: DesktopProjectRequest = { ...current, runId };
    const result = snapshot(request);
    if (result.ok) {
      dependencies.setCurrentRequest(request);
    }
    return result;
  }));

  dependencies.ipcMain.handle("folioloom:doctor", async () => resultFrom(() => {
    const current = dependencies.getCurrentRequest();
    if (current === undefined) {
      return failure("DESKTOP_NO_PROJECT", "open an initialized project first");
    }
    return dependencies.projectService.doctor({
      manifestPath: current.manifestPath,
      ...(current.glossaryPath === undefined ? {} : { glossaryPath: current.glossaryPath }),
    });
  }));
}
