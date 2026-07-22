import { existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { app, BrowserWindow, dialog, ipcMain, Menu } from "electron";

import type { DesktopProjectRequest } from "../contracts.js";
import { DesktopPreferences } from "../desktop-preferences.js";
import { DesktopProjectService } from "../desktop-project-service.js";
import { registerDesktopIpc } from "./ipc.js";
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

function loadRecentRequest(preferences: DesktopPreferences, preferencesPath: string): DesktopProjectRequest | undefined {
  const recent = preferences.load();
  if (recent === undefined) {
    return undefined;
  }
  if (!existsSync(recent.manifestPath)) {
    rmSync(preferencesPath, { force: true });
    return undefined;
  }
  return recent;
}

void app.whenReady().then(() => {
  Menu.setApplicationMenu(desktopChrome.applicationMenu);
  const preferencesPath = join(app.getPath("userData"), "desktop-preferences.json");
  const preferences = new DesktopPreferences(preferencesPath);
  const projectService = new DesktopProjectService();
  let currentRequest = loadRecentRequest(preferences, preferencesPath);

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
