import { contextBridge, ipcRenderer } from "electron";

import type { FolioLoomDesktopApi } from "./folioloom-api.js";

const desktopApi: FolioLoomDesktopApi = {
  chooseSource: () => ipcRenderer.invoke("folioloom:choose-source"),
  getOnboardingState: () => ipcRenderer.invoke("folioloom:onboarding-state"),
  discoverModels: (request) => ipcRenderer.invoke("folioloom:discover-models", request),
  testModel: (request) => ipcRenderer.invoke("folioloom:test-model", request),
  forgetCredential: (providerId) => ipcRenderer.invoke("folioloom:forget-credential", providerId),
  chooseProject: () => ipcRenderer.invoke("folioloom:choose-project"),
  chooseStore: () => ipcRenderer.invoke("folioloom:choose-store"),
  refreshProject: () => ipcRenderer.invoke("folioloom:refresh-project"),
  selectRun: (runId) => ipcRenderer.invoke("folioloom:select-run", runId),
  runDoctor: () => ipcRenderer.invoke("folioloom:doctor"),
};

contextBridge.exposeInMainWorld("folioLoom", desktopApi);
