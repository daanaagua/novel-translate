import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";

import type {
  DesktopOnboardingState,
  DesktopProjectSnapshot,
  DesktopResult,
  DesktopTestModelResult,
  DesktopTrialProgress,
} from "../../contracts.js";
import type { FolioLoomDesktopApi } from "../../preload/folioloom-api.js";
import { App } from "./App.js";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const providers: DesktopOnboardingState["providers"] = [
  {
    id: "deepseek",
    displayName: "DeepSeek",
    keyPlaceholder: "DeepSeek API Key",
    efforts: ["off", "high", "max"],
    fallbackModelIds: ["deepseek-chat", "deepseek-reasoner"],
    allowManualModel: true,
    allowCustomBaseUrl: false,
    credentialStatus: "missing",
  },
  {
    id: "kimi-cn",
    displayName: "Kimi",
    keyPlaceholder: "Kimi API Key",
    efforts: ["off", "high"],
    fallbackModelIds: ["moonshot-v1-8k"],
    allowManualModel: true,
    allowCustomBaseUrl: false,
    credentialStatus: "missing",
  },
  {
    id: "bailian",
    displayName: "阿里云百炼",
    keyPlaceholder: "百炼 API Key",
    efforts: ["off", "high"],
    fallbackModelIds: ["qwen-plus"],
    allowManualModel: true,
    allowCustomBaseUrl: false,
    credentialStatus: "missing",
  },
  {
    id: "volcengine",
    displayName: "火山方舟",
    keyPlaceholder: "火山 API Key",
    efforts: ["off", "high"],
    fallbackModelIds: ["doubao-seed"],
    allowManualModel: true,
    allowCustomBaseUrl: false,
    credentialStatus: "missing",
  },
  {
    id: "openai",
    displayName: "OpenAI",
    keyPlaceholder: "OpenAI API Key",
    efforts: ["low", "medium", "high"],
    fallbackModelIds: ["gpt-5-mini"],
    allowManualModel: true,
    allowCustomBaseUrl: false,
    credentialStatus: "missing",
  },
  {
    id: "siliconflow",
    displayName: "硅基流动",
    keyPlaceholder: "硅基流动 API Key",
    efforts: [],
    fallbackModelIds: ["deepseek-ai/DeepSeek-V3"],
    allowManualModel: true,
    allowCustomBaseUrl: false,
    credentialStatus: "missing",
  },
  {
    id: "openai-compatible",
    displayName: "自定义 OpenAI-compatible",
    keyPlaceholder: "兼容接口 API Key",
    efforts: ["on", "off"],
    fallbackModelIds: [],
    allowManualModel: true,
    allowCustomBaseUrl: true,
    credentialStatus: "missing",
  },
];

const project: DesktopProjectSnapshot = {
  manifestPath: "C:\\books\\example\\internal-project-file",
  title: "The Example Book",
  sourceLanguage: "英语",
  sourceChars: 12840,
  sourceVersion: "source-example",
  store: { state: "ready" },
  runs: [],
  runSelection: "none",
};

const emptyOnboarding: DesktopOnboardingState = {
  providers,
  readiness: { source: false, model: false, trial: false },
};

const readyOnboarding: DesktopOnboardingState = {
  ...emptyOnboarding,
  project,
  activeModel: {
    providerId: "deepseek",
    modelId: "deepseek-reasoner",
    reasoningEffort: "max",
    capability: "ready",
  },
  latestProbe: { status: "ready", message: "连接检查已通过" },
  readiness: { source: true, model: true, trial: true },
};

function ok<T>(value: T): DesktopResult<T> {
  return { ok: true, value };
}

function failure<T = never>(
  code: string,
  message: string,
  technicalDetails?: string,
): DesktopResult<T> {
  return {
    ok: false,
    error: {
      code,
      message,
      retryable: false,
      ...(technicalDetails === undefined ? {} : { technicalDetails }),
    },
  };
}

function testResult(onboarding: DesktopOnboardingState = readyOnboarding): DesktopTestModelResult {
  return {
    report: { status: "ready", message: "连接检查已通过" },
    onboarding,
  };
}

function createApi(overrides: Partial<FolioLoomDesktopApi> = {}): FolioLoomDesktopApi {
  return {
    chooseSource: vi.fn().mockResolvedValue(failure("DESKTOP_SELECTION_CANCELLED", "已取消选择")),
    getOnboardingState: vi.fn().mockResolvedValue(ok(emptyOnboarding)),
    discoverModels: vi.fn().mockResolvedValue(ok([])),
    testModel: vi.fn().mockResolvedValue(ok(testResult())),
    forgetCredential: vi.fn().mockResolvedValue(ok(emptyOnboarding)),
    startTrial: vi.fn().mockResolvedValue(failure("DESKTOP_TRIAL_NOT_READY", "试译尚未准备好")),
    cancelTrial: vi.fn().mockResolvedValue(ok(undefined)),
    onTrialProgress: vi.fn().mockReturnValue(() => undefined),
    chooseProject: vi.fn().mockResolvedValue(failure("DESKTOP_NO_PROJECT", "没有可打开的书稿")),
    chooseStore: vi.fn().mockResolvedValue(failure("DESKTOP_NO_PROJECT", "没有可打开的书稿")),
    refreshProject: vi.fn().mockResolvedValue(failure("DESKTOP_NO_PROJECT", "没有可打开的书稿")),
    selectRun: vi.fn().mockResolvedValue(failure("DESKTOP_NO_PROJECT", "没有可打开的书稿")),
    runDoctor: vi.fn().mockResolvedValue(failure("DESKTOP_NO_PROJECT", "没有可打开的书稿")),
    ...overrides,
  };
}

describe("FolioLoom desktop onboarding", () => {
  it("starts with a reader-facing manuscript choice instead of an engineering project picker", async () => {
    render(<App api={createApi()} />);

    expect(await screen.findByRole("heading", { name: "开始翻译一本书" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "选择书稿" })).toBeTruthy();
  });

  it("renders six direct providers and keeps the custom interface under more services", async () => {
    const user = userEvent.setup();
    render(<App api={createApi()} />);

    for (const name of ["DeepSeek", "Kimi", "阿里云百炼", "火山方舟", "OpenAI", "硅基流动"]) {
      expect(await screen.findByRole("button", { name })).toBeTruthy();
    }
    expect(screen.getByRole("button", { name: "更多服务" })).toBeTruthy();
    expect(screen.queryByLabelText("Base URL")).toBeNull();

    await user.click(screen.getByRole("button", { name: "更多服务" }));
    await user.click(screen.getByRole("button", { name: "自定义 OpenAI-compatible" }));

    expect(screen.getByLabelText("Base URL")).toBeTruthy();
    expect(screen.getByRole("button", { name: "on" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "off" })).toBeTruthy();
  });

  it("uses raw provider effort values without translating or normalizing them", async () => {
    const user = userEvent.setup();
    render(<App api={createApi()} />);

    await user.click(await screen.findByRole("button", { name: "DeepSeek" }));

    expect(screen.getByRole("button", { name: "high" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "max" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "最大" })).toBeNull();
  });

  it("clears the API Key field after the connection promise settles", async () => {
    const user = userEvent.setup();
    const testModel = vi.fn().mockResolvedValue(ok(testResult()));
    render(<App api={createApi({ testModel })} />);

    const apiKey = await screen.findByLabelText("API Key");
    await user.type(apiKey, "sk-temporary-key");
    await user.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => expect(testModel).toHaveBeenCalledWith(expect.objectContaining({
      providerId: "deepseek",
      apiKey: "sk-temporary-key",
    })));
    await waitFor(() => expect((apiKey as HTMLInputElement).value).toBe(""));
  });

  it("keeps the trial action disabled until the model is ready", async () => {
    const sourceOnly: DesktopOnboardingState = {
      ...emptyOnboarding,
      project,
      readiness: { source: true, model: false, trial: false },
      activeModel: {
        providerId: "deepseek",
        modelId: "deepseek-reasoner",
        capability: "limited",
      },
      latestProbe: { status: "limited", message: "这个模型还不能用于完整翻译流程" },
    };
    render(<App api={createApi({ getOnboardingState: vi.fn().mockResolvedValue(ok(sourceOnly)) })} />);

    expect((await screen.findByRole("button", { name: "开始试译" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("runs one real trial and renders the committed source beside its translation", async () => {
    const user = userEvent.setup();
    let publishProgress: ((progress: DesktopTrialProgress) => void) | undefined;
    const startTrial = vi.fn().mockImplementation(async () => {
      publishProgress?.({ stage: "translating" });
      publishProgress?.({ stage: "checking" });
      publishProgress?.({ stage: "completed" });
      return ok({
        runId: "trial-run-1",
        sourceText: "The bell rang above the empty court.",
        translationText: "钟声在空旷的庭院上空回响。",
      });
    });
    const onTrialProgress = vi.fn().mockImplementation((listener: (progress: DesktopTrialProgress) => void) => {
      publishProgress = listener;
      return () => { publishProgress = undefined; };
    });
    const { container } = render(<App api={createApi({
      getOnboardingState: vi.fn().mockResolvedValue(ok(readyOnboarding)),
      startTrial,
      onTrialProgress,
    })} />);

    const button = await waitFor(() => {
      const target = container.querySelector<HTMLButtonElement>('[data-action="start-trial"]');
      expect(target?.disabled).toBe(false);
      return target as HTMLButtonElement;
    });
    await user.click(button);

    expect(await screen.findByText("The bell rang above the empty court.")).toBeTruthy();
    expect(screen.getByText("钟声在空旷的庭院上空回响。")).toBeTruthy();
    expect(startTrial).toHaveBeenCalledTimes(1);
    expect(onTrialProgress).toHaveBeenCalledTimes(1);
  });

  it("shows a normal error first and keeps redacted technical detail collapsed", async () => {
    const user = userEvent.setup();
    const secret = "json-header-secret-should-not-reach-the-dom";
    const testModel = vi.fn().mockResolvedValue(failure(
      "AUTH_INVALID",
      "API Key 未被接受，请检查后重试。",
      `headers={"x-api-key":"${secret}"}`,
    ));
    const { container } = render(<App api={createApi({ testModel })} />);

    await user.type(await screen.findByLabelText("API Key"), secret);
    await user.click(screen.getByRole("button", { name: "测试连接" }));

    expect(await screen.findByText("API Key 未被接受，请检查后重试。")).toBeTruthy();
    const details = screen.getByText("技术详情").closest("details");
    expect(details?.open).toBe(false);
    expect(container.textContent).not.toContain(secret);
  });

  it("does not put internal workflow jargon in the default visible interface", async () => {
    const { container } = render(<App api={createApi()} />);
    await screen.findByRole("heading", { name: "开始翻译一本书" });
    const visibleText = container.textContent ?? "";

    for (const forbidden of ["V5", "source_manifest.json", "book.db", "canonical", "SQLite", "状态库", "协议"]) {
      expect(visibleText).not.toContain(forbidden);
    }
  });
});
