import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { app, BrowserWindow, dialog, ipcMain, Menu, safeStorage } from "electron";

import {
  DESKTOP_TRIAL_PROGRESS_CHANNEL,
  type DesktopProjectRequest,
  type DesktopTrialProgress,
} from "../contracts.js";
import { DesktopCredentialStore } from "../desktop-credential-store.js";
import { DesktopModelService } from "../desktop-model-service.js";
import { DesktopPreferences } from "../desktop-preferences.js";
import { DesktopProjectService } from "../desktop-project-service.js";
import { DesktopSourceService } from "../desktop-source-service.js";
import { DesktopTrialService } from "../desktop-trial-service.js";
import { createProviderRuntime } from "../../providers/runtime.js";
import { registerDesktopIpc } from "./ipc.js";
import { createDesktopProviderRegistryAdapter } from "./provider-model-adapter.js";
import {
  desktopWindowChrome,
  installNavigationGuards,
  isTrustedDesktopIpcEvent,
  preloadEntryPath,
  resolveDesktopRendererTarget,
} from "./runtime.js";

const currentDirectory = dirname(fileURLToPath(import.meta.url));
const trustedRendererUrls = new Map<number, string>();
const desktopChrome = desktopWindowChrome();
let trialServiceForShutdown: DesktopTrialService | undefined;
let quitAfterTrialSettles = false;
let quitSettlementStarted = false;

function broadcastTrialProgress(progress: DesktopTrialProgress): void {
  for (const window of BrowserWindow.getAllWindows()) {
    if (window.isDestroyed() || !trustedRendererUrls.has(window.webContents.id)) continue;
    window.webContents.send(DESKTOP_TRIAL_PROGRESS_CHANNEL, progress);
  }
}

function createWindow(): BrowserWindow {
  const rendererFilePath = join(currentDirectory, "../renderer/index.html");
  const rendererTarget = resolveDesktopRendererTarget({
    isPackaged: app.isPackaged,
    rendererFilePath,
    rendererUrl: process.env.ELECTRON_RENDERER_URL,
  });
  const window = new BrowserWindow({
    ...desktopChrome.windowOptions,
    width: 1440,
    height: 920,
    minWidth: 1100,
    minHeight: 720,
    backgroundColor: "#101318",
    title: "FolioLoom",
    webPreferences: {
      preload: preloadEntryPath(currentDirectory),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  const webContentsId = window.webContents.id;
  trustedRendererUrls.set(webContentsId, rendererTarget.expectedUrl);
  window.once("closed", () => {
    trustedRendererUrls.delete(webContentsId);
  });
  installNavigationGuards(window.webContents);
  if (rendererTarget.kind === "development") {
    void window.loadURL(rendererTarget.url);
  } else {
    void window.loadFile(rendererTarget.filePath);
  }
  return window;
}

function loadRecentRequest(preferences: DesktopPreferences): DesktopProjectRequest | undefined {
  const recent = preferences.load();
  if (recent === undefined) {
    return undefined;
  }
  if (!existsSync(recent.manifestPath)) {
    // The preferences file also carries model metadata; discard only the stale
    // recent-project pointer instead of erasing a verified model setup.
    preferences.saveState({ ...preferences.loadState(), recent: undefined });
    return undefined;
  }
  return recent;
}

void app.whenReady().then(() => {
  Menu.setApplicationMenu(desktopChrome.applicationMenu);
  const preferencesPath = join(app.getPath("userData"), "desktop-preferences.json");
  const preferences = new DesktopPreferences(preferencesPath);
  const projectService = new DesktopProjectService();
  const sourceService = new DesktopSourceService({
    projectsRoot: join(app.getPath("documents"), "FolioLoom", "Projects"),
  });
  const credentialStore = new DesktopCredentialStore({
    path: join(app.getPath("userData"), "desktop-credentials.json"),
    secretBox: safeStorage,
  });
  const modelService = new DesktopModelService({
    providers: createDesktopProviderRegistryAdapter(),
    preferences,
    credentials: credentialStore,
  });
  const trialService = new DesktopTrialService({
    runtime: {
      async resolve() {
        const profile = preferences.loadState().activeModelProfile;
        if (profile === undefined) return undefined;
        const credential = credentialStore.read(profile.providerId);
        if (credential.status !== "available") return undefined;
        const runtime = createProviderRuntime(profile, credential.credential);
        return { profile, model: runtime.model, streamFn: runtime.streamFn };
      },
    },
    onProgress(stage) {
      broadcastTrialProgress({ stage });
    },
  });
  trialServiceForShutdown = trialService;
  let currentRequest = loadRecentRequest(preferences);

  registerDesktopIpc({
    ipcMain: {
      handle(channel, handler) {
        ipcMain.handle(channel, handler);
      },
    },
    dialog: {
      showOpenDialog(options) {
        return dialog.showOpenDialog(options);
      },
    },
    projectService,
    sourceService,
    modelService,
    trialService,
    isTrustedEvent(event) {
      return isTrustedDesktopIpcEvent(event, trustedRendererUrls);
    },
    getCurrentRequest() {
      return currentRequest;
    },
    setCurrentRequest(request) {
      currentRequest = request;
      preferences.save(request);
    },
  });

  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", (event) => {
  if (quitAfterTrialSettles || trialServiceForShutdown === undefined) return;
  event.preventDefault();
  if (quitSettlementStarted) return;
  quitSettlementStarted = true;
  void trialServiceForShutdown.cancel().finally(() => {
    quitAfterTrialSettles = true;
    app.quit();
  });
});
