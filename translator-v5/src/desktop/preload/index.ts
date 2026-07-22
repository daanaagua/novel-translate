import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

import {
  DESKTOP_TRIAL_PROGRESS_CHANNEL,
  type DesktopTrialProgress,
  type DesktopTrialStage,
} from "../contracts.js";
import type { FolioLoomDesktopApi } from "./folioloom-api.js";

const TRIAL_STAGES = new Set<DesktopTrialStage>([
  "preparing",
  "translating",
  "checking",
  "completed",
  "failed",
]);

function trialProgress(value: unknown): DesktopTrialProgress | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  if (Object.keys(record).length !== 1 || typeof record.stage !== "string") return undefined;
  return TRIAL_STAGES.has(record.stage as DesktopTrialStage)
    ? { stage: record.stage as DesktopTrialStage }
    : undefined;
}

const desktopApi: FolioLoomDesktopApi = {
  chooseSource: () => ipcRenderer.invoke("folioloom:choose-source"),
  confirmSourceEncoding: (request) => ipcRenderer.invoke("folioloom:confirm-source-encoding", request),
  getOnboardingState: () => ipcRenderer.invoke("folioloom:onboarding-state"),
  discoverModels: (request) => ipcRenderer.invoke("folioloom:discover-models", request),
  testModel: (request) => ipcRenderer.invoke("folioloom:test-model", request),
  forgetCredential: (providerId) => ipcRenderer.invoke("folioloom:forget-credential", providerId),
  startTrial: () => ipcRenderer.invoke("folioloom:start-trial"),
  cancelTrial: () => ipcRenderer.invoke("folioloom:cancel-trial"),
  onTrialProgress: (listener) => {
    const forward = (_event: IpcRendererEvent, value: unknown): void => {
      const progress = trialProgress(value);
      if (progress !== undefined) listener(progress);
    };
    ipcRenderer.on(DESKTOP_TRIAL_PROGRESS_CHANNEL, forward);
    return () => ipcRenderer.removeListener(DESKTOP_TRIAL_PROGRESS_CHANNEL, forward);
  },
  chooseProject: () => ipcRenderer.invoke("folioloom:choose-project"),
  chooseStore: () => ipcRenderer.invoke("folioloom:choose-store"),
  refreshProject: () => ipcRenderer.invoke("folioloom:refresh-project"),
  selectRun: (runId) => ipcRenderer.invoke("folioloom:select-run", runId),
  runDoctor: () => ipcRenderer.invoke("folioloom:doctor"),
};

contextBridge.exposeInMainWorld("folioLoom", desktopApi);
