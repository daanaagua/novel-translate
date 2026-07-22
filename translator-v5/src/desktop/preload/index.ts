import { contextBridge, ipcRenderer } from "electron";

import type { FolioLoomDesktopApi } from "./folioloom-api.js";

const desktopApi: FolioLoomDesktopApi = {
  chooseProject: () => ipcRenderer.invoke("folioloom:choose-project"),
  chooseStore: () => ipcRenderer.invoke("folioloom:choose-store"),
  refreshProject: () => ipcRenderer.invoke("folioloom:refresh-project"),
  selectRun: (runId) => ipcRenderer.invoke("folioloom:select-run", runId),
  runDoctor: () => ipcRenderer.invoke("folioloom:doctor"),
};

contextBridge.exposeInMainWorld("folioLoom", desktopApi);
