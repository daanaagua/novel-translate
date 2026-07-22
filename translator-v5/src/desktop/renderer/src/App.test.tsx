import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";

import type {
  DesktopDoctorReport,
  DesktopProjectSnapshot,
  DesktopResult,
} from "../../contracts.js";
import type { FolioLoomDesktopApi } from "../../preload/folioloom-api.js";
import { App } from "./App.js";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const snapshot: DesktopProjectSnapshot = {
  manifestPath: "C:\\books\\example\\source_manifest.json",
  title: "The Example Book",
  sourceLanguage: "en",
  sourceChars: 12840,
  sourceVersion: "source-example",
  store: {
    state: "ready",
    path: "C:\\books\\example\\book.db",
  },
  runs: [
    {
      runId: "run-example",
      sourceVersion: "source-example",
      modelId: "deepseek-v4-flash",
      status: "running",
      progress: {
        totalWindows: 4,
        pendingWindows: 3,
        completedWindows: 1,
        warningWindows: 0,
        humanRequiredWindows: 0,
        failedWindows: 0,
      },
    },
  ],
  selectedRunId: "run-example",
  runSelection: "selected",
};

const doctorReport: DesktopDoctorReport = {
  sourceVersion: "source-example",
  sourceChars: 12840,
  coveredChars: 12840,
  annotationCount: 12,
  blockCount: 8,
  windowCount: 4,
  incidentCodes: [],
  anomalyCount: 0,
  glossary: {
    path: "C:\\books\\example\\glossary.json",
    totalTerms: 3,
    matchedTerms: 3,
    unmatchedTerms: 0,
    unmatchedForms: [],
  },
};

function ok<T>(value: T): DesktopResult<T> {
  return { ok: true, value };
}

function failure<T = never>(code: string, message: string): DesktopResult<T> {
  return { ok: false, error: { code, message, retryable: false } };
}

function createApi(overrides: Partial<FolioLoomDesktopApi> = {}): FolioLoomDesktopApi {
  return {
    chooseProject: vi.fn().mockResolvedValue(ok(snapshot)),
    chooseStore: vi.fn().mockResolvedValue(ok(snapshot)),
    refreshProject: vi.fn().mockResolvedValue(ok(snapshot)),
    selectRun: vi.fn().mockResolvedValue(ok(snapshot)),
    runDoctor: vi.fn().mockResolvedValue(ok(doctorReport)),
    ...overrides,
  };
}

const emptyApi = createApi({
  refreshProject: vi.fn().mockResolvedValue(failure("DESKTOP_NO_PROJECT", "open an initialized project first")),
});

const projectApi = createApi({
  refreshProject: vi.fn().mockResolvedValue(failure("DESKTOP_NO_PROJECT", "open an initialized project first")),
});

const doctorFailureApi = createApi({
  runDoctor: vi.fn().mockResolvedValue(failure(
    "CANONICAL_HASH_MISMATCH",
    "canonical source hash does not match the manifest",
  )),
});

const noStoreSnapshot: DesktopProjectSnapshot = {
  ...snapshot,
  store: { state: "not_found" },
  runs: [],
  selectedRunId: undefined,
  runSelection: "none",
};

const multiRunSnapshot: DesktopProjectSnapshot = {
  ...snapshot,
  runs: [
    {
      ...snapshot.runs[0]!,
      runId: "run-left",
      modelId: "model-left",
    },
    {
      ...snapshot.runs[0]!,
      runId: "run-right",
      modelId: "model-right",
    },
  ],
  selectedRunId: undefined,
  runSelection: "required",
};

describe("FolioLoom desktop workbench", () => {
  it("renders one integrated application titlebar", () => {
    render(<App api={emptyApi} />);

    expect(screen.getByRole("banner", { name: "应用标题栏" })).toBeTruthy();
    expect(screen.getByText("FolioLoom · 翻译中")).toBeTruthy();
  });

  it("uses a relative production logo asset path for Electron file pages", () => {
    const { container } = render(<App api={emptyApi} />);
    const mark = container.querySelector<HTMLImageElement>(".brand-mark");

    expect(mark?.getAttribute("src")).toBe("./folioloom-mark.svg");
  });

  it("empty workbench asks the user to open an initialized project", () => {
    render(<App api={emptyApi} />);

    expect(screen.getByRole("button", { name: "打开项目" })).toBeTruthy();
    expect(screen.getByText("尚未打开项目")).toBeTruthy();
  });

  it("project overview renders 翻译中 and real counters without a fake percentage", async () => {
    const user = userEvent.setup();
    render(<App api={projectApi} />);

    await user.click(screen.getByRole("button", { name: "打开项目" }));

    expect(await screen.findByRole("heading", { name: "翻译中" })).toBeTruthy();
    expect(screen.getByText("已完成 1 / 4 窗口")).toBeTruthy();
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it("doctor failure keeps a structured next step visible", async () => {
    const user = userEvent.setup();
    render(<App api={doctorFailureApi} />);

    await user.click(await screen.findByRole("button", { name: "运行检查" }));

    expect(await screen.findByText("CANONICAL_HASH_MISMATCH")).toBeTruthy();
    expect(screen.getByText("请恢复与 manifest 匹配的 canonical 原文后重试")).toBeTruthy();
  });

  it("shows a readable no-store state instead of an invented run status", async () => {
    const noStoreApi = createApi({
      refreshProject: vi.fn().mockResolvedValue(ok(noStoreSnapshot)),
    });
    render(<App api={noStoreApi} />);

    expect(await screen.findByRole("heading", { name: "尚未开始翻译" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "选择状态库" })).toBeTruthy();
  });

  it("renders successful doctor coverage and structural evidence", async () => {
    const user = userEvent.setup();
    render(<App api={createApi()} />);

    await user.click(await screen.findByRole("button", { name: "运行检查" }));

    expect(await screen.findByRole("heading", { name: "原文检查已完成" })).toBeTruthy();
    expect(screen.getByText("已覆盖 12,840 / 12,840 字")).toBeTruthy();
    expect(screen.getByText("结构块")).toBeTruthy();
  });

  it("lets the reader choose a concrete run when more than one matches", async () => {
    const user = userEvent.setup();
    const selectedSnapshot: DesktopProjectSnapshot = {
      ...multiRunSnapshot,
      selectedRunId: "run-right",
      runSelection: "selected",
    };
    const selectRun = vi.fn().mockResolvedValue(ok(selectedSnapshot));
    const multiRunApi = createApi({
      refreshProject: vi.fn().mockResolvedValue(ok(multiRunSnapshot)),
      selectRun,
    });
    render(<App api={multiRunApi} />);

    expect(await screen.findByRole("heading", { name: "请选择要查看的运行" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /model-right/ }));

    expect(await screen.findByRole("heading", { name: "model-right" })).toBeTruthy();
    expect(selectRun).toHaveBeenCalledWith("run-right");
  });

  it("keeps a safe empty state when the preload bridge is unavailable", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("folioLoom", undefined);
    render(<App />);

    expect(await screen.findByText("尚未打开项目")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "打开项目" }));
    expect(screen.queryByText("DESKTOP_RENDERER_ERROR")).toBeNull();
  });

  it("inactive workspace says it is unavailable instead of exposing a fake action", async () => {
    const user = userEvent.setup();
    render(<App api={createApi()} />);

    await user.click(await screen.findByRole("button", { name: "审阅队列" }));

    expect(await screen.findByText("将在运行控制阶段接入")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "开始审阅" })).toBeNull();
  });
});
