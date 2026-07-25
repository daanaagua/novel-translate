import { useMemo, useState, type JSX } from "react";

import type {
  DesktopError,
  DesktopFullBookRunSnapshot,
  DesktopFullBookSnapshot,
  DesktopTrialMode,
} from "../../../contracts.js";
import { TechnicalDetails, redactTechnicalDetails } from "./TechnicalDetails.js";

interface RunWorkspaceProps {
  title: string;
  modelReady: boolean;
  snapshot: DesktopFullBookSnapshot;
  busy: boolean;
  error?: DesktopError;
  onStart(mode: DesktopTrialMode): void;
  onPause(): void;
  onResume(runId: string): void;
}

const phaseCopy: Readonly<Record<DesktopFullBookRunSnapshot["phase"], string>> = {
  idle: "尚未开始",
  preparing: "正在准备翻译",
  running: "翻译中",
  pausing: "正在完成当前文本块后暂停",
  paused: "已暂停，进度已保存",
  completed: "整本翻译已完成",
  needs_attention: "有内容需要处理",
  failed: "运行中断，进度已保存",
};

const phaseBadge: Readonly<Record<DesktopFullBookRunSnapshot["phase"], string>> = {
  idle: "未开始",
  preparing: "准备中",
  running: "运行中",
  pausing: "暂停中",
  paused: "已暂停",
  completed: "已完成",
  needs_attention: "需要处理",
  failed: "已中断",
};

function currentRun(snapshot: DesktopFullBookSnapshot): DesktopFullBookRunSnapshot | undefined {
  return snapshot.runs.find((run) => run.runId === snapshot.activeRunId)
    ?? snapshot.runs.at(-1);
}

export function RunWorkspace({
  title,
  modelReady,
  snapshot,
  busy,
  error,
  onStart,
  onPause,
  onResume,
}: RunWorkspaceProps): JSX.Element {
  const [mode, setMode] = useState<DesktopTrialMode>("quality");
  const run = useMemo(() => currentRun(snapshot), [snapshot]);
  const completed = run === undefined
    ? 0
    : run.progress.completedWindows + run.progress.warningWindows;
  const total = run?.progress.totalWindows ?? 0;
  const progressMaximum = Math.max(total, 1);

  return (
    <main className="onboarding-scroll">
      <div className="content-column workspace-page">
        <header className="workspace-heading">
          <p className="eyebrow">FolioLoom / Full book</p>
          <h1>翻译运行</h1>
          <p>{title}</p>
        </header>

        {run === undefined ? (
          <section className="workspace-card run-start-card">
            <div>
              <p className="eyebrow">运行方式</p>
              <h2>开始整本翻译</h2>
              <p className="workspace-copy">
                系统会保存每个文本块的结果，关闭程序前暂停即可在下次继续。
              </p>
            </div>
            <fieldset className="mode-choice" disabled={busy}>
              <legend>翻译模式</legend>
              <label>
                <input
                  type="radio"
                  aria-label="质量模式"
                  name="fullbook-mode"
                  checked={mode === "quality"}
                  onChange={() => setMode("quality")}
                />
                <span><strong>质量模式</strong><small>优先保留复杂语境与叙事连续性</small></span>
              </label>
              <label>
                <input
                  type="radio"
                  aria-label="快速模式"
                  name="fullbook-mode"
                  checked={mode === "fast"}
                  onChange={() => setMode("fast")}
                />
                <span><strong>快速模式</strong><small>减少推理开销，适合先生成可读初稿</small></span>
              </label>
            </fieldset>
            <button
              className="primary-button"
              type="button"
              disabled={!modelReady || busy}
              onClick={() => onStart(mode)}
            >
              {busy ? "正在启动" : "开始整本翻译"}
            </button>
            {!modelReady ? <p className="workspace-hint">请先在项目概览完成模型连接测试。</p> : null}
          </section>
        ) : (
          <section className="workspace-card run-progress-card">
            <header className="run-progress-header">
              <div>
                <p className="eyebrow">当前状态</p>
                <h2>{phaseCopy[run.phase]}</h2>
              </div>
              <span className={`run-state is-${run.phase}`}>{phaseBadge[run.phase]}</span>
            </header>

            <div className="progress-copy">
              <strong>{completed} / {total} 个文本块</strong>
              <span>{total === 0 ? "等待生成分块计划" : `${Math.round((completed / total) * 100)}%`}</span>
            </div>
            <progress
              aria-label="整本翻译进度"
              aria-valuenow={completed}
              aria-valuemin={0}
              aria-valuemax={total}
              value={completed}
              max={progressMaximum}
            />

            <div className="run-metrics">
              <article><span>等待</span><strong>{run.progress.pendingWindows}</strong></article>
              <article><span>进行中</span><strong>{run.progress.runningWindows}</strong></article>
              <article><span>警告</span><strong>{run.progress.warningWindows}</strong></article>
              <article><span>需要处理</span><strong>{run.progress.humanRequiredWindows}</strong></article>
            </div>

            {run.phase === "needs_attention" ? (
              <div className="workspace-notice is-warning">
                {run.progress.humanRequiredWindows > 0
                  ? <p>{run.progress.humanRequiredWindows} 个文本块需要人工处理。</p>
                  : null}
                {run.progress.failedWindows > 0
                  ? <p>{run.progress.failedWindows} 个文本块运行失败。</p>
                  : null}
              </div>
            ) : null}

            <div className="workspace-actions">
              {run.canPause ? (
                <button
                  className="primary-button"
                  type="button"
                  disabled={busy}
                  onClick={onPause}
                >
                  暂停
                </button>
              ) : null}
              {run.canResume ? (
                <button
                  className="primary-button"
                  type="button"
                  disabled={busy || !modelReady}
                  onClick={() => onResume(run.runId)}
                >
                  {busy ? "正在继续" : "继续翻译"}
                </button>
              ) : null}
            </div>
          </section>
        )}

        {error === undefined ? null : (
          <section className="operation-error" role="status">
            <p>{redactTechnicalDetails(error.message)}</p>
            {error.nextAction === undefined
              ? null
              : <p>{redactTechnicalDetails(error.nextAction)}</p>}
            <TechnicalDetails details={error.technicalDetails} />
          </section>
        )}
      </div>
    </main>
  );
}
