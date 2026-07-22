import assert from "node:assert/strict";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import test from "node:test";

import {
  desktopWindowChrome,
  installNavigationGuards,
  isTrustedDesktopIpcEvent,
  preloadEntryPath,
  resolveDesktopRendererTarget,
  type DesktopNavigationWebContents,
} from "../src/desktop/main/runtime.js";

test("desktop chrome keeps native controls in a dark overlay and removes the application menu", () => {
  assert.deepEqual(desktopWindowChrome(), {
    applicationMenu: null,
    windowOptions: {
      autoHideMenuBar: true,
      titleBarStyle: "hidden",
      titleBarOverlay: {
        color: "#0c0f13",
        symbolColor: "#edf2f7",
        height: 42,
      },
    },
  });
});

test("preload path points to Electron Vite's emitted MJS preload entry", () => {
  const mainDirectory = join(tmpdir(), "folioloom", "out", "main");
  assert.equal(
    preloadEntryPath(mainDirectory),
    join(mainDirectory, "..", "preload", "index.mjs"),
  );
});

test("packaged runtime ignores arbitrary development renderer URLs", () => {
  const rendererFilePath = join(tmpdir(), "folioloom", "out", "renderer", "index.html");
  const target = resolveDesktopRendererTarget({
    isPackaged: true,
    rendererFilePath,
    rendererUrl: "https://attacker.example/",
  });

  assert.deepEqual(target, {
    kind: "file",
    filePath: rendererFilePath,
    expectedUrl: pathToFileURL(rendererFilePath).href,
  });
});

test("development runtime accepts only a loopback renderer URL", () => {
  const rendererFilePath = join(tmpdir(), "folioloom", "out", "renderer", "index.html");
  const remoteTarget = resolveDesktopRendererTarget({
    isPackaged: false,
    rendererFilePath,
    rendererUrl: "https://attacker.example/",
  });
  assert.equal(remoteTarget.kind, "file");

  const loopbackTarget = resolveDesktopRendererTarget({
    isPackaged: false,
    rendererFilePath,
    rendererUrl: "http://127.0.0.1:5173/",
  });
  assert.deepEqual(loopbackTarget, {
    kind: "development",
    url: "http://127.0.0.1:5173/",
    expectedUrl: "http://127.0.0.1:5173/",
  });
});

test("navigation guards deny remote navigation and every window.open request", () => {
  let navigationListener: ((event: { preventDefault(): void }, url: string) => void) | undefined;
  let windowOpenHandler: ((details: unknown) => { action: "deny" }) | undefined;
  const webContents: DesktopNavigationWebContents = {
    on(_event, listener) {
      navigationListener = listener;
    },
    setWindowOpenHandler(handler) {
      windowOpenHandler = handler;
    },
  };

  installNavigationGuards(webContents);
  assert.notEqual(navigationListener, undefined);
  assert.notEqual(windowOpenHandler, undefined);

  let prevented = false;
  navigationListener!({ preventDefault: () => { prevented = true; } }, "https://attacker.example/");
  assert.equal(prevented, true);
  assert.deepEqual(windowOpenHandler!({ url: "https://attacker.example/" }), { action: "deny" });
});

test("trusted IPC requires the app window, expected renderer URL, and main frame", () => {
  const trustedRenderers = new Map([[17, "http://127.0.0.1:5173/"]]);
  const trustedEvent = {
    sender: { id: 17 },
    senderFrame: { url: "http://127.0.0.1:5173/", parent: null },
  };
  assert.equal(isTrustedDesktopIpcEvent(trustedEvent, trustedRenderers), true);
  assert.equal(isTrustedDesktopIpcEvent({
    sender: { id: 18 },
    senderFrame: trustedEvent.senderFrame,
  }, trustedRenderers), false);
  assert.equal(isTrustedDesktopIpcEvent({
    sender: trustedEvent.sender,
    senderFrame: { url: "https://attacker.example/", parent: null },
  }, trustedRenderers), false);
  assert.equal(isTrustedDesktopIpcEvent({
    sender: trustedEvent.sender,
    senderFrame: { url: "http://127.0.0.1:5173/untrusted-route", parent: null },
  }, trustedRenderers), false);
  assert.equal(isTrustedDesktopIpcEvent({
    sender: trustedEvent.sender,
    senderFrame: { ...trustedEvent.senderFrame, parent: {} },
  }, trustedRenderers), false);
});
