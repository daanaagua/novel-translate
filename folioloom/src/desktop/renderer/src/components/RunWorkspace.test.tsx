import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";

import type {
  DesktopFullBookRunSnapshot,
  DesktopFullBookSnapshot,
} from "../../../contracts.js";
import { RunWorkspace } from "./RunWorkspace.js";

afterEach(() => cleanup());

function run(
  phase: DesktopFullBookRunSnapshot["phase"],
  overrides: Partial<DesktopFullBookRunSnapshot> = {},
): DesktopFullBookRunSnapshot {
  return {
    runId: "internal-run-id",
    sourceVersion: "internal-source-version",
    modelId: "deepseek-v4",
    mode: "quality",
    optimizationProfile: "balanced",
    phase,
    progress: {
      totalWindows: 100,
      pendingWindows: 67,
      runningWindows: phase === "running" ? 1 : 0,
      stagedWindows: 0,
      completedWindows: 30,
      warningWindows: 2,
      humanRequiredWindows: 0,
      failedWindows: 0,
    },
    canPause: phase === "running",
    canResume: phase === "paused" || phase === "failed",
    canExport: phase === "completed",
    ...overrides,
  };
}

function snapshot(item?: DesktopFullBookRunSnapshot): DesktopFullBookSnapshot {
  return item === undefined ? { runs: [] } : {
    ...(item.phase === "running" ? { activeRunId: item.runId } : {}),
    runs: [item],
  };
}

describe("RunWorkspace", () => {
  it("starts idle books with one of three optimization profiles", async () => {
    const user = userEvent.setup();
    const onStart = vi.fn();
    render(
      <RunWorkspace
        title="示例小说"
        modelReady
        snapshot={snapshot()}
        busy={false}
        onStart={onStart}
        onPause={vi.fn()}
        onResume={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "翻译运行" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "经济" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "均衡" }).getAttribute("aria-pressed"))
      .toBe("true");
    expect(screen.getByRole("button", { name: "极速" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "开始整本翻译" })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "经济" }));
    await user.click(screen.getByRole("button", { name: "开始整本翻译" }));
    expect(onStart).toHaveBeenCalledWith("economy");
  });

  it("shows durable progress and only the action allowed by the current phase", async () => {
    const user = userEvent.setup();
    const onPause = vi.fn();
    const onResume = vi.fn();
    const { rerender } = render(
      <RunWorkspace
        title="示例小说"
        modelReady
        snapshot={snapshot(run("running", {
          scheduler: {
            estimatedRemainingMs: 90_001,
            predictedTokenRange: { lower: 1_000, upper: 1_200 },
            wallTimeDeviationPercent: 5,
            tokenDeviationPercent: -2.5,
            adjustment: "throttled",
          },
        }))}
        busy={false}
        onStart={vi.fn()}
        onPause={onPause}
        onResume={onResume}
      />,
    );

    expect(screen.getByText("32 / 100 个文本块")).toBeTruthy();
    expect(screen.getByText("2 分钟")).toBeTruthy();
    expect(screen.getByText("1,000–1,200")).toBeTruthy();
    expect(screen.getByText("耗时 +5.0% · Token -2.5%")).toBeTruthy();
    expect(screen.getByText("正在因限流调整并发")).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("32");
    expect(screen.queryByRole("button", { name: "开始整本翻译" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "暂停" }));
    expect(onPause).toHaveBeenCalledTimes(1);

    rerender(
      <RunWorkspace
        title="示例小说"
        modelReady
        snapshot={snapshot(run("pausing"))}
        busy={false}
        onStart={vi.fn()}
        onPause={onPause}
        onResume={onResume}
      />,
    );
    expect(screen.getByText("正在完成当前文本块后暂停")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "继续翻译" })).toBeNull();

    rerender(
      <RunWorkspace
        title="示例小说"
        modelReady
        snapshot={snapshot(run("paused"))}
        busy={false}
        onStart={vi.fn()}
        onPause={onPause}
        onResume={onResume}
      />,
    );
    await user.click(screen.getByRole("button", { name: "继续翻译" }));
    expect(onResume).toHaveBeenCalledWith("internal-run-id");
    expect(screen.queryByText("internal-run-id")).toBeNull();
    expect(screen.queryByText(/SQLite|v5-book/u)).toBeNull();
  });

  it("keeps progress visible beside attention and retry guidance", () => {
    render(
      <RunWorkspace
        title="示例小说"
        modelReady
        snapshot={snapshot(run("needs_attention", {
          progress: {
            ...run("needs_attention").progress,
            humanRequiredWindows: 3,
            failedWindows: 1,
          },
        }))}
        busy={false}
        error={{
          code: "REQUEST_TIMEOUT",
          message: "网络连接超时",
          nextAction: "检查网络后继续。",
          retryable: true,
          technicalDetails: "socket timeout",
        }}
        onStart={vi.fn()}
        onPause={vi.fn()}
        onResume={vi.fn()}
      />,
    );
    expect(screen.getByText(/3 个文本块需要人工处理/u)).toBeTruthy();
    expect(screen.getByText("32 / 100 个文本块")).toBeTruthy();
    expect(screen.getByText("检查网络后继续。")).toBeTruthy();
    expect(screen.getByText("socket timeout")).toBeTruthy();
  });
});
